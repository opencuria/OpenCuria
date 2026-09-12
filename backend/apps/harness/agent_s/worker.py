"""Worker step orchestration (mirrors ``Worker.generate_next_action``).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

One :func:`run_step` call reproduces a full Agent-S turn:

1. reflection gating (turn 0 seeds the reflection conversation; the LLM is
   only called from turn 1 on — "reflection from turn 2" in 1-based terms);
2. generator message assembly — ``""`` on later turns,
   ``"The initial screen is provided. No action has been taken yet."`` on
   turn 0, optional ``REFLECTION: ...`` block, ``Current Text Buffer`` notes
   line, and the previous code-agent result block (consumed/reset after use);
3. ``[SINGLE_ACTION, CODE_VALID]`` formatted generation at the worker
   temperature (CODE_VALID materializes once — the double-materialization
   quirk);
4. history append (raw plan), assistant echo, ``parse_code_from_string`` +
   re-materialization (fallback ``wait(1.333)`` when empty/unparseable —
   Agent-S catches *all* exceptions including ``assert``);
5. turn counter bump, screenshot log, and trajectory flush.

``executor_info`` keys (``plan``/``plan_code``/``exec_code``/``reflection``/
``reflection_thoughts``/``code_agent_output``) are preserved in
:class:`StepOutput` alongside per-purpose usage records.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from typing import Any

from . import formatting as _formatting
from . import grounding as _grounding
from . import messages as _messages
from . import parsing as _parsing
from . import prompts as _prompts
from . import worker_prompt as _worker_prompt
from .actions import AgentActionSurface
from .actions_parser import ActionParseError, ParsedAction, parse_action_line
from .llm import call_llm_formatted, call_llm_safe
from .materializer import DefaultActionMaterializer
from .ports import ActionMaterializer, CallPurpose, CompletionPort, Usage


@dataclass
class StepUsage:
    purpose: CallPurpose
    usage: Usage


@dataclass
class StepOutput:
    plan: str
    plan_code: str
    exec_code: str
    reflection: str | None
    reflection_thoughts: str | None
    code_agent_output: dict[str, Any] | None
    terminal: str | None = None
    usages: list[StepUsage] = field(default_factory=list)


@dataclass
class WorkerState:
    platform: str = "linux"
    temperature: float | None = None
    use_thinking: bool = False
    max_trajectory_length: int = 8
    enable_reflection: bool = True
    engine_type: str = ""
    generator_system_template: str = ""
    generator_messages: list[dict[str, Any]] = field(default_factory=list)
    reflection_messages: list[dict[str, Any]] = field(default_factory=list)
    reflection_system: str = ""
    turn_count: int = 0
    worker_history: list[str] = field(default_factory=list)
    reflections: list[str] = field(default_factory=list)
    screenshot_inputs: list[bytes] = field(default_factory=list)


def create_state(
    *,
    platform: str = "linux",
    worker_engine_params: dict[str, Any] | None = None,
    skipped_actions: list[str] | None = None,
    code_execution_available: bool = False,
    max_trajectory_length: int = 8,
    enable_reflection: bool = True,
) -> WorkerState:
    """Build initial worker state (mirrors ``Worker.__init__``/``reset``).

    ``code_execution_available`` mirrors Agent-S hiding ``call_code_agent``
    when no env/controller is present; it is only consulted when
    ``skipped_actions`` is ``None``. The default ``False`` hides the code
    action for direct use without a code backend (advertising it would let
    the planner call an action that raises ``RuntimeError``).
    """
    params = dict(worker_engine_params or {})
    if "temperature" not in params:
        # Agent-S CLI passes no temperature (None): keep None (provider
        # default) instead of inventing 0.0.
        temperature: float | None = None
    else:
        raw_temperature = params.get("temperature")
        temperature = None if raw_temperature is None else float(raw_temperature)
    use_thinking = str(params.get("model", "")) in _prompts.CLAUDE_THINKING_MODELS
    engine_type = str(params.get("engine_type", ""))
    if skipped_actions is None:
        skipped_actions = _worker_prompt.default_skipped_actions(
            platform, code_execution_available
        )
    template = _worker_prompt.build_initial_system_prompt(
        AgentActionSurface, platform, skipped_actions
    )
    reflection_system = _prompts.REFLECTION_ON_TRAJECTORY
    return WorkerState(
        platform=platform,
        temperature=temperature,
        use_thinking=use_thinking,
        max_trajectory_length=max_trajectory_length,
        enable_reflection=enable_reflection,
        engine_type=engine_type,
        generator_system_template=template,
        generator_messages=[_messages.system_message(template)],
        reflection_messages=[_messages.system_message(reflection_system)],
        reflection_system=reflection_system,
        turn_count=0,
    )


_INITIAL_SCREEN_TEXT = "The initial screen is provided. No action has been taken yet."


def flush_messages(state: WorkerState) -> None:
    """Trim trajectory history (mirrors ``Worker.flush_messages``).

    Long-context engines (anthropic/openai/gemini) keep all text and only
    the latest ``max_trajectory_length`` images across generator+reflection
    conversations (deleting individual image parts, newest first). All other
    engines drop whole turns: generator is ``[system, user, assistant, ...]``
    so two entries per round; reflection is ``[system, user, ...]`` so one
    per round.
    """
    if state.engine_type in ("anthropic", "openai", "gemini"):
        max_images = state.max_trajectory_length
        for conv in (state.generator_messages, state.reflection_messages):
            img_count = 0
            for i in range(len(conv) - 1, -1, -1):
                content = conv[i].get("content", [])
                for j in range(len(content) - 1, -1, -1):
                    if _messages.is_image_part(content[j]):
                        img_count += 1
                        if img_count > max_images:
                            del content[j]
    else:
        if len(state.generator_messages) > 2 * state.max_trajectory_length + 1:
            state.generator_messages.pop(1)
            state.generator_messages.pop(1)
        if len(state.reflection_messages) > state.max_trajectory_length + 1:
            state.reflection_messages.pop(1)


def build_reflection_seed(instruction: str) -> str:
    """Turn-0 reflection system-prompt suffix (exact ``textwrap.dedent``)."""
    return textwrap.dedent(
        f"""
                    Task Description: {instruction}
                    Current Trajectory below:
                    """
    )


def build_code_agent_block(code_result: dict[str, Any]) -> str:
    """Render the ``CODE AGENT RESULT`` generator-message block."""
    block = "\nCODE AGENT RESULT:\n"
    block += f"Task/Subtask Instruction: {code_result['task_instruction']}\n"
    block += f"Steps Completed: {code_result['steps_executed']}\n"
    block += f"Max Steps: {code_result['budget']}\n"
    block += f"Completion Reason: {code_result['completion_reason']}\n"
    block += f"Summary: {code_result['summary']}\n"
    if code_result["execution_history"]:
        block += "Execution History:\n"
        for i, step in enumerate(code_result["execution_history"]):
            action = step["action"]
            if "```python" in action:
                code_start = action.find("```python") + 9
                code_end = action.find("```", code_start)
                if code_end != -1:
                    python_code = action[code_start:code_end].strip()
                    block += f"Step {i + 1}: \n```python\n{python_code}\n```\n"
                else:
                    block += f"Step {i + 1}: \n{action}\n"
            elif "```bash" in action:
                code_start = action.find("```bash") + 7
                code_end = action.find("```", code_start)
                if code_end != -1:
                    bash_code = action[code_start:code_end].strip()
                    block += f"Step {i + 1}: \n```bash\n{bash_code}\n```\n"
                else:
                    block += f"Step {i + 1}: \n{action}\n"
            else:
                block += f"Step {i + 1}: \n{action}\n"
    block += "\n"
    return block


async def _generate_reflection(
    state: WorkerState,
    completion: CompletionPort,
    instruction: str,
    obs: dict[str, Any],
    record,
    sleep,
) -> tuple[str | None, str | None]:
    reflection = None
    reflection_thoughts = None
    if state.enable_reflection:
        if state.turn_count == 0:
            text_content = build_reflection_seed(instruction)
            updated = state.reflection_system + "\n" + text_content
            state.reflection_system = updated
            _messages.replace_system_prompt(state.reflection_messages, updated)
            _messages.append_message(
                state.reflection_messages,
                text_content=_INITIAL_SCREEN_TEXT,
                image_content=obs["screenshot"],
                role="user",
            )
        else:
            _messages.append_message(
                state.reflection_messages,
                text_content=state.worker_history[-1],
                image_content=obs["screenshot"],
                role="user",
            )
            full_reflection, usages = await call_llm_safe(
                completion,
                state.reflection_messages,
                purpose="reflection",
                temperature=state.temperature,
                use_thinking=state.use_thinking,
                record_usage=record,
                sleep=sleep,
            )
            reflection, reflection_thoughts = _parsing.split_thinking_response(
                full_reflection
            )
            state.reflections.append(reflection)
    return reflection, reflection_thoughts


async def _materialize_parsed(
    materializer: ActionMaterializer,
    action: ParsedAction,
    obs: dict[str, Any],
    binders,
) -> Any:
    from .actions_parser import coerce_action

    coerced = action
    if binders is not None:
        try:
            coerced = coerce_action(action, binders)
        except ActionParseError:
            pass
    return await materializer.materialize(coerced, obs)


async def run_step(
    state: WorkerState,
    completion: CompletionPort,
    materializer: ActionMaterializer,
    instruction: str,
    obs: dict[str, Any],
    *,
    record=None,
    sleep=None,
    signature_binders=None,
) -> StepOutput:
    """Orchestrate one full Agent-S step; returns the typed step output."""
    usages: list[StepUsage] = []

    def _record(purpose: CallPurpose, usage: Usage) -> None:
        usages.append(StepUsage(purpose=purpose, usage=usage))
        if record is not None:
            record(purpose, usage)

    if isinstance(materializer, DefaultActionMaterializer):
        materializer.current_task_instruction = instruction
        # Route every grounding/text-span/code/summary usage of this step
        # into the step's ``usages`` list (Agent-S records them via the
        # shared LLM backends; here the materializer callback is the only
        # channel). Any pre-existing sink is restored afterwards — even when
        # reflection, worker completion, or formatting raises (including
        # ``CancelledError``/``BaseException`` paths), so the outer ``try``
        # covers the whole step body below.
        previous_sink = materializer.usage_sink
        materializer.usage_sink = _record
    else:
        previous_sink = None

    try:
        generator_message = "" if state.turn_count > 0 else _INITIAL_SCREEN_TEXT

        if state.turn_count == 0:
            prompt_with_instructions = state.generator_system_template.replace(
                "TASK_DESCRIPTION", instruction
            )
            _messages.replace_system_prompt(
                state.generator_messages, prompt_with_instructions
            )

        reflection, reflection_thoughts = await _generate_reflection(
            state, completion, instruction, obs, _record, sleep
        )
        if reflection:
            generator_message += (
                "REFLECTION: You may use this reflection on the previous action "
                f"and overall trajectory:\n{reflection}\n"
            )

        notes = (
            materializer.notes
            if isinstance(materializer, DefaultActionMaterializer)
            else []
        )
        generator_message += f"\nCurrent Text Buffer = [{','.join(notes)}]\n"

        last_result = (
            materializer.last_code_agent_result
            if isinstance(materializer, DefaultActionMaterializer)
            else None
        )
        if last_result is not None:
            generator_message += build_code_agent_block(last_result)
            if isinstance(materializer, DefaultActionMaterializer):
                materializer.last_code_agent_result = None

        _messages.append_message(
            state.generator_messages,
            generator_message,
            image_content=obs["screenshot"],
            role="user",
        )

        async def _code_valid_probe(action: ParsedAction) -> Any:
            return await _materialize_parsed(
                materializer, action, obs, signature_binders
            )

        async def _single_action(response: str) -> tuple[bool, str]:
            return _formatting.single_action_check(response)

        async def _code_valid(response: str) -> tuple[bool, str]:
            return await _formatting.code_valid_check_async(response, _code_valid_probe)

        plan, _ = await call_llm_formatted(
            completion,
            state.generator_messages,
            [_single_action, _code_valid],
            purpose="worker",
            temperature=state.temperature,
            use_thinking=state.use_thinking,
            formatting_template=_prompts.FORMATTING_FEEDBACK_PROMPT,
            record_usage=_record,
            sleep=sleep,
        )
        state.worker_history.append(plan)
        state.generator_messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": plan}]}
        )

        plan_code = _parsing.parse_code_from_string(plan)
        try:
            assert plan_code, "Plan code should not be empty"
            parsed = parse_action_line(plan_code)
            materialized = await _materialize_parsed(
                materializer, parsed, obs, signature_binders
            )
            exec_code = materialized.exec_code
            terminal = materialized.terminal
            code_agent_output = materialized.code_agent_result
        except Exception:
            exec_code = "import time; time.sleep(1.333)"
            terminal = None
            code_agent_output = None

        if isinstance(materializer, DefaultActionMaterializer):
            pending = materializer.last_code_agent_result
        else:
            pending = code_agent_output
        output = StepOutput(
            plan=plan,
            plan_code=plan_code,
            exec_code=exec_code,
            reflection=reflection,
            reflection_thoughts=reflection_thoughts,
            code_agent_output=pending,
            terminal=terminal,
            usages=usages,
        )
        state.turn_count += 1
        state.screenshot_inputs.append(obs["screenshot"])
        flush_messages(state)
        return output
    finally:
        if isinstance(materializer, DefaultActionMaterializer):
            materializer.usage_sink = previous_sink


def default_signature_binders():
    """Binders for :class:`AgentActionSurface` (arity/default checking)."""
    import inspect as _inspect

    return {
        name: _inspect.signature(getattr(AgentActionSurface, name))
        for name in _worker_prompt.iter_agent_action_names(AgentActionSurface)
    }


def is_thinking_model(model: str) -> bool:
    return _grounding.wants_thinking(model)
