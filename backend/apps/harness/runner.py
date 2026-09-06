"""Agentic loop for the harness (M4).

:func:`HarnessRunner.run` drives one agent turn: it composes the system
prompt, calls the provider with permission-filtered tool schemas,
streams deltas via an ``emit`` callback, gates ``ask`` tools through an
``on_permission`` callback, executes approved tools, and repeats until
a text-only answer, an optional last step, an abort, or a doom-loop
guard.

Permission ``ask`` and question tools wait until the user resolves them
or the run is aborted. An optional timeout still auto-denies (permissions)
or raises ``ToolError`` (questions) when tests set one. Cancellation
(``asyncio.CancelledError``) is re-raised after emitting an ``aborted``
event so callers still observe cancellation.

Independent tool calls in one step run concurrently. Results are fed
back to the model in the original emitted call order. A single tool
failure does not cancel its siblings.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from .access.base import HARNESS_WORKSPACE_ROOT, WorkspaceAccessor
from .agents.definitions import (
    SMALL_MODEL,
    AgentDefinition,
    get_agent,
    subagent_descriptions,
)
from .compaction import (
    CONTEXT_OVERFLOW_COMPACTION_ERROR,
    ModelLimits,
    apply_compaction,
    apply_overflow_replay,
    build_compaction_prompt,
    ensure_current_user,
    find_previous_summary,
    is_context_overflow_error,
    is_overflow,
    select,
)
from .computeruse_loop import (
    FRESH_DESKTOP_TEXT,
    INITIAL_DESKTOP_TEXT,
    ComputerUseLoopState,
    append_video_to_output,
    default_recording_path,
    sanitize_run_id,
)
from .images import hydrate_workspace_images
from .max_steps import MAX_STEPS_PROMPT, MAX_STEPS_TOOL_ERROR
from .permissions.evaluator import ASK, DENY, PermissionEvaluator
from .prompts.composer import compose_system_prompt
from .provider_retry import (
    RETRY_MAX_RETRIES,
    is_retryable_provider_error,
    retry_delay,
)
from .providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from .tools.base import ToolContext, ToolRegistry, ToolResult

log = structlog.get_logger(__name__)

#: Default subagent nesting limit (M5): depth 0 is the top-level turn,
#: each ``task`` child runs at depth+1. ``task`` is withheld at
#: ``depth >= max_depth``; ``todowrite`` is withheld from any child.
DEFAULT_MAX_DEPTH = 1

#: How many consecutive identical tool+input calls trigger doom-loop ask.
DOOM_LOOP_REPEATS = 3

EmitCallback = Callable[[dict[str, Any]], Awaitable[None]]
PermissionCallback = Callable[..., Awaitable[str]]
QuestionCallback = Callable[..., Awaitable[list[Any]]]


@dataclass
class RunOptions:
    """Per-run settings for :meth:`HarnessRunner.run`."""

    max_steps: int | None = None
    history: list[LLMMessage] = field(default_factory=list)
    session_id: str = ""
    workspace_id: str = ""
    organization_id: str = ""
    cwd: str = HARNESS_WORKSPACE_ROOT
    small_model: str = ""
    skills: list[str] = field(default_factory=list)
    permission_timeout: float | None = None
    question_timeout: float | None = None
    auto_approve: bool = False
    on_permission: PermissionCallback | None = None
    on_question: QuestionCallback | None = None
    run_subagent: Any | None = None
    depth: int = 0
    max_depth: int = DEFAULT_MAX_DEPTH
    context_length: int = 0
    max_output_tokens: int = 0
    auto_compact: bool = True
    current_user_message_id: str = ""
    last_step_prompt_tokens: int = 0
    last_step_completion_tokens: int = 0
    last_step_total_tokens: int = 0


@dataclass
class RunResult:
    """Outcome of one :meth:`HarnessRunner.run` call."""

    output: str
    steps: int
    usage: Usage
    cost: float
    finish_reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _PendingToolCall:
    """One accumulated tool call for the current step."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""


@dataclass
class _ToolCallOutcome:
    """Result of dispatching one tool call in a step."""

    message: LLMMessage
    result: ToolResult | None = None


def _action_for_tool(tool_name: str, args: dict[str, Any]) -> str:
    """Derive the permission action string for a tool invocation."""
    key = (tool_name or "").strip().lower()
    if key == "bash":
        return str(args.get("command", ""))
    if key in ("read", "edit", "write", "list"):
        return str(args.get("path", ""))
    if key in ("glob", "grep"):
        path = str(args.get("path", "") or "")
        pattern = str(args.get("pattern", "") or "")
        return path or pattern
    if key == "webfetch":
        return str(args.get("url", ""))
    if key == "task":
        return str(args.get("description", ""))
    return ""


def _combine_decisions(*decisions: str) -> str:
    """Combine decisions with deny > ask > allow precedence."""
    if DENY in decisions:
        return DENY
    if ASK in decisions:
        return ASK
    return "allow"


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """Parse raw tool arguments (JSON string or dict) into a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_last_step(step: int, max_steps: int | None) -> bool:
    """Return True when *step* is the optional configured last step."""
    return max_steps is not None and step >= max_steps


async def _todos_payload(ctx: ToolContext) -> list[dict[str, Any]]:
    """Best-effort todo list for the ``todo_updated`` event."""
    try:
        repository = None
        if ctx.registry is not None:
            try:
                tool = ctx.registry.get("todowrite")
            except KeyError:
                tool = None
            repository = getattr(tool, "_repository", None)
        if repository is None:
            from .tools.todos import repository_for_session

            repository = repository_for_session(ctx.session_id)
        stored = await sync_to_async(repository.list)(ctx.session_id)
        return [
            {
                "content": item.content,
                "status": item.status,
                "priority": item.priority,
                "order": item.order,
            }
            for item in stored.items
        ]
    except Exception:  # pragma: no cover - event must never break loop
        return []


class HarnessRunner:
    """Agentic loop: provider + tools + permissions + streaming events."""

    def __init__(
        self,
        *,
        provider: ProviderAdapter,
        tools: ToolRegistry,
        evaluator: PermissionEvaluator | None = None,
        accessor: WorkspaceAccessor | None = None,
        emit: EmitCallback | None = None,
        chat_options: ChatOptions | None = None,
    ) -> None:
        """Create a runner.

        Args:
            provider: LLM provider adapter for chat streaming.
            tools: Registry of executable tools.
            evaluator: Base (global/org) permission evaluator.
            accessor: Workspace accessor for tools and context files.
            emit: Async callback receiving streaming event dicts.
            chat_options: Optional per-request provider settings.
        """
        self.provider = provider
        self.tools = tools
        self.evaluator = evaluator or PermissionEvaluator()
        self.accessor = accessor
        self.chat_options = chat_options or ChatOptions()
        self._emit = emit or self._noop_emit

    @staticmethod
    async def _noop_emit(event: dict[str, Any]) -> None:
        """Drop events when no emitter was provided."""
        return None

    async def _send(self, event: dict[str, Any]) -> None:
        """Emit *event* without breaking the loop on emitter errors."""
        try:
            await self._emit(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("emit_failed", error=str(exc), event_type=event.get("type"))

    def _filtered_schemas(
        self, agent: AgentDefinition, mode: str, *, depth: int, max_depth: int
    ) -> list[ToolSchema]:
        """Return tool schemas excluding permission-denied tools.

        Depth filtering (M5): ``task`` is withheld when
        ``depth >= max_depth`` and ``todowrite`` is withheld from any
        child run (``depth > 0``), mirroring OpenCode where subagents
        get no todowrite. Filtering happens before permission checks
        so nested tools never reach the provider.
        """
        agent_eval = PermissionEvaluator(agent_rules=dict(agent.permissions or {}))
        schemas: list[ToolSchema] = []
        for tool in self.tools.list():
            key = tool.permission_key or tool.name
            tool_key = (tool.name or "").strip().lower()
            if tool_key == "task" and depth >= max_depth:
                continue
            if tool_key == "todowrite" and depth > 0:
                continue
            decision = _combine_decisions(
                self.evaluator.evaluate(key, "", mode=mode),
                agent_eval.evaluate(key, "", mode=mode),
            )
            if decision == DENY:
                continue
            schemas.append(
                ToolSchema(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.args_schema.model_json_schema(),
                )
            )
        return schemas

    def _decide(
        self,
        agent: AgentDefinition,
        tool_name: str,
        action: str,
        mode: str,
        *,
        doom_loop: bool = False,
    ) -> str:
        """Combine base and agent permission layers (deny wins).

        The permission *key* (not the tool name) is evaluated: ``write``
        and ``list`` share keys with ``edit``/``read`` (see tools), so
        key-level rules apply to them as well.
        """
        key = self._permission_key(tool_name)
        agent_eval = PermissionEvaluator(agent_rules=dict(agent.permissions or {}))
        if doom_loop:
            return _combine_decisions(
                self.evaluator.evaluate(key, action, mode=mode, doom_loop=True),
                agent_eval.evaluate(key, action, mode=mode, doom_loop=True),
            )
        return _combine_decisions(
            self.evaluator.evaluate(key, action, mode=mode),
            agent_eval.evaluate(key, action, mode=mode),
        )

    def _permission_key(self, tool_name: str) -> str:
        """Return the permission key for *tool_name* (falls back to name)."""
        try:
            return self.tools.get(tool_name).permission_key or tool_name
        except KeyError:
            return tool_name

    async def _resolve_ask(
        self,
        *,
        tool_name: str,
        action: str,
        title: str,
        call_id: str,
        step: int,
        doom_loop: bool,
        opts: RunOptions,
    ) -> bool:
        """Resolve an ``ask`` gate via ``on_permission`` (True = approved).

        Auto-denies when no callback is configured (unless
        ``auto_approve``). An optional positive ``permission_timeout``
        still auto-denies; otherwise the loop waits until resolve or abort.
        """
        key = "doom_loop" if doom_loop else "permission"
        if opts.auto_approve:
            log.info("permission_auto_approved", tool=tool_name, action=action)
            return True
        callback = opts.on_permission
        if callback is None:
            log.info("permission_auto_denied_no_callback", tool=tool_name)
            return False
        log.info(
            "permission_ask_pending",
            tool=tool_name,
            action=action,
            session_id=opts.session_id,
        )
        pending = callback(
            tool=tool_name,
            action=action,
            title=title,
            call_id=call_id,
            key=key,
        )
        timeout = opts.permission_timeout
        try:
            if timeout is not None and timeout > 0:
                response = await asyncio.wait_for(pending, timeout=timeout)
            else:
                response = await pending
        except asyncio.TimeoutError:
            log.warning("permission_timeout_auto_deny", tool=tool_name)
            return False
        normalized = str(response or "").strip().lower()
        return normalized in ("once", "always", "allow", "approved", "yes")

    def _tool_error_outcome(
        self, call: _PendingToolCall, content: str
    ) -> _ToolCallOutcome:
        """Build a tool-role message for a rejected or failed call."""
        return _ToolCallOutcome(
            message=LLMMessage(
                role="tool",
                content=content,
                tool_call_id=call.call_id,
            )
        )

    async def _dispatch_tool_call(
        self,
        *,
        call: _PendingToolCall,
        ctx: ToolContext,
        agent: AgentDefinition,
        mode: str,
        step: int,
        depth: int,
        max_depth: int,
        doom_loop: bool,
        opts: RunOptions,
    ) -> _ToolCallOutcome:
        """Permission-gate and execute one tool call (safe to run concurrently)."""
        action = _action_for_tool(call.name, call.arguments)
        title = self._tool_title(call.name, call.arguments)
        tool_key = (call.name or "").strip().lower()
        if tool_key == "task" and depth >= max_depth:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": (
                        f"Subagent depth limit reached (depth={depth}, "
                        f"max_depth={max_depth})"
                    ),
                }
            )
            return self._tool_error_outcome(
                call,
                "Subagent depth limit reached; nested task calls are not allowed.",
            )
        if tool_key == "todowrite" and depth > 0:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": "todowrite is not available to subagents.",
                }
            )
            return self._tool_error_outcome(
                call, "todowrite is not available to subagents."
            )
        decision = self._decide(agent, call.name, action, mode, doom_loop=doom_loop)
        log.debug(
            "permission_decision",
            tool=call.name,
            action=action,
            decision=decision,
            agent=agent.name,
            session_id=opts.session_id,
        )
        approved = True
        if decision == DENY:
            approved = False
        elif decision == ASK or doom_loop:
            approved = await self._resolve_ask(
                tool_name=call.name,
                action=action,
                title=title,
                call_id=call.call_id,
                step=step,
                doom_loop=doom_loop,
                opts=opts,
            )
        if not approved:
            if doom_loop:
                reason = "doom-loop guard denied"
            else:
                reason = "denied by permissions"
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": f"Permission {reason}: {title}",
                }
            )
            return self._tool_error_outcome(call, f"Permission {reason}: {title}")
        await self._send(
            {
                "type": "tool_started",
                "step": step,
                "call_id": call.call_id,
                "tool": call.name,
                "title": title,
                "arguments": call.raw_arguments,
            }
        )
        call_ctx = replace(ctx, call_id=call.call_id)
        try:
            result = await self.tools.execute(call.name, call.arguments, call_ctx)
        except asyncio.CancelledError:
            raise
        except KeyError as exc:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": str(exc),
                }
            )
            return self._tool_error_outcome(call, f"Unknown tool '{call.name}'")
        except Exception as exc:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": str(exc),
                }
            )
            return self._tool_error_outcome(call, f"Tool '{call.name}' failed: {exc}")
        await self._send(
            {
                "type": "tool_completed",
                "step": step,
                "call_id": call.call_id,
                "tool": call.name,
                "output": result.output,
            }
        )
        if result.metadata.get("unified_diff"):
            await self._send(
                {
                    "type": "patch",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "title": f"Patch {result.metadata.get('path', call.name)}",
                    "path": result.metadata.get("path", ""),
                    "unified_diff": result.metadata.get("unified_diff", ""),
                    "old_content": result.metadata.get("old_content", ""),
                    "new_content": result.metadata.get("new_content", ""),
                }
            )
        if tool_key == "todowrite":
            await self._send(
                {
                    "type": "todo_updated",
                    "step": step,
                    "call_id": call.call_id,
                    "todos": await _todos_payload(call_ctx),
                }
            )
        return _ToolCallOutcome(
            message=LLMMessage(
                role="tool",
                content=result.output,
                tool_call_id=call.call_id,
            ),
            result=result,
        )

    async def _run_step_tools(
        self,
        *,
        calls: list[_PendingToolCall],
        recent_calls: list[str],
        ctx: ToolContext,
        agent: AgentDefinition,
        mode: str,
        step: int,
        depth: int,
        max_depth: int,
        opts: RunOptions,
    ) -> list[_ToolCallOutcome]:
        """Execute *calls* concurrently; return outcomes in original order."""
        doom_flags: list[bool] = []
        for call in calls:
            fingerprint = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
            recent_calls.append(fingerprint)
            doom_flags.append(
                len(recent_calls) >= DOOM_LOOP_REPEATS
                and len(set(recent_calls[-DOOM_LOOP_REPEATS:])) == 1
            )
        tasks = [
            asyncio.create_task(
                self._dispatch_tool_call(
                    call=call,
                    ctx=ctx,
                    agent=agent,
                    mode=mode,
                    step=step,
                    depth=depth,
                    max_depth=max_depth,
                    doom_loop=doom_loop,
                    opts=opts,
                ),
                name=f"harness-tool-{call.call_id}",
            )
            for call, doom_loop in zip(calls, doom_flags)
        ]
        try:
            raw = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        outcomes: list[_ToolCallOutcome] = []
        for call, item in zip(calls, raw):
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, BaseException):
                log.warning(
                    "tool_dispatch_failed",
                    tool=call.name,
                    call_id=call.call_id,
                    error=str(item),
                )
                await self._send(
                    {
                        "type": "tool_error",
                        "step": step,
                        "call_id": call.call_id,
                        "tool": call.name,
                        "error": str(item),
                    }
                )
                outcomes.append(
                    self._tool_error_outcome(call, f"Tool '{call.name}' failed: {item}")
                )
                continue
            outcomes.append(item)
        return outcomes

    async def run(
        self,
        prompt: str,
        agent_name: str,
        model: str,
        mode: str = "build",
        opts: RunOptions | None = None,
    ) -> RunResult:
        """Run one agent turn and return the aggregated result.

        Args:
            prompt: User prompt for this turn.
            agent_name: Static agent definition name (unknown raises).
            model: Provider model identifier.
            mode: ``plan`` or ``build``.
            opts: Run settings (steps, history, permission callback).

        Returns:
            The final text, step count, summed usage/cost, and a finish
            reason (``stop`` | ``max_steps``).

        Raises:
            KeyError: For unknown agent names.
            ValueError: For invalid modes or empty prompts.
            asyncio.CancelledError: On abort (after emitting ``aborted``).
        """
        options = opts or RunOptions()
        if mode not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        agent = get_agent(agent_name)  # raises KeyError for unknown agents
        effective_model = model
        if agent.model_override == SMALL_MODEL and options.small_model:
            effective_model = options.small_model
        max_steps = options.max_steps or agent.steps
        max_depth = options.max_depth if options.max_depth > 0 else DEFAULT_MAX_DEPTH
        depth = max(0, options.depth)
        if depth > max_depth:
            raise ValueError(f"depth {depth} exceeds max_depth {max_depth}")
        cwd = options.cwd or HARNESS_WORKSPACE_ROOT

        try:
            return await self._run_inner(
                prompt=prompt,
                agent=agent,
                model=effective_model,
                mode=mode,
                opts=options,
                cwd=cwd,
                max_steps=max_steps,
                depth=depth,
                max_depth=max_depth,
            )
        except asyncio.CancelledError:
            await self._send({"type": "aborted", "reason": "cancelled"})
            raise

    async def _run_inner(
        self,
        *,
        prompt: str,
        agent: AgentDefinition,
        model: str,
        mode: str,
        opts: RunOptions,
        cwd: str,
        max_steps: int | None,
        depth: int = 0,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> RunResult:
        """Execute the step loop (cancellation handled by :meth:`run`)."""
        if agent.name == "computeruse":
            return await self._run_computeruse_inner(
                prompt=prompt,
                agent=agent,
                model=model,
                mode=mode,
                opts=opts,
                cwd=cwd,
                max_steps=max_steps,
                depth=depth,
                max_depth=max_depth,
            )
        schemas = self._filtered_schemas(agent, mode, depth=depth, max_depth=max_depth)
        composed = await compose_system_prompt(
            agent=agent,
            mode=mode,
            tools=schemas,
            subagents=subagent_descriptions(),
            accessor=self.accessor,
            cwd=cwd,
            skills=list(opts.skills or []),
        )
        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=composed.system),
            *list(opts.history or []),
            LLMMessage(
                role="user",
                content=prompt,
                message_id=opts.current_user_message_id or "",
            ),
        ]
        if self.accessor is not None:
            last = messages[-1]
            if last.role == "user" and isinstance(last.content, str):
                hydrated = await hydrate_workspace_images(last.content, self.accessor)
                messages[-1] = LLMMessage(
                    role=last.role,
                    content=hydrated,
                    tool_calls=last.tool_calls,
                    tool_call_id=last.tool_call_id,
                    message_id=last.message_id,
                )
        ctx = ToolContext(
            session_id=opts.session_id or "session",
            workspace_id=opts.workspace_id or "workspace",
            accessor=self.accessor
            or _MissingAccessor(workspace_id=opts.workspace_id or "workspace"),
            agent_name=agent.name,
            directory=cwd,
            depth=depth,
            max_depth=max_depth,
            model=model,
            parent_emit=self._emit,
            provider=self.provider,
            registry=self.tools,
            evaluator=self.evaluator,
            run_subagent=opts.run_subagent,
            on_question=opts.on_question,
            question_timeout=opts.question_timeout,
        )

        total_usage = Usage()
        total_cost = 0.0
        recent_calls: list[str] = []
        limits = ModelLimits(
            context_length=opts.context_length,
            max_output_tokens=opts.max_output_tokens,
        )
        last_step_prompt = opts.last_step_prompt_tokens
        last_step_completion = opts.last_step_completion_tokens
        last_step_total = opts.last_step_total_tokens

        step = 0
        while True:
            step += 1
            is_last = _is_last_step(step, max_steps)
            if step == 1 and is_overflow(
                prompt_tokens=last_step_prompt,
                completion_tokens=last_step_completion,
                total_tokens=last_step_total,
                limits=limits,
                auto=opts.auto_compact,
            ):
                messages = await self._maybe_compact_history(
                    messages=messages,
                    agent=agent,
                    model=model,
                    mode=mode,
                    opts=opts,
                    limits=limits,
                )
            await self._send({"type": "step_start", "step": step})
            (
                text,
                calls,
                usage,
                messages,
            ) = await self._provider_step_with_overflow_retry(
                model=model,
                messages=messages,
                schemas=schemas,
                step=step,
                agent=agent,
                mode=mode,
                opts=opts,
                is_last_step=is_last,
            )
            last_step_prompt = usage.prompt_tokens
            last_step_completion = usage.completion_tokens
            last_step_total = usage.total_tokens
            total_usage = total_usage.merge(usage)
            total_cost = total_usage.cost
            await self._send(
                {
                    "type": "step_finish",
                    "step": step,
                    "tokens": {
                        "prompt_tokens": usage.prompt_tokens,
                        "completion_tokens": usage.completion_tokens,
                        "total_tokens": usage.total_tokens,
                    },
                    "cost": usage.cost,
                }
            )
            if is_last:
                if calls:
                    await self._reject_last_step_tools(calls, step)
                return RunResult(
                    output=text,
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="max_steps",
                )
            if not calls:
                messages.append(LLMMessage(role="assistant", content=text))
                if is_overflow(
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    limits=limits,
                    auto=opts.auto_compact,
                ):
                    await self._maybe_compact_history(
                        messages=messages,
                        agent=agent,
                        model=model,
                        mode=mode,
                        opts=opts,
                        limits=limits,
                    )
                return RunResult(
                    output=text,
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="stop",
                )
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=text or None,
                    tool_calls=[
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "arguments": call.raw_arguments,
                        }
                        for call in calls
                    ],
                )
            )
            outcomes = await self._run_step_tools(
                calls=calls,
                recent_calls=recent_calls,
                ctx=ctx,
                agent=agent,
                mode=mode,
                step=step,
                depth=depth,
                max_depth=max_depth,
                opts=opts,
            )
            for outcome in outcomes:
                messages.append(outcome.message)
            if is_overflow(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                limits=limits,
                auto=opts.auto_compact,
            ):
                messages = await self._maybe_compact_history(
                    messages=messages,
                    agent=agent,
                    model=model,
                    mode=mode,
                    opts=opts,
                    limits=limits,
                )

    async def _run_computeruse_inner(
        self,
        *,
        prompt: str,
        agent: AgentDefinition,
        model: str,
        mode: str,
        opts: RunOptions,
        cwd: str,
        max_steps: int | None,
        depth: int = 0,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> RunResult:
        """Execute the computer-use loop with recording and image compaction."""
        schemas = self._filtered_schemas(agent, mode, depth=depth, max_depth=max_depth)
        composed = await compose_system_prompt(
            agent=agent,
            mode=mode,
            tools=schemas,
            subagents=subagent_descriptions(),
            accessor=self.accessor,
            cwd=cwd,
            skills=list(opts.skills or []),
        )
        user_prompt = LLMMessage(role="user", content=prompt)
        if self.accessor is not None and isinstance(user_prompt.content, str):
            hydrated = await hydrate_workspace_images(
                user_prompt.content, self.accessor
            )
            user_prompt = LLMMessage(role="user", content=hydrated)
        cu_state = ComputerUseLoopState(
            base_messages=[
                LLMMessage(role="system", content=composed.system),
                *list(opts.history or []),
                user_prompt,
            ]
        )
        ctx = ToolContext(
            session_id=opts.session_id or "session",
            workspace_id=opts.workspace_id or "workspace",
            accessor=self.accessor
            or _MissingAccessor(workspace_id=opts.workspace_id or "workspace"),
            agent_name=agent.name,
            directory=cwd,
            depth=depth,
            max_depth=max_depth,
            model=model,
            parent_emit=self._emit,
            provider=self.provider,
            registry=self.tools,
            evaluator=self.evaluator,
            run_subagent=opts.run_subagent,
            on_question=opts.on_question,
            question_timeout=opts.question_timeout,
        )
        run_id = sanitize_run_id(opts.session_id or "session")
        total_usage = Usage()
        total_cost = 0.0
        recent_calls: list[str] = []
        record_started = False
        held = False
        recording_path = default_recording_path(run_id)

        try:
            await ctx.accessor.desktop_action(
                "hold",
                {"kind": "computeruse", "run_id": run_id},
            )
            held = True
            try:
                start = await ctx.accessor.desktop_action(
                    "record_start", {"run_id": run_id}
                )
            except Exception as exc:
                raise RuntimeError(f"Computer-use record_start failed: {exc}") from exc
            if not start.get("ok"):
                raise RuntimeError(f"Computer-use record_start failed: {start!r}")
            record_started = True
            recording_path = str(start.get("path") or default_recording_path(run_id))
            initial = await self.tools.execute("view_screen", {}, ctx)
            if not initial.image_jpeg:
                raise RuntimeError(
                    "Computer-use initial desktop screenshot did not return image data."
                )
            cu_state.set_screenshot(INITIAL_DESKTOP_TEXT, initial.image_jpeg)

            step = 0
            while True:
                step += 1
                is_last = _is_last_step(step, max_steps)
                await self._send({"type": "step_start", "step": step})
                request_messages, request_schemas, request_opts = (
                    self._last_step_request(
                        cu_state.build_provider_messages(), schemas, is_last
                    )
                )
                text, calls, usage = await self._provider_step(
                    model=model,
                    messages=request_messages,
                    schemas=request_schemas,
                    step=step,
                    chat_options=request_opts,
                )
                total_usage = total_usage.merge(usage)
                total_cost = total_usage.cost
                await self._send(
                    {
                        "type": "step_finish",
                        "step": step,
                        "tokens": {
                            "prompt_tokens": usage.prompt_tokens,
                            "completion_tokens": usage.completion_tokens,
                            "total_tokens": usage.total_tokens,
                        },
                        "cost": usage.cost,
                    }
                )
                if is_last:
                    if calls:
                        await self._reject_last_step_tools(calls, step)
                    return self._computeruse_result(
                        output=append_video_to_output(text, run_id),
                        steps=step,
                        usage=total_usage,
                        cost=total_cost,
                        finish_reason="max_steps",
                        recording_path=recording_path,
                    )
                if not calls:
                    return self._computeruse_result(
                        output=append_video_to_output(text, run_id),
                        steps=step,
                        usage=total_usage,
                        cost=total_cost,
                        finish_reason="stop",
                        recording_path=recording_path,
                    )
                if cu_state.round_messages:
                    cu_state.flush_round_to_ledger()
                cu_state.round_messages.append(
                    LLMMessage(
                        role="assistant",
                        content=text or None,
                        tool_calls=[
                            {
                                "id": call.call_id,
                                "name": call.name,
                                "arguments": call.raw_arguments,
                            }
                            for call in calls
                        ],
                    )
                )
                outcomes = await self._run_step_tools(
                    calls=calls,
                    recent_calls=recent_calls,
                    ctx=ctx,
                    agent=agent,
                    mode=mode,
                    step=step,
                    depth=depth,
                    max_depth=max_depth,
                    opts=opts,
                )
                for outcome in outcomes:
                    cu_state.round_messages.append(outcome.message)
                    if outcome.result is not None and outcome.result.image_jpeg:
                        cu_state.set_screenshot(
                            FRESH_DESKTOP_TEXT, outcome.result.image_jpeg
                        )
        finally:
            if record_started:
                try:
                    stop = await ctx.accessor.desktop_action(
                        "record_stop", {"run_id": run_id}
                    )
                    if stop.get("path"):
                        recording_path = str(stop["path"])
                except Exception as exc:  # pragma: no cover - best effort
                    log.warning(
                        "computeruse_record_stop_failed",
                        run_id=run_id,
                        error=str(exc),
                    )
            if held:
                try:
                    await ctx.accessor.desktop_action(
                        "release",
                        {"kind": "computeruse", "run_id": run_id},
                    )
                except Exception as exc:  # pragma: no cover - best effort
                    log.warning(
                        "computeruse_desktop_release_failed",
                        run_id=run_id,
                        error=str(exc),
                    )

    @staticmethod
    def _computeruse_result(
        *,
        output: str,
        steps: int,
        usage: Usage,
        cost: float,
        finish_reason: str,
        recording_path: str,
    ) -> RunResult:
        """Build a :class:`RunResult` for a computer-use run."""
        return RunResult(
            output=output,
            steps=steps,
            usage=usage,
            cost=cost,
            finish_reason=finish_reason,
            metadata={"recording_path": recording_path},
        )

    def _last_step_request(
        self,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        is_last: bool,
    ) -> tuple[list[LLMMessage], list[ToolSchema], ChatOptions]:
        """Build provider inputs for a normal or last-step turn."""
        if not is_last:
            return messages, schemas, self.chat_options
        return (
            [*messages, LLMMessage(role="assistant", content=MAX_STEPS_PROMPT)],
            [],
            replace(self.chat_options, tool_choice="none"),
        )

    async def _reject_last_step_tools(
        self, calls: list[_PendingToolCall], step: int
    ) -> None:
        """Fail leaked last-step tool calls without executing them."""
        for call in calls:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "tool": call.name,
                    "error": MAX_STEPS_TOOL_ERROR,
                }
            )

    async def _provider_step_with_overflow_retry(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        agent: AgentDefinition,
        mode: str,
        opts: RunOptions,
        is_last_step: bool = False,
    ) -> tuple[str, list[_PendingToolCall], Usage, list[LLMMessage]]:
        """Run one provider step, compacting and retrying once on overflow errors."""
        request_messages, request_schemas, request_opts = self._last_step_request(
            messages, schemas, is_last_step
        )
        try:
            text, calls, usage = await self._provider_step_with_transient_retry(
                model=model,
                messages=request_messages,
                schemas=request_schemas,
                step=step,
                chat_options=request_opts,
            )
            return text, calls, usage, messages
        except Exception as exc:
            if not is_context_overflow_error(exc):
                raise
            compacted = await self._maybe_compact_history(
                messages=messages,
                agent=agent,
                model=model,
                mode=mode,
                opts=opts,
                limits=ModelLimits(
                    context_length=opts.context_length,
                    max_output_tokens=opts.max_output_tokens,
                ),
                overflow=True,
            )
            retry_messages, retry_schemas, retry_opts = self._last_step_request(
                compacted, schemas, is_last_step
            )
            try:
                text, calls, usage = await self._provider_step_with_transient_retry(
                    model=model,
                    messages=retry_messages,
                    schemas=retry_schemas,
                    step=step,
                    chat_options=retry_opts,
                )
            except Exception as retry_exc:
                if is_context_overflow_error(retry_exc):
                    raise RuntimeError(CONTEXT_OVERFLOW_COMPACTION_ERROR) from retry_exc
                raise
            return text, calls, usage, compacted

    async def _provider_step_with_transient_retry(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        chat_options: ChatOptions | None = None,
    ) -> tuple[str, list[_PendingToolCall], Usage]:
        """Run one provider step, retrying transient timeouts and 5xx errors."""
        attempt = 0
        while True:
            try:
                return await self._provider_step(
                    model=model,
                    messages=messages,
                    schemas=schemas,
                    step=step,
                    chat_options=chat_options,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if (
                    not is_retryable_provider_error(exc)
                    or attempt >= RETRY_MAX_RETRIES
                ):
                    raise
                attempt += 1
                delay = retry_delay(attempt)
                log.warning(
                    "provider_step_retry",
                    attempt=attempt,
                    delay_seconds=delay,
                    error=str(exc),
                )
                await asyncio.sleep(delay)

    async def _provider_step(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
        chat_options: ChatOptions | None = None,
    ) -> tuple[str, list[_PendingToolCall], Usage]:
        """Stream one provider step and accumulate text/tool calls/usage."""
        fragments: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        usage = Usage()
        async for delta in self.provider.chat_stream(
            model, messages, schemas, chat_options or self.chat_options
        ):
            if delta.usage is not None:
                usage = usage.merge(delta.usage)
            if delta.text:
                text_parts.append(delta.text)
                await self._send(
                    {
                        "type": "part_updated",
                        "step": step,
                        "delta": {"text": delta.text},
                    }
                )
            if delta.reasoning:
                await self._send(
                    {
                        "type": "part_updated",
                        "step": step,
                        "delta": {"reasoning": delta.reasoning},
                    }
                )
            for fragment in delta.tool_calls or ():
                index = int(fragment.get("index", 0) or 0)
                slot = fragments.setdefault(
                    index, {"id": "", "name": "", "arguments": ""}
                )
                if fragment.get("id"):
                    slot["id"] = fragment["id"]
                if fragment.get("name"):
                    slot["name"] = fragment["name"]
                args = fragment.get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args)
                if args:
                    slot["arguments"] += str(args)
        calls: list[_PendingToolCall] = []
        for index in sorted(fragments):
            slot = fragments[index]
            name = str(slot.get("name", "") or "")
            if not name:
                continue
            raw = str(slot.get("arguments", "") or "")
            calls.append(
                _PendingToolCall(
                    call_id=str(slot.get("id", "") or f"call-{step}-{index}"),
                    name=name,
                    arguments=_parse_arguments(raw),
                    raw_arguments=raw,
                )
            )
        return "".join(text_parts), calls, usage

    def _tool_title(self, tool_name: str, args: dict[str, Any]) -> str:
        """Best-effort human title for a tool invocation."""
        try:
            tool = self.tools.get(tool_name)
        except KeyError:
            return tool_name
        try:
            return tool.title(tool.coerce_args(args))
        except Exception:  # pragma: no cover - title must never break loop
            return tool_name

    async def _maybe_compact_history(
        self,
        *,
        messages: list[LLMMessage],
        agent: AgentDefinition,
        model: str,
        mode: str,
        opts: RunOptions,
        limits: ModelLimits,
        overflow: bool = False,
    ) -> list[LLMMessage]:
        """Summarize history via the hidden compaction agent."""
        if agent.name == "compaction":
            return messages
        selection = select(messages, limits)
        if not selection.head:
            return messages
        previous_summary = find_previous_summary(messages)
        prompt = build_compaction_prompt(
            selection.head,
            previous_summary=previous_summary,
        )
        if not prompt.strip():
            return messages
        try:
            child = HarnessRunner(
                provider=self.provider,
                tools=self.tools,
                evaluator=self.evaluator,
                accessor=self.accessor,
                emit=None,
                chat_options=self.chat_options,
            )
            result = await child.run(
                prompt,
                "compaction",
                model,
                mode,
                RunOptions(
                    auto_approve=True,
                    history=[],
                    auto_compact=False,
                    session_id=opts.session_id,
                    workspace_id=opts.workspace_id,
                    organization_id=opts.organization_id,
                ),
            )
            summary = (result.output or "").strip()
            if not summary:
                return messages
            compacted, tail_start_id = apply_compaction(messages, summary, limits)
            if overflow:
                compacted = apply_overflow_replay(messages, compacted)
            else:
                compacted = ensure_current_user(messages, compacted)
            await self._send(
                {
                    "type": "compaction",
                    "summary": summary[:32_768],
                    "auto": True,
                    "overflow": overflow,
                    "tail_start_id": tail_start_id,
                }
            )
            return compacted
        except Exception as exc:  # pragma: no cover - compaction must not abort
            log.warning("compaction_failed", error=str(exc))
            return messages


class _MissingAccessor(WorkspaceAccessor):
    """Fallback accessor that fails loudly when no accessor is wired."""

    async def exec_stream(
        self, command, workdir=HARNESS_WORKSPACE_ROOT, env=None, timeout=None
    ):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")
        yield  # pragma: no cover - keeps the method an iterator

    async def exec_wait(
        self, command, workdir=HARNESS_WORKSPACE_ROOT, env=None, timeout=None
    ):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def read_file(self, path, max_size=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def write_file(self, path, content, mode=0o644):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def list_dir(self, path):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def stat(self, path):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def desktop_action(self, action, args=None, timeout=None):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_start(self, command, workdir="/workspace", env=None, name=""):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_list(self):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_get(self, process_id):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")

    async def process_stop(self, process_id):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")


#: Backwards-friendly alias (brief allows ``AgentRunner``).
AgentRunner = HarnessRunner
