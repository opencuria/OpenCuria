"""Code-agent state machine (budget, DONE/FAIL, summary) without subprocesses.

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Reference (``gui_agents/s3/agents/code_agent.py``):

- budget loop ``while step_count < budget``; each turn calls the LLM at
  temperature 1, raises ``RuntimeError`` on empty responses, splits
  thoughts/answer, appends ``{"step", "action", "thoughts"}`` to history,
  checks ``action.upper().strip() == "DONE"/"FAIL"`` *before* executing code,
  executes ``python``/``bash`` blocks (``run_bash_script(code, timeout=30)``),
  records ``{"status": "skipped", ...}`` when no code block is present, then
  appends the assistant response + formatted result and increments;
- budget exhaustion yields
  ``completion_reason = f"BUDGET_EXHAUSTED_AFTER_{step_count}_STEPS"``
  (note: ``step_count`` equals the budget in that path);
- ``steps_executed`` is ``step_count`` — which excludes the terminal DONE/FAIL
  turn (quirk preserved: history contains the terminal step but the counter
  does not);
- the summary call builds ``"Task: ...\\n\\nExecution Steps:\\n"`` plus per
  step ``"\\nStep N:\\n[Thoughts...\\n]Code: ...\\n"`` and the fixed
  instruction block (``<150 words``, factual), sent with the
  ``CODE_SUMMARY_AGENT_PROMPT`` system prompt at temperature 1; empty history
  short-circuits to ``"No actions were executed."``.

Execution goes through the injected async
:class:`CodeExecutionPort <apps.harness.agent_s.ports.CodeExecutionPort>`;
"no code block" results use the same ``format_result`` path as Agent-S (which
only reads ``status``/``output``/``error`` and therefore renders a bare
``Step N Result:\\nStatus: skipped\\nReturn Code: -1\\n`` block).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import messages as _messages
from . import parsing as _parsing
from . import prompts as _prompts
from .llm import call_llm_safe
from .ports import CodeExecutionPort, CompletionPort, Usage


def extract_code_block(action: str) -> tuple[str | None, str | None]:
    """Extract ``(code_type, code)`` exactly like Agent-S.

    Checks `````python`` first, then `````bash``, then bare ````` —
    splitting on the *second* ````` occurrence. Returns ``(None, None)``
    when no fence is present or the block is empty.
    """
    if "```python" in action:
        code_type = "python"
        code = action.split("```python")[1].split("```")[0].strip()
    elif "```bash" in action:
        code_type = "bash"
        code = action.split("```bash")[1].split("```")[0].strip()
    elif "```" in action:
        code_type = None
        code = action.split("```")[1].split("```")[0].strip()
    else:
        code_type = None
        code = None
    return code_type, code


async def execute_code(
    code_type: str | None, code: str | None, executor: CodeExecutionPort
) -> dict[str, Any]:
    """Execute via the injected port (unknown types -> error dict)."""
    if code_type == "bash":
        assert code is not None
        return await executor.run_bash(code, timeout=30)
    if code_type == "python":
        assert code is not None
        return await executor.run_python(code)
    return {"status": "error", "error": f"Unknown code type: {code_type}"}


def format_result(result: dict[str, Any] | None, step_count: int) -> str:
    """Format an execution result (exact Agent-S text)."""
    if not result:
        return (
            f"\nStep {step_count + 1} Error:\n"
            "Error: No result returned from execution\n"
        )
    status = result.get("status", "unknown")
    return_code = result.get("returncode", result.get("return_code", -1))
    if "returncode" in result:
        output = result.get("output", "")
        error = result.get("error", "")
    else:
        output = result.get("output", "")
        error = result.get("error", "")
    result_text = f"Step {step_count + 1} Result:\n"
    result_text += f"Status: {status}\n"
    result_text += f"Return Code: {return_code}\n"
    if output:
        result_text += f"Output:\n{output}\n"
    if error:
        result_text += f"Error:\n{error}\n"
    return result_text


_SUMMARY_JUDGMENT_LINE = (
    "Do not make judgments about success or failure. "
    "Simply describe what was attempted and what resulted."
)


def build_summary_prompt(
    execution_history: list[dict[str, Any]], task_instruction: str
) -> str:
    execution_context = f"Task: {task_instruction}\n\nExecution Steps:\n"
    for step in execution_history:
        step_num = step["step"]
        thoughts = step.get("thoughts", "")
        action = step.get("action", "")
        execution_context += f"\nStep {step_num}:\n"
        if thoughts:
            execution_context += f"Thoughts: {thoughts}\n"
        execution_context += f"Code: {action}\n"
    return f"""
{execution_context}

Please provide a concise summary of the code execution session. Focus on:

1. The code logic implemented at each step
2. The outputs and results produced by each code execution
3. The progression of the solution approach

{_SUMMARY_JUDGMENT_LINE}

Keep the summary under 150 words and use clear, factual language.
"""


@dataclass
class CodeAgentResult:
    task_instruction: str
    completion_reason: str
    summary: str
    execution_history: list[dict[str, Any]] = field(default_factory=list)
    steps_executed: int = 0
    budget: int = 20

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_instruction": self.task_instruction,
            "completion_reason": self.completion_reason,
            "summary": self.summary,
            "execution_history": list(self.execution_history),
            "steps_executed": self.steps_executed,
            "budget": self.budget,
        }


async def run_code_agent(
    completion: CompletionPort,
    executor: CodeExecutionPort,
    task_instruction: str,
    screenshot: bytes | None,
    *,
    budget: int = 20,
    record_usage=None,
    sleep=None,
) -> tuple[CodeAgentResult, list[Usage]]:
    """Run the code-agent loop; returns ``(result, usages)``."""
    wire: list[dict[str, Any]] = [_messages.system_message(_prompts.CODE_AGENT_PROMPT)]
    context = f"Task: {task_instruction}\n\nCurrent screenshot is provided for context."
    _messages.append_message(
        wire, context, image_content=screenshot or b"", role="user"
    )

    usages: list[Usage] = []
    step_count = 0
    execution_history: list[dict[str, Any]] = []
    completion_reason: str | None = None

    while step_count < budget:
        text, call_usages = await call_llm_safe(
            completion,
            wire,
            purpose="code",
            temperature=1.0,
            use_thinking=False,
            record_usage=record_usage,
            sleep=sleep,
        )
        usages.extend(call_usages)
        if not text or text.strip() == "":
            raise RuntimeError(f"Step {step_count + 1}: LLM returned empty response")
        action, thoughts = _parsing.split_thinking_response(text)
        execution_history.append(
            {"step": step_count + 1, "action": action, "thoughts": thoughts}
        )
        action_upper = action.upper().strip()
        if action_upper == "DONE":
            completion_reason = "DONE"
            break
        if action_upper == "FAIL":
            completion_reason = "FAIL"
            break
        code_type, code = extract_code_block(action)
        if code:
            try:
                result = await execute_code(code_type, code, executor)
            except Exception as exc:  # pragma: no cover - port contract
                result = {"status": "error", "error": str(exc)}
        else:
            result = {"status": "skipped", "message": "No code block found"}
        wire.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        wire.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": format_result(result, step_count)}
                ],
            }
        )
        step_count += 1

    if completion_reason is None:
        completion_reason = f"BUDGET_EXHAUSTED_AFTER_{step_count}_STEPS"

    summary, summary_usages = await generate_summary(
        completion,
        execution_history,
        task_instruction,
        record_usage=record_usage,
        sleep=sleep,
    )
    usages.extend(summary_usages)
    return (
        CodeAgentResult(
            task_instruction=task_instruction,
            completion_reason=completion_reason,
            summary=summary,
            execution_history=execution_history,
            steps_executed=step_count,
            budget=budget,
        ),
        usages,
    )


async def generate_summary(
    completion: CompletionPort,
    execution_history: list[dict[str, Any]],
    task_instruction: str,
    *,
    record_usage=None,
    sleep=None,
) -> tuple[str, list[Usage]]:
    """Generate the post-run summary (temperature 1, summary system prompt)."""
    if not execution_history:
        return "No actions were executed.", []
    wire: list[dict[str, Any]] = [
        _messages.system_message(_prompts.CODE_SUMMARY_AGENT_PROMPT)
    ]
    wire.append(
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": build_summary_prompt(execution_history, task_instruction),
                }
            ],
        }
    )
    try:
        text, usages = await call_llm_safe(
            completion,
            wire,
            purpose="code_summary",
            temperature=1.0,
            use_thinking=False,
            record_usage=record_usage,
            sleep=sleep,
        )
    except Exception as exc:
        return f"Summary generation failed: {exc}", []
    if not text or text.strip() == "":
        return "Summary generation failed - no response from LLM", usages
    return text, usages
