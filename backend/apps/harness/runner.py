"""Agentic loop for the harness (M4).

:func:`HarnessRunner.run` drives one agent turn: it composes the system
prompt, calls the provider with permission-filtered tool schemas,
streams deltas via an ``emit`` callback, gates ``ask`` tools through an
``on_permission`` callback, executes approved tools, and repeats until
a text-only answer, the step budget, an abort, or a doom-loop guard.

Permission ``ask`` never hangs: the ``on_permission`` wait is wrapped
in ``asyncio.wait_for`` with a configurable timeout and auto-denies on
timeout. Cancellation (``asyncio.CancelledError``) is re-raised after
emitting an ``aborted`` event so callers still observe cancellation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
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
    COMPACTION_TOKEN_THRESHOLD,
    apply_compaction_summary,
    build_compaction_prompt,
    should_compact,
)
from .computeruse_loop import (
    ComputerUseLoopState,
    FRESH_DESKTOP_TEXT,
    INITIAL_DESKTOP_TEXT,
    append_video_to_output,
    default_recording_path,
    sanitize_run_id,
)
from .images import hydrate_workspace_images
from .permissions.evaluator import ASK, DENY, PermissionEvaluator
from .prompts.composer import compose_system_prompt
from .providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from .tools.base import ToolContext, ToolRegistry

log = structlog.get_logger(__name__)

#: Default per-step tool schema source: deny-tools are never offered.
DEFAULT_MAX_STEPS = 20

#: Default subagent nesting limit (M5): depth 0 is the top-level turn,
#: each ``task`` child runs at depth+1. ``task`` is withheld at
#: ``depth >= max_depth``; ``todowrite`` is withheld from any child.
DEFAULT_MAX_DEPTH = 1

#: Default wait for the ``on_permission`` callback before auto-deny.
DEFAULT_PERMISSION_TIMEOUT = 120.0

#: Default wait for the ``on_question`` callback before auto-timeout.
DEFAULT_QUESTION_TIMEOUT = 600.0

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
    permission_timeout: float = DEFAULT_PERMISSION_TIMEOUT
    question_timeout: float = DEFAULT_QUESTION_TIMEOUT
    auto_approve: bool = False
    on_permission: PermissionCallback | None = None
    on_question: QuestionCallback | None = None
    run_subagent: Any | None = None
    depth: int = 0
    max_depth: int = DEFAULT_MAX_DEPTH
    session_tokens: dict[str, int] = field(default_factory=dict)
    compaction_threshold: int = COMPACTION_TOKEN_THRESHOLD


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
            log.warning(
                "emit_failed", error=str(exc), event_type=event.get("type")
            )

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
        ``auto_approve``) and on callback timeout — the loop never hangs.
        """
        key = "doom_loop" if doom_loop else "permission"
        if opts.auto_approve:
            log.info("permission_auto_approved", tool=tool_name, action=action)
            return True
        callback = opts.on_permission
        if callback is None:
            log.info("permission_auto_denied_no_callback", tool=tool_name)
            return False
        try:
            response = await asyncio.wait_for(
                callback(
                    tool=tool_name,
                    action=action,
                    title=title,
                    call_id=call_id,
                    key=key,
                ),
                timeout=opts.permission_timeout,
            )
        except asyncio.TimeoutError:
            log.warning("permission_timeout_auto_deny", tool=tool_name)
            return False
        normalized = str(response or "").strip().lower()
        return normalized in ("once", "always", "allow", "approved", "yes")

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
        max_steps = options.max_steps or agent.steps or DEFAULT_MAX_STEPS
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
        max_steps: int,
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
            LLMMessage(role="user", content=prompt),
        ]
        if self.accessor is not None:
            last = messages[-1]
            if last.role == "user" and isinstance(last.content, str):
                hydrated = await hydrate_workspace_images(
                    last.content, self.accessor
                )
                messages[-1] = LLMMessage(
                    role=last.role,
                    content=hydrated,
                    tool_calls=last.tool_calls,
                    tool_call_id=last.tool_call_id,
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
        last_text = ""
        recent_calls: list[str] = []

        for step in range(1, max_steps + 1):
            messages = await self._maybe_compact_history(
                messages=messages,
                agent=agent,
                model=model,
                mode=mode,
                opts=opts,
            )
            await self._send({"type": "step_start", "step": step})
            text, calls, usage = await self._provider_step(
                model=model, messages=messages, schemas=schemas, step=step
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
            if not calls:
                last_text = text
                messages.append(LLMMessage(role="assistant", content=text))
                return RunResult(
                    output=text,
                    steps=step,
                    usage=total_usage,
                    cost=total_cost,
                    finish_reason="stop",
                )
            last_text = text
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
            for call in calls:
                fingerprint = (
                    f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                )
                recent_calls.append(fingerprint)
                doom_loop = (
                    len(recent_calls) >= DOOM_LOOP_REPEATS
                    and len(set(recent_calls[-DOOM_LOOP_REPEATS:])) == 1
                )
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
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=(
                                "Subagent depth limit reached; nested task "
                                "calls are not allowed."
                            ),
                            tool_call_id=call.call_id,
                        )
                    )
                    continue
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
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content="todowrite is not available to subagents.",
                            tool_call_id=call.call_id,
                        )
                    )
                    continue
                decision = self._decide(
                    agent, call.name, action, mode, doom_loop=doom_loop
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
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=f"Permission {reason}: {title}",
                            tool_call_id=call.call_id,
                        )
                    )
                    continue
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
                try:
                    ctx.call_id = call.call_id
                    result = await self.tools.execute(call.name, call.arguments, ctx)
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
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=f"Unknown tool '{call.name}'",
                            tool_call_id=call.call_id,
                        )
                    )
                    continue
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
                    messages.append(
                        LLMMessage(
                            role="tool",
                            content=f"Tool '{call.name}' failed: {exc}",
                            tool_call_id=call.call_id,
                        )
                    )
                    continue
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
                if (call.name or "").strip().lower() == "todowrite":
                    await self._send(
                        {
                            "type": "todo_updated",
                            "step": step,
                            "call_id": call.call_id,
                            "todos": await _todos_payload(ctx),
                        }
                    )
                messages.append(
                    LLMMessage(
                        role="tool",
                        content=result.output,
                        tool_call_id=call.call_id,
                    )
                )

        summary = (
            f"{last_text}\n\n[Stopped after {max_steps} steps: step budget "
            "exhausted. Summarize progress and continue in a follow-up run.]"
            if last_text
            else (
                f"[Stopped after {max_steps} steps: step budget exhausted "
                "without a final answer.]"
            )
        )
        return RunResult(
            output=summary,
            steps=max_steps,
            usage=total_usage,
            cost=total_cost,
            finish_reason="max_steps",
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
        max_steps: int,
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
            hydrated = await hydrate_workspace_images(user_prompt.content, self.accessor)
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
        last_text = ""
        recent_calls: list[str] = []
        record_started = False
        recording_path = default_recording_path(run_id)

        try:
            await ctx.accessor.desktop_action("ensure")
            try:
                start = await ctx.accessor.desktop_action(
                    "record_start", {"run_id": run_id}
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Computer-use record_start failed: {exc}"
                ) from exc
            if not start.get("ok"):
                raise RuntimeError(
                    f"Computer-use record_start failed: {start!r}"
                )
            record_started = True
            recording_path = str(
                start.get("path") or default_recording_path(run_id)
            )
            initial = await self.tools.execute("view_screen", {}, ctx)
            if not initial.image_jpeg:
                raise RuntimeError(
                    "Computer-use initial desktop screenshot did not return image data."
                )
            cu_state.set_screenshot(INITIAL_DESKTOP_TEXT, initial.image_jpeg)

            for step in range(1, max_steps + 1):
                await self._send({"type": "step_start", "step": step})
                provider_messages = cu_state.build_provider_messages()
                text, calls, usage = await self._provider_step(
                    model=model,
                    messages=provider_messages,
                    schemas=schemas,
                    step=step,
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
                if not calls:
                    last_text = text
                    return self._computeruse_result(
                        output=append_video_to_output(text, run_id),
                        steps=step,
                        usage=total_usage,
                        cost=total_cost,
                        finish_reason="stop",
                        recording_path=recording_path,
                    )
                last_text = text
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
                for call in calls:
                    fingerprint = (
                        f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                    )
                    recent_calls.append(fingerprint)
                    doom_loop = (
                        len(recent_calls) >= DOOM_LOOP_REPEATS
                        and len(set(recent_calls[-DOOM_LOOP_REPEATS:])) == 1
                    )
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
                        cu_state.round_messages.append(
                            LLMMessage(
                                role="tool",
                                content=(
                                    "Subagent depth limit reached; nested task "
                                    "calls are not allowed."
                                ),
                                tool_call_id=call.call_id,
                            )
                        )
                        continue
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
                        cu_state.round_messages.append(
                            LLMMessage(
                                role="tool",
                                content="todowrite is not available to subagents.",
                                tool_call_id=call.call_id,
                            )
                        )
                        continue
                    decision = self._decide(
                        agent, call.name, action, mode, doom_loop=doom_loop
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
                        cu_state.round_messages.append(
                            LLMMessage(
                                role="tool",
                                content=f"Permission {reason}: {title}",
                                tool_call_id=call.call_id,
                            )
                        )
                        continue
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
                    try:
                        ctx.call_id = call.call_id
                        result = await self.tools.execute(
                            call.name, call.arguments, ctx
                        )
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
                        cu_state.round_messages.append(
                            LLMMessage(
                                role="tool",
                                content=f"Unknown tool '{call.name}'",
                                tool_call_id=call.call_id,
                            )
                        )
                        continue
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
                        cu_state.round_messages.append(
                            LLMMessage(
                                role="tool",
                                content=f"Tool '{call.name}' failed: {exc}",
                                tool_call_id=call.call_id,
                            )
                        )
                        continue
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
                                "title": (
                                    f"Patch {result.metadata.get('path', call.name)}"
                                ),
                                "path": result.metadata.get("path", ""),
                                "unified_diff": result.metadata.get(
                                    "unified_diff", ""
                                ),
                                "old_content": result.metadata.get("old_content", ""),
                                "new_content": result.metadata.get("new_content", ""),
                            }
                        )
                    if (call.name or "").strip().lower() == "todowrite":
                        await self._send(
                            {
                                "type": "todo_updated",
                                "step": step,
                                "call_id": call.call_id,
                                "todos": await _todos_payload(ctx),
                            }
                        )
                    cu_state.round_messages.append(
                        LLMMessage(
                            role="tool",
                            content=result.output,
                            tool_call_id=call.call_id,
                        )
                    )
                    if result.image_jpeg:
                        cu_state.set_screenshot(
                            FRESH_DESKTOP_TEXT, result.image_jpeg
                        )
            summary = (
                f"{last_text}\n\n[Stopped after {max_steps} steps: step budget "
                "exhausted. Summarize progress and continue in a follow-up run.]"
                if last_text
                else (
                    f"[Stopped after {max_steps} steps: step budget exhausted "
                    "without a final answer.]"
                )
            )
            return self._computeruse_result(
                output=append_video_to_output(summary, run_id),
                steps=max_steps,
                usage=total_usage,
                cost=total_cost,
                finish_reason="max_steps",
                recording_path=recording_path,
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

    async def _provider_step(
        self,
        *,
        model: str,
        messages: list[LLMMessage],
        schemas: list[ToolSchema],
        step: int,
    ) -> tuple[str, list[_PendingToolCall], Usage]:
        """Stream one provider step and accumulate text/tool calls/usage."""
        fragments: dict[int, dict[str, Any]] = {}
        text_parts: list[str] = []
        usage = Usage()
        async for delta in self.provider.chat_stream(
            model, messages, schemas, self.chat_options
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
    ) -> list[LLMMessage]:
        """Summarize oversized history via the hidden compaction agent."""
        threshold = opts.compaction_threshold or COMPACTION_TOKEN_THRESHOLD
        if not should_compact(
            messages,
            session_tokens=opts.session_tokens,
            threshold=threshold,
        ):
            return messages
        if not opts.small_model:
            return messages
        prompt = build_compaction_prompt(messages)
        if not prompt.strip():
            return messages
        try:
            child = HarnessRunner(
                provider=self.provider,
                tools=self.tools,
                evaluator=self.evaluator,
                accessor=self.accessor,
                emit=self._emit,
                chat_options=self.chat_options,
            )
            result = await child.run(
                prompt,
                "compaction",
                opts.small_model,
                mode,
                RunOptions(
                    auto_approve=True,
                    max_steps=1,
                    history=[],
                ),
            )
            summary = (result.output or "").strip()
            if not summary:
                return messages
            compacted = apply_compaction_summary(messages, summary)
            await self._send(
                {
                    "type": "compaction",
                    "summary": summary[:2000],
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

    async def desktop_action(
        self, action, args=None, timeout=None
    ):  # type: ignore[no-untyped-def]
        """Raise (no workspace connected)."""
        raise RuntimeError("No workspace accessor configured")


#: Backwards-friendly alias (brief allows ``AgentRunner``).
AgentRunner = HarnessRunner
