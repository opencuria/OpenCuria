"""Provider-independent permission gating and execution for registered tools."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog
from asgiref.sync import sync_to_async

from ..access.base import WorkspaceAccessor
from ..agents.definitions import AgentDefinition
from ..media.attachments import build_tool_message_content
from ..permissions.evaluator import ASK, DEFAULT_GLOBAL_RULES, DENY, PermissionEvaluator
from ..providers.base import LLMMessage, ToolSchema
from ..todos.service import TodoService
from .base import ToolContext, ToolError, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from ..engines.base import RunOptions

log = structlog.get_logger(__name__)
EXTERNAL_PATH_TOOLS = frozenset({"bash", "process_start"})
TOOL_ERROR_ARGS_SNIPPET = 500


@dataclass
class _PendingToolCall:
    """One accumulated tool call for the current step."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""
    queued_id: str = ""


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
    if key == "process_start":
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
    if key.startswith("mcp_"):
        # MCP tools never derive the action from args: raw call values
        # may carry user secrets and must never reach the permission
        # UI/DB. The module-level fallback is empty; the instance path
        # below substitutes the tool's ``original_name``.
        return ""
    return ""


def _action_for_registered_tool(tool: Any, tool_name: str) -> str:
    """Return the permission action for an already-registered tool.

    MCP tools report their original MCP tool name; everything else
    reports no action (never raw args).
    """
    original = str(getattr(tool, "original_name", "") or "").strip()
    if original and str(tool_name or "").strip().lower().startswith("mcp_"):
        return original
    return ""


def _detect_external_directory(tool_name: str, args: dict[str, Any]) -> bool:
    """Return True when a shell command touches paths outside /workspace."""
    key = (tool_name or "").strip().lower()
    if key not in EXTERNAL_PATH_TOOLS:
        return False
    from .shell import detect_external_directory

    return detect_external_directory(str(args.get("command", "") or ""))


def _combine_decisions(*decisions: str) -> str:
    """Combine decisions with deny > ask > allow precedence."""
    if DENY in decisions:
        return DENY
    if ASK in decisions:
        return ASK
    return "allow"


def _with_args(message: str, call: _PendingToolCall) -> str:
    """Append the raw tool arguments (truncated) to an error message."""
    raw = (call.raw_arguments or "").strip()
    if not raw:
        return message
    return f"{message} (args: {raw[:TOOL_ERROR_ARGS_SNIPPET]})"


async def _todos_payload(ctx: ToolContext) -> list[dict[str, Any]]:
    """Best-effort todo list for the ``todo_updated`` event."""
    try:
        stored = await sync_to_async(TodoService().list)(ctx.session_id)
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


class AuthorizedToolDispatcher:
    """Execute tools under current role, depth and global permission policy.

    Provider engines supply the registry, workspace and event sink; no model
    adapter or native inference loop is required to authorize an invocation.
    """

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        accessor: WorkspaceAccessor | None,
        emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        evaluator: PermissionEvaluator | None = None,
    ) -> None:
        """Configure provider-independent tool execution dependencies."""
        self._log = log
        self.tools = tools
        self.accessor = accessor
        self.evaluator = evaluator or PermissionEvaluator(
            global_rules=dict(DEFAULT_GLOBAL_RULES)
        )
        self._emit = emit or self._noop_emit

    @staticmethod
    async def _noop_emit(event: dict[str, Any]) -> None:
        """Drop events when no sink was provided."""

    async def _send(self, event: dict[str, Any]) -> None:
        """Send an execution event to the engine-owned sink."""
        await self._emit(event)

    def _permission_log_action(self, action: str) -> str:
        """Project actions for diagnostic logs; engines may redact secrets."""
        return action

    def _filtered_schemas(
        self, agent: AgentDefinition, mode: str, *, depth: int, max_depth: int
    ) -> list[ToolSchema]:
        """Return tool schemas excluding permission-denied tools.

        The run *mode* is forwarded to both evaluators so
        ``mode_rules`` slices apply here as well as in :meth:`_decide`
        (Phase 3 wiring decision; ``mode_rules=None`` is a no-op).

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
                    parameters=tool.parameters_schema(),
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
        external_directory: bool = False,
    ) -> str:
        """Combine base and agent permission layers (deny wins).

        The run *mode* is forwarded to both evaluators so per-mode
        ``mode_rules`` (when configured) participate in the merged
        decision (Phase 3 wiring decision). The permission *key* (not
        the tool name) is evaluated: ``write`` and ``list`` share keys
        with ``edit``/``read`` (see tools), so key-level rules apply
        to them as well. ``external_directory`` only overrides when an
        explicit reserved-key rule exists; otherwise normal tool rules
        apply (paths outside /workspace are allowed by default).
        """
        key = self._permission_key(tool_name)
        agent_eval = PermissionEvaluator(agent_rules=dict(agent.permissions or {}))
        if doom_loop:
            return _combine_decisions(
                self.evaluator.evaluate(key, action, mode=mode, doom_loop=True),
                agent_eval.evaluate(key, action, mode=mode, doom_loop=True),
            )
        if external_directory:
            return _combine_decisions(
                self.evaluator.evaluate(
                    key, action, mode=mode, external_directory=True
                ),
                agent_eval.evaluate(key, action, mode=mode, external_directory=True),
            )
        decision = _combine_decisions(
            self.evaluator.evaluate(key, action, mode=mode),
            agent_eval.evaluate(key, action, mode=mode),
        )
        try:
            execution_tool = getattr(self.tools.get(tool_name), "execution_tool", False)
        except KeyError:
            execution_tool = False
        if mode == "plan" and execution_tool:
            decision = _combine_decisions(decision, ASK)
        return decision

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

        Once-approved ``(tool, action)`` pairs cached on
        ``opts.once_approved`` return True without a callback. A fresh
        ``once``/``allow``/``yes`` response is cached; ``always`` flows
        through the allowlist/service untouched. Auto-denies when no
        callback is configured (unless ``auto_approve``). An optional
        positive ``permission_timeout`` still auto-denies; otherwise
        the loop waits until resolve or abort.
        """
        key = "doom_loop" if doom_loop else "permission"
        if (tool_name, action) in opts.once_approved:
            self._log.info("permission_once_cached", tool=tool_name, action=action)
            return True
        if opts.auto_approve:
            self._log.info("permission_auto_approved", tool=tool_name, action=action)
            return True
        callback = opts.on_permission
        if callback is None:
            self._log.info("permission_auto_denied_no_callback", tool=tool_name)
            return False
        self._log.info(
            "permission_ask_pending",
            tool=tool_name,
            action=self._permission_log_action(action),
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
            self._log.warning("permission_timeout_auto_deny", tool=tool_name)
            return False
        normalized = str(response or "").strip().lower()
        approved = normalized in ("once", "always", "allow", "approved", "yes")
        if approved and normalized != "always":
            opts.once_approved.add((tool_name, action))
        return approved

    def _tool_error_outcome(
        self, call: _PendingToolCall, content: str | list[dict[str, Any]]
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
        try:
            registered = self.tools.get(call.name)
        except KeyError:
            registered = None
        if registered is not None:
            registered_action = _action_for_registered_tool(registered, call.name)
            if registered_action:
                action = registered_action
        title = self._tool_title(call.name, call.arguments)
        tool_key = (call.name or "").strip().lower()
        if tool_key == "task" and depth >= max_depth:
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "queued_id": call.queued_id,
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
                    "queued_id": call.queued_id,
                    "tool": call.name,
                    "error": "todowrite is not available to subagents.",
                }
            )
            return self._tool_error_outcome(
                call, "todowrite is not available to subagents."
            )
        external = _detect_external_directory(call.name, call.arguments)
        decision = self._decide(
            agent,
            call.name,
            action,
            mode,
            doom_loop=doom_loop,
            external_directory=external,
        )
        self._log.debug(
            "permission_decision",
            tool=call.name,
            action=self._permission_log_action(action),
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
                    "queued_id": call.queued_id,
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
                "queued_id": call.queued_id,
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
            error = _with_args(f"Unknown tool '{call.name}': {exc}", call)
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "queued_id": call.queued_id,
                    "tool": call.name,
                    "error": error,
                }
            )
            return self._tool_error_outcome(call, error)
        except Exception as exc:
            error = _with_args(f"Tool '{call.name}' failed: {exc}", call)
            await self._send(
                {
                    "type": "tool_error",
                    "step": step,
                    "call_id": call.call_id,
                    "queued_id": call.queued_id,
                    "tool": call.name,
                    "error": error,
                }
            )
            return self._tool_error_outcome(
                call,
                exc.content
                if isinstance(exc, ToolError) and exc.content is not None
                else error,
            )
        await self._send(
            {
                "type": "tool_completed",
                "step": step,
                "call_id": call.call_id,
                "queued_id": call.queued_id,
                "tool": call.name,
                "output": result.output,
                "attachments": list(result.attachments or []),
            }
        )
        if result.metadata.get("unified_diff"):
            await self._send(
                {
                    "type": "patch",
                    "step": step,
                    "call_id": call.call_id,
                    "queued_id": call.queued_id,
                    "tool": call.name,
                    "title": f"Patch {result.metadata.get('path', call.name)}",
                    "path": result.metadata.get("path", ""),
                    "unified_diff": result.metadata.get("unified_diff", ""),
                }
            )
        if tool_key == "todowrite":
            await self._send(
                {
                    "type": "todo_updated",
                    "step": step,
                    "call_id": call.call_id,
                    "queued_id": call.queued_id,
                    "todos": await _todos_payload(call_ctx),
                }
            )
        return _ToolCallOutcome(
            # Images and PDFs become multimodal parts (see
            # build_tool_message_content): images as image_url, PDFs as
            # canonical file parts lowered per provider. The bytes also
            # travel via the tool_completed event into HarnessPart.meta.
            message=LLMMessage(
                role="tool",
                content=(
                    result.content
                    if result.content is not None and not result.truncated
                    else build_tool_message_content(result.output, result.attachments)
                ),
                tool_call_id=call.call_id,
            ),
            result=result,
        )

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
