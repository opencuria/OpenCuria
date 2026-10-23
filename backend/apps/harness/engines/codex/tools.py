"""Authorize Codex dynamic tools through the common managed-tool dispatcher."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from typing import Any

from ...access.base import WorkspaceAccessor
from ...agents.definitions import AgentDefinition
from ...permissions.evaluator import PermissionEvaluator
from ...permissions.scopes import is_permission_scope, permission_scope
from ...runner import RunOptions
from ...tools.base import ToolContext, ToolRegistry
from ...tools.invocation import AuthorizedToolDispatcher, _PendingToolCall
from ..shared.security import redact_action, redact_title
from ..shared.shellpolicy import shell_command_is_explore_safe


class ManagedToolInvoker(AuthorizedToolDispatcher):
    """Adapt Codex RPC calls without requiring a native model or provider."""

    def __init__(
        self,
        tools: ToolRegistry,
        accessor: WorkspaceAccessor | None,
        emit: Callable[[dict[str, Any]], Awaitable[None]],
        evaluator: PermissionEvaluator | None = None,
        *,
        secrets: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        """Configure dispatch and a live secret source (including refreshed auth)."""
        super().__init__(tools=tools, accessor=accessor, emit=emit, evaluator=evaluator)
        self._secrets = secrets or (lambda: ())
        self.seen: set[str] = set()
        self.active: dict[str, str] = {}

    def _permission_log_action(self, action: str) -> str:
        """Keep provider secrets out of dispatch diagnostic logs."""
        return redact_action(action, self._secrets())

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
        """Persist only safe exact-action scopes, never raw provider credentials."""
        secrets = tuple(self._secrets())
        display = redact_action(action, secrets)
        scope = permission_scope(action, display)
        callback = opts.on_permission

        async def safe_permission(**kwargs: Any) -> str:
            kwargs["action"] = display
            if is_permission_scope(scope):
                kwargs["permission_action"] = scope
            assert callback is not None
            return await callback(**kwargs)

        return await super()._resolve_ask(
            tool_name=tool_name,
            action=scope,
            title=redact_title(title, secrets),
            call_id=call_id,
            step=step,
            doom_loop=doom_loop,
            opts=replace(opts, on_permission=safe_permission if callback else None),
        )

    def _decide(
        self,
        agent: AgentDefinition,
        tool_name: str,
        action: str,
        mode: str,
        **kwargs: Any,
    ) -> str:
        """Keep primary plan edits gated even if a caller passed the build role."""
        decision = super()._decide(agent, tool_name, action, mode, **kwargs)
        if (
            (agent.name == "plan" or (agent.name == "build" and mode == "plan"))
            and self._permission_key(tool_name) == "edit"
            and decision == "allow"
        ):
            return "ask"
        return decision

    async def _send(self, event: dict[str, Any]) -> None:
        """Track lifecycle balance before forwarding engine events."""
        key = event.get("call_id")
        if event.get("type") == "tool_queued":
            self.active[key] = event.get("tool", "tool")
        elif event.get("type") in {"tool_completed", "tool_error"}:
            self.active.pop(key, None)
        await self._emit(event)

    async def finish_abandoned(self) -> None:
        """Close outstanding call lifecycles when the app-server ends."""
        for key, tool in list(self.active.items()):
            await self._send(
                {
                    "type": "tool_error",
                    "step": 1,
                    "call_id": key,
                    "tool": tool,
                    "error": "Codex run ended before tool completion",
                }
            )

    def schemas(
        self, agent: AgentDefinition, mode: str, opts: RunOptions
    ) -> list[dict[str, Any]]:
        """Register a stable ceiling; every invocation uses current permissions.

        A thread retains its dynamic signatures across plan/build transitions.
        Registry/schema changes still alter the fingerprint and require a fork.
        Role, depth and policy filtering belong to dispatch, not registration.
        """
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.parameters_schema(),
            }
            for tool in self.tools.list()
        ]

    async def invoke(
        self,
        params: dict[str, Any],
        *,
        agent: AgentDefinition,
        mode: str,
        opts: RunOptions,
        model: str,
        allowed: set[str],
    ) -> dict[str, Any]:
        """Validate an RPC request and dispatch under this run's live policy."""
        name = str(params.get("tool") or "")
        call_id = str(params.get("callId") or "")
        args = params.get("arguments")
        if call_id in self.seen:
            return {
                "contentItems": [
                    {
                        "type": "inputText",
                        "text": (
                            "Duplicate tool request rejected; "
                            "original action not repeated."
                        ),
                    }
                ],
                "success": False,
            }
        self.seen.add(call_id)
        if len(self.seen) > 4096:
            raise RuntimeError("Codex tool count limit reached")
        error = ""
        if (
            not call_id
            or name not in allowed
            or not isinstance(args, dict)
            or params.get("namespace")
            or (name == "task" and opts.run_subagent is None)
        ):
            error = "Tool is not authorized in the current run"
        elif agent.name == "explore" and (
            name in {"write", "edit", "task"}
            or name.startswith(("mcp_", "process_"))
            or (
                name == "bash"
                and not shell_command_is_explore_safe(str(args.get("command") or ""))
            )
        ):
            error = "Explore permits only read-only tools and allowlisted shell reads"
        if error:
            await self._send(
                {
                    "type": "tool_error",
                    "step": 1,
                    "call_id": call_id,
                    "tool": name,
                    "error": error,
                }
            )
            return {
                "contentItems": [{"type": "inputText", "text": error}],
                "success": False,
            }
        raw = json.dumps(args, ensure_ascii=False)
        if len(raw.encode()) > 256 * 1024:
            raise ValueError("Codex tool arguments exceed limit")
        await self._send(
            {
                "type": "tool_queued",
                "step": 1,
                "call_id": call_id,
                "tool": name,
                "arguments": raw,
            }
        )
        ctx = ToolContext(
            session_id=opts.session_id,
            workspace_id=opts.workspace_id,
            accessor=self.accessor,
            agent_name=agent.name,
            directory=opts.cwd,
            depth=opts.depth,
            max_depth=opts.max_depth,
            model=model,
            registry=self.tools,
            run_subagent=opts.run_subagent,
            on_question=opts.on_question,
            question_timeout=opts.question_timeout,
        )
        outcome = await self._dispatch_tool_call(
            call=_PendingToolCall(call_id, name, args, raw),
            ctx=ctx,
            agent=agent,
            mode=mode,
            step=1,
            depth=opts.depth,
            max_depth=opts.max_depth,
            doom_loop=False,
            opts=opts,
        )
        from .media import dynamic_content

        try:
            content = await dynamic_content(outcome.message.content)
        except ValueError as exc:
            # Conversion failures must be visible after observable tool effects.
            return {
                "success": False,
                "contentItems": [{"type": "inputText", "text": str(exc)}],
            }
        return {"contentItems": content, "success": outcome.result is not None}
