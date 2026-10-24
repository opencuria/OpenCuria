"""Fail-closed OpenCuria policy adapters for Claude Code tool hooks."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict, deque
from typing import Any

from ...agents.definitions import AgentDefinition, canonical_subagent_type, get_agent
from ...permissions.evaluator import ASK, DENY, PermissionEvaluator
from ...permissions.scopes import is_permission_scope, permission_scope
from ...runner import RunOptions
from ...tools.base import ToolRegistry
from .events import StepState
from .policy import (
    NATIVE_TOOL_KEYS,
    combine_decisions,
    decide_tool,
    registry_tool_identity,
    tool_identity,
)
from .security import redact_action, redact_arguments, redact_title
from .shellpolicy import shell_command_is_explore_safe


class PermissionBridge:
    """Gate native/MCP tools and correlate SDK MCP dispatch IDs."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        evaluator: PermissionEvaluator,
        options: RunOptions,
        agent: AgentDefinition,
        mode: str,
        max_depth: int,
        engine: Any | None = None,
    ) -> None:
        self.tools, self.evaluator, self.options = tools, evaluator, options
        self.agent, self.mode, self.max_depth, self.engine = (
            agent,
            mode,
            max_depth,
            engine,
        )
        self._mcp_calls: dict[tuple[str, str], deque[str]] = defaultdict(deque)
        self._queued_call_ids: set[str] = set()
        self._authorized_call_ids: set[str] = set()
        self._call_ready: dict[str, asyncio.Event] = {}
        self._claimed_call_ids: set[str] = set()
        self._mcp_lock = asyncio.Lock()

    @staticmethod
    def canonical_args(arguments: dict[str, Any]) -> str:
        """Serialize arguments for deterministic MCP callback correlation."""
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)

    def queue_mcp_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
        *,
        authorized: bool = True,
    ) -> None:
        """Record a hook-checked MCP call, preserving duplicate order."""
        if not call_id or call_id in self._queued_call_ids:
            return
        key = (tool_name, self.canonical_args(arguments))
        self._mcp_calls[key].append(call_id)
        self._queued_call_ids.add(call_id)
        self._call_ready[call_id] = asyncio.Event()
        if authorized:
            self._authorized_call_ids.add(call_id)

    def has_queued_mcp_call(
        self, tool_name: str, arguments: dict[str, Any], call_id: str
    ) -> bool:
        """Check exact pending identity without authorizing the MCP call."""
        return call_id in self._mcp_calls.get(
            (tool_name, self.canonical_args(arguments)), ()
        )

    def authorize_mcp_call(
        self, tool_name: str, arguments: dict[str, Any], call_id: str
    ) -> bool:
        """Authorize one exact pending ASK call after user approval."""
        key = (tool_name, self.canonical_args(arguments))
        if call_id not in self._mcp_calls.get(key, ()):
            return False
        self._authorized_call_ids.add(call_id)
        ready = self._call_ready.get(call_id)
        if ready is not None:
            ready.set()
        return True

    async def _claim_queued_call(self, key: tuple[str, str], call_id: str) -> bool:
        """Atomically claim one exact allowed call."""
        async with self._mcp_lock:
            pending = self._mcp_calls.get(key)
            if (
                not pending
                or call_id not in pending
                or call_id in self._claimed_call_ids
                or call_id not in self._authorized_call_ids
            ):
                return False
            pending.remove(call_id)
            self._queued_call_ids.discard(call_id)
            self._authorized_call_ids.discard(call_id)
            self._call_ready.pop(call_id, None)
            self._claimed_call_ids.add(call_id)
            if not pending:
                self._mcp_calls.pop(key, None)
            return True

    async def claim_mcp_call(
        self, call_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> bool:
        """Consume the exact call ID carried in MCP request metadata."""
        return await self._claim_queued_call(
            (tool_name, self.canonical_args(arguments)), call_id
        )

    async def take_mcp_call(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        preferred_call_id: str = "",
        timeout: float = 15.0,
    ) -> str | None:
        """Wait for and consume one exact hook-approved MCP call."""
        key = (tool_name, self.canonical_args(arguments))
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            async with self._mcp_lock:
                pending = self._mcp_calls.get(key)
                if not pending:
                    return None
                call_id = preferred_call_id or pending[0]
                if call_id not in pending or call_id in self._claimed_call_ids:
                    return None
                ready = (
                    None
                    if call_id in self._authorized_call_ids
                    else self._call_ready.setdefault(call_id, asyncio.Event())
                )
            if ready is None:
                return call_id if await self._claim_queued_call(key, call_id) else None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                await asyncio.wait_for(ready.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    def discard_mcp_call(self, call_id: str) -> None:
        """Remove a denied or failed call from all pending queues."""
        for key, pending in tuple(self._mcp_calls.items()):
            if call_id in pending:
                pending.remove(call_id)
                if not pending:
                    self._mcp_calls.pop(key, None)
        self._queued_call_ids.discard(call_id)
        self._authorized_call_ids.discard(call_id)
        self._claimed_call_ids.discard(call_id)
        ready = self._call_ready.pop(call_id, None)
        if ready is not None:
            ready.set()

    def _safe_projection(
        self, result: tuple[str, str, str, str]
    ) -> tuple[str, str, str, str]:
        """Redact callback-visible action/title while preserving policy."""
        secrets = self.engine._event_secrets() if self.engine is not None else ()
        return (
            result[0],
            result[1],
            redact_action(result[2], secrets),
            redact_title(result[3], secrets),
        )

    def decide(
        self,
        source_name: str,
        arguments: dict[str, Any],
        *,
        agent: AgentDefinition | None = None,
        depth: int = 0,
    ) -> tuple[str, str, str, str]:
        """Evaluate effective agent, permission rules and nesting depth."""
        if self.engine is not None and not source_name.startswith("mcp__"):
            arguments = self.engine._event_arguments(source_name, arguments)
        raw_arguments = dict(arguments)
        selected = (
            sorted(self.engine._managed_skill_names) if self.engine is not None else []
        )
        effective_agent = agent or self.agent
        result = decide_tool(
            source_name=source_name,
            arguments=arguments,
            tools=self.tools,
            agent=effective_agent,
            evaluator=self.evaluator,
            mode=self.mode,
            depth=depth,
            max_depth=self.max_depth,
            selected_skills=selected,
            cwd=getattr(self.engine, "_cwd", None),
        )
        if effective_agent.name == "explore":
            if source_name in {"Task", "Agent"}:
                result = (DENY, result[1], result[2], "Explore agents cannot delegate")
            elif source_name in {"WebFetch", "WebSearch"}:
                permission_key = (
                    "webfetch" if source_name == "WebFetch" else "websearch"
                )
                result = (
                    combine_decisions(
                        self.evaluator.evaluate(
                            permission_key, result[2], mode=self.mode
                        ),
                        PermissionEvaluator(
                            agent_rules=dict(effective_agent.permissions or {})
                        ).evaluate(permission_key, result[2], mode=self.mode),
                    ),
                    result[1],
                    result[2],
                    result[3],
                )
            elif source_name == "Bash" and not shell_command_is_explore_safe(
                str(raw_arguments.get("command") or "")
            ):
                result = (
                    DENY,
                    result[1],
                    result[2],
                    "Explore agents cannot mutate files",
                )
            elif source_name.startswith("mcp__"):
                # MCP annotations are not a trustworthy read-only contract.
                result = (
                    DENY,
                    result[1],
                    result[2],
                    "Explore agents cannot use MCP tools",
                )
        return self._safe_projection(result)

    def action_scope(
        self,
        source_name: str,
        arguments: dict[str, Any],
        *,
        display_action: str,
    ) -> str:
        """Return a redaction-safe exact identity for this tool action."""
        cwd = getattr(self.engine, "_cwd", None)
        if source_name.startswith("mcp__"):
            _event_tool, _key, raw_action = registry_tool_identity(
                source_name, arguments, self.tools
            )
        elif source_name in NATIVE_TOOL_KEYS:
            _event_tool, _key, raw_action = tool_identity(
                source_name, arguments, self.tools, cwd=cwd
            )
        else:
            _event_tool, _key, raw_action = registry_tool_identity(
                source_name, arguments, self.tools
            )
        return permission_scope(raw_action, display_action)

    def scope(
        self, engine: Any, hook_input: dict[str, Any]
    ) -> tuple[str | None, AgentDefinition, int]:
        """Resolve SDK-confirmed child identity; otherwise keep root scope."""
        agent_id = str(hook_input.get("agent_id") or "")
        if not agent_id:
            return None, self.agent, self.options.depth
        if agent_id not in engine._agent_parent_tool:
            raise ValueError("Unknown Claude subagent identity")
        parent = str(engine._agent_parent_tool.get(agent_id) or "")
        raw_type = str(engine._agent_type_raw.get(agent_id) or "")
        try:
            child = get_agent(canonical_subagent_type(raw_type))
        except (KeyError, ValueError) as exc:
            raise ValueError("Unknown Claude subagent type") from exc
        depth = engine._agent_depth.get(agent_id)
        if child.name not in {"explore", "general"} or not parent or depth is None:
            raise ValueError("Unmapped Claude subagent identity")
        # Task/Agent's native CLI call may complete before its asynchronous
        # child does. Trust only the SDK-mapped, still-live child, with its
        # original depth; completed or invented children still fail closed.
        finished_subtasks = getattr(engine, "_finished_subtasks", set())
        started_subtasks = getattr(engine, "_started_subtasks", set())
        if agent_id in finished_subtasks or (
            parent not in engine._tool_events and agent_id not in started_subtasks
        ):
            raise ValueError("Claude subagent parent call is unavailable")
        return parent, child, depth

    @staticmethod
    def hook_decision(decision: str, reason: str = "") -> dict[str, Any]:
        """Build SDK-compatible PreToolUse output."""
        output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
        if reason:
            output["permissionDecisionReason"] = reason[:500]
        return {"hookSpecificOutput": output}

    def hooks(self, engine: Any) -> dict[str, list[Any]]:
        """Build permission and subagent lifecycle callbacks for this run."""
        from claude_agent_sdk import HookMatcher

        async def pre_tool_use(hook_input, tool_use_id, _context):
            wire = str(hook_input.get("tool_name") or "")
            source = wire.removeprefix("mcp__opencuria__")
            registry_source = engine._wire_to_registry.get(source, source)
            is_mcp = wire.startswith("mcp__opencuria__")
            policy_source = registry_source if is_mcp else source
            arguments = hook_input.get("tool_input")
            arguments = arguments if isinstance(arguments, dict) else {}
            call_id = str(tool_use_id or hook_input.get("tool_use_id") or "")
            try:
                parent, agent, depth = self.scope(engine, hook_input)
            except ValueError:
                if call_id:
                    self.discard_mcp_call(call_id)
                    await engine._finish_tool_error(
                        call_id,
                        "Unmapped Claude subagent identity",
                        parent_id=None,
                    )
                return self.hook_decision("deny", "Unmapped Claude subagent identity")
            if not call_id:
                return self.hook_decision("deny", "Missing OpenCuria tool call id")
            is_task_call = source in {"Task", "Agent"}
            decision, event_tool, _action, title = self.decide(
                policy_source, arguments, agent=agent, depth=depth
            )
            safe_action = redact_action(_action, engine._event_secrets())
            permission_action = self.action_scope(
                policy_source, arguments, display_action=safe_action
            )
            if agent.name == "explore":
                if is_mcp or source in {"Write", "Edit", "NotebookEdit"}:
                    decision = DENY
                elif source == "Bash" and not shell_command_is_explore_safe(
                    str(arguments.get("command") or "")
                ):
                    decision = DENY
                elif source in {"Task", "Agent"}:
                    decision = DENY
            if not is_mcp and source == "Skill":
                skill = str(arguments.get("skill", "") or "")
                if agent.name == "explore" or skill not in engine._managed_skill_names:
                    decision = DENY
            if not is_mcp and source in {"ExitPlanMode", "AskUserQuestion"}:
                decision = DENY
            if (
                call_id not in engine._tool_events
                or call_id in engine._early_queued_calls
            ):
                early_queued = call_id in engine._early_queued_calls
                await engine._flush_stream_redactors(parent=parent or "")
                safe_args = redact_arguments(
                    engine._event_arguments(source, arguments), engine._event_secrets()
                )
                safe_title = redact_title(title, engine._event_secrets())
                state = {
                    "tool": event_tool,
                    "source_tool": wire,
                    "registry_tool": registry_source,
                    "policy_source": policy_source,
                    "event_arguments": safe_args,
                    "title": safe_title,
                    "step": (
                        engine._step_states.setdefault(parent or "", StepState()).step
                        if early_queued
                        else await engine._start_new_step(parent)
                    ),
                    "parent_tool_use_id": parent,
                    "agent_name": agent.name,
                    "agent_id": str(hook_input.get("agent_id") or ""),
                    "depth": depth,
                    "permission_decision": decision,
                    "permission_scope": permission_action,
                    "permission_scope_is_opaque": is_permission_scope(
                        permission_action
                    ),
                    "started": False,
                }
                engine._tool_events[call_id] = state
                engine._tool_arguments[call_id] = dict(arguments)
                if early_queued:
                    engine._early_queued_calls.discard(call_id)
                if not early_queued:
                    await engine._publish(
                        {
                            "type": "tool_queued",
                            "step": state["step"],
                            "call_id": call_id,
                            "tool": event_tool,
                            "title": safe_title,
                            "arguments": json.dumps(safe_args, ensure_ascii=False),
                            "metadata": {
                                "source_tool": wire,
                                "registry_tool": registry_source,
                            },
                        },
                        parent_tool_use_id=parent,
                    )
            if is_task_call and decision not in {DENY, ASK}:
                engine._pending_task_tool_ids.add(call_id)
            if decision == DENY:
                engine._pending_task_tool_ids.discard(call_id)
                engine._clear_native_snapshot(call_id)
                reason = (
                    "Read-only explore agents cannot use this tool"
                    if agent.name == "explore"
                    else "Denied by OpenCuria permissions"
                )
                self.discard_mcp_call(call_id)
                await engine._finish_tool_error(call_id, reason, parent_id=parent)
                return self.hook_decision("deny", reason)
            if is_mcp and registry_source in self.tools:
                self.queue_mcp_call(
                    registry_source, arguments, call_id, authorized=decision != ASK
                )
            if decision == ASK or (
                self.mode == "plan" and not is_mcp and source in NATIVE_TOOL_KEYS
            ):
                # Since CLI 2.1.299, a hook allow in Plan can fall through to its
                # auto-mode classifier instead of the host. Ask routes to our
                # paired callback; preserve the actual OpenCuria decision so an
                # allowed action does not become a new user approval prompt.
                return self.hook_decision(
                    "ask", "OpenCuria permission decision required"
                )
            if not is_mcp and source in {"Write", "Edit"}:
                await engine._native_pre_tool(call_id, source, arguments=arguments)
            await engine._start_tool(call_id, parent_id=parent)
            return self.hook_decision("allow")

        async def post_tool_use(hook_input, tool_use_id, _context):
            call_id = str(tool_use_id or hook_input.get("tool_use_id") or "")
            state = engine._tool_events.get(call_id)
            if state is None:
                return {}
            engine._pending_task_tool_ids.discard(call_id)
            result = engine._tool_results.pop(call_id, None)
            output = engine._tool_output(
                result.output if result is not None else hook_input.get("tool_response")
            )
            self.discard_mcp_call(call_id)
            if result is not None and result.metadata.get("is_error"):
                await engine._finish_tool_error(
                    call_id, output, parent_id=state.get("parent_tool_use_id")
                )
                return {}
            await engine._finish_tool_completed(
                call_id,
                output,
                parent_id=state.get("parent_tool_use_id"),
                attachments=list(result.attachments or []) if result else [],
            )
            if state.get("tool") == "todowrite":
                await engine._send_todos(state.get("parent_tool_use_id"))
            return {}

        async def post_tool_failure(hook_input, tool_use_id, _context):
            call_id = str(tool_use_id or hook_input.get("tool_use_id") or "")
            state = engine._tool_events.get(call_id)
            if state is None:
                return {}
            engine._pending_task_tool_ids.discard(call_id)
            self.discard_mcp_call(call_id)
            error = redact_action(
                str(hook_input.get("error") or "Claude tool failed"),
                engine._event_secrets(),
            )
            await engine._finish_tool_error(
                call_id, error, parent_id=state.get("parent_tool_use_id")
            )
            return {}

        async def subagent_start(hook_input, tool_use_id, _context):
            agent_id = str(hook_input.get("agent_id") or "")
            parent_tool = str(
                tool_use_id or engine._agent_parent_tool.get(agent_id) or ""
            )
            if not parent_tool and len(engine._pending_task_tool_ids) == 1:
                candidate = next(iter(engine._pending_task_tool_ids))
                if candidate in engine._tool_events:
                    parent_tool = candidate
                else:
                    engine._pending_task_tool_ids.discard(candidate)
            elif not parent_tool and len(engine._pending_task_tool_ids) > 1:
                # Retire every ambiguous task callback without guessing which
                # child belongs to which parent tool invocation.
                for call_id in tuple(engine._pending_task_tool_ids):
                    engine._pending_task_tool_ids.discard(call_id)
                    state = engine._tool_events.get(call_id)
                    if state is not None:
                        await engine._finish_tool_error(
                            call_id,
                            "Subagent task correlation is ambiguous",
                            parent_id=state.get("parent_tool_use_id"),
                        )
                return {}
            await engine._message_projector.start_subtask(
                agent_id,
                parent_tool,
                str(hook_input.get("description") or ""),
                agent_type=str(hook_input.get("agent_type") or ""),
            )
            return {}

        async def subagent_stop(hook_input, _tool_use_id, _context):
            """Remember a stop request; the SDK task update supplies final status."""
            agent_id = str(hook_input.get("agent_id") or "")
            if agent_id:
                engine._stopped_subagents.add(agent_id)
            return {}

        return {
            "PreToolUse": [
                HookMatcher(matcher=None, hooks=[pre_tool_use], timeout=120)
            ],
            "PostToolUse": [
                HookMatcher(matcher=None, hooks=[post_tool_use], timeout=120)
            ],
            "PostToolUseFailure": [
                HookMatcher(matcher=None, hooks=[post_tool_failure], timeout=120)
            ],
            "SubagentStart": [
                HookMatcher(matcher=None, hooks=[subagent_start], timeout=30)
            ],
            "SubagentStop": [
                HookMatcher(matcher=None, hooks=[subagent_stop], timeout=30)
            ],
        }

    async def can_use_tool(
        self, engine: Any, source_name: str, arguments: dict[str, Any], context: Any
    ) -> Any:
        """Resolve native SDK ask requests through OpenCuria's permission gate."""
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        is_mcp = source_name.startswith("mcp__opencuria__")
        arguments = (
            dict(arguments)
            if is_mcp
            else engine._event_arguments(source_name, arguments)
        )
        call_id = str(
            getattr(context, "tool_use_id", None)
            or getattr(context, "toolUseID", None)
            or ""
        )
        if not call_id and is_mcp:
            try:
                from mcp.server.lowlevel.server import request_ctx

                metadata = request_ctx.get().meta
                extra = metadata.model_extra or {} if metadata is not None else {}
                cc = extra.get("claudecode") or extra.get("claudeCode") or {}
                call_id = str(
                    extra.get("claudecode/toolUseId")
                    or cc.get("toolUseId")
                    or cc.get("tool_use_id")
                    or ""
                )
            except (LookupError, AttributeError, TypeError):
                call_id = ""
        state = engine._tool_events.get(call_id, {})
        if not is_mcp and not state:
            return PermissionResultDeny(
                message="Native permission request has no matching OpenCuria hook"
            )
        if not is_mcp:
            expected = engine._tool_arguments.get(call_id)
            if (
                not call_id
                or not state
                or expected is None
                or state.get("source_tool") != source_name
                or state.get("permission_decision") not in {"ask", "allow"}
                or engine._event_arguments(source_name, expected) != arguments
            ):
                return PermissionResultDeny(
                    message="Native permission request has no matching OpenCuria hook"
                )
        registry_name = ""
        if is_mcp:
            wire = source_name.removeprefix("mcp__opencuria__")
            registry_name = engine._wire_to_registry.get(wire, wire)
            if not call_id or not state:
                return PermissionResultDeny(
                    message="MCP permission request has no matching OpenCuria hook"
                )
            if state.get("permission_decision") == "allow":
                return PermissionResultAllow()
            if state.get(
                "permission_decision"
            ) != "ask" or not self.has_queued_mcp_call(
                registry_name, arguments, call_id
            ):
                return PermissionResultDeny(message="MCP call was not approved")
        hook_agent_id = str(getattr(context, "agent_id", None) or "")
        state_agent_id = str(state.get("agent_id") or "")
        if hook_agent_id and state_agent_id and hook_agent_id != state_agent_id:
            return PermissionResultDeny(message="Claude subagent identity mismatch")
        trusted_agent_id = hook_agent_id or state_agent_id
        try:
            parent_id, scoped_agent, depth = self.scope(
                engine, {"agent_id": trusted_agent_id}
            )
        except ValueError:
            if call_id:
                self.discard_mcp_call(call_id)
                await engine._finish_tool_error(
                    call_id,
                    "Unmapped Claude subagent identity",
                    parent_id=None,
                )
            return PermissionResultDeny(message="Unmapped Claude subagent identity")
        if not is_mcp and state.get("parent_tool_use_id") != parent_id:
            return PermissionResultDeny(message="Claude subagent parent scope mismatch")
        try:
            agent = (
                get_agent(str(state["agent_name"]))
                if state.get("agent_name")
                else scoped_agent
            )
        except KeyError:
            agent = scoped_agent
        policy_source = registry_name if is_mcp else source_name
        decision, tool, action, title = self.decide(
            policy_source, arguments, agent=agent, depth=depth
        )
        safe_action = redact_action(action, engine._event_secrets())
        permission_action = self.action_scope(
            policy_source, arguments, display_action=safe_action
        )
        if agent.name == "explore":
            if is_mcp or source_name in {"Write", "Edit", "NotebookEdit"}:
                decision = DENY
            elif source_name == "Bash" and not shell_command_is_explore_safe(
                str(arguments.get("command") or "")
            ):
                decision = DENY
        if decision == DENY:
            if not is_mcp:
                engine._clear_native_snapshot(call_id)
            if call_id:
                await engine._finish_tool_error(
                    call_id, "Denied by OpenCuria permissions", parent_id=parent_id
                )
            return PermissionResultDeny(message="Denied by OpenCuria permissions")
        if decision != ASK:
            if is_mcp:
                return (
                    PermissionResultAllow()
                    if state.get("permission_decision") == "allow"
                    else PermissionResultDeny(
                        message="MCP call lacks a PreToolUse decision"
                    )
                )
            if call_id:
                if source_name in {"Write", "Edit"}:
                    await engine._native_pre_tool(
                        call_id, source_name, arguments=arguments
                    )
                await engine._start_tool(call_id, parent_id=parent_id)
            return PermissionResultAllow()
        if not call_id:
            return PermissionResultDeny(message="Permission request has no call id")
        action_key = (tool, permission_action)
        if (
            not is_mcp
            and agent.name != "explore"
            and (action_key in self.options.once_approved or self.options.auto_approve)
        ):
            approved = True
        elif is_mcp and agent.name != "explore" and self.options.auto_approve:
            approved = True
        elif self.options.on_permission is None:
            approved = False
        else:
            try:
                pending = self.options.on_permission(
                    tool=tool,
                    action=redact_action(action, engine._event_secrets()),
                    title=redact_title(title, engine._event_secrets()),
                    call_id=call_id,
                    key="permission",
                    **(
                        {"permission_action": permission_action}
                        if is_permission_scope(permission_action)
                        else {}
                    ),
                )
                timeout = self.options.permission_timeout
                response = (
                    await asyncio.wait_for(pending, timeout)
                    if timeout is not None and timeout > 0
                    else await pending
                )
            except asyncio.TimeoutError:
                response = "reject"
            approved = str(response or "").strip().lower() in {
                "once",
                "always",
                "allow",
                "approved",
                "yes",
            }
            if approved and not is_mcp and agent.name != "explore":
                self.options.once_approved.add(action_key)
        if not approved:
            if not is_mcp:
                engine._clear_native_snapshot(call_id)
            self.discard_mcp_call(call_id)
            await engine._finish_tool_error(
                call_id, "Denied by user", parent_id=parent_id
            )
            return PermissionResultDeny(message="Denied by user")
        if is_mcp:
            if not self.authorize_mcp_call(registry_name, arguments, call_id):
                self.discard_mcp_call(call_id)
                await engine._finish_tool_error(
                    call_id,
                    "MCP permission approval did not match the call",
                    parent_id=parent_id,
                )
                return PermissionResultDeny(
                    message="MCP permission approval did not match the call"
                )
            state["permission_decision"] = "allow"
        elif source_name in {"Write", "Edit"}:
            await engine._native_pre_tool(call_id, source_name, arguments=arguments)
        if not is_mcp and source_name in {"Task", "Agent"}:
            engine._pending_task_tool_ids.add(call_id)
        await engine._start_tool(call_id, parent_id=parent_id)
        return PermissionResultAllow()


__all__ = ["PermissionBridge"]
