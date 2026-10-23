"""Pinned official Codex app-server engine, scoped to one fenced workspace lease."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import replace
from typing import Any, TypeVar

from ...access.base import WorkspaceAccessor, sanitize_exec_workdir
from ...agents.definitions import get_agent
from ...desktop_leases import DesktopLease
from ...permissions.evaluator import PermissionEvaluator
from ...prompts.native import native_prompt_extension
from ...providers.base import Usage
from ...runner import RunOptions, RunResult
from ...tools.base import ToolRegistry
from ..shared.security import StreamingRedactor, redact_text, redact_value
from .auth import CodexAuthConfiguration
from .catalog import CODEX_VERSION, codex_provider
from .configuration import _toml, runtime_argv, runtime_settings
from .errors import CodexRecoveryError
from .events import CodexEventProjector
from .media import user_input
from .protocol import CodexProtocol, CodexProtocolError, CodexRPCError
from .session_store import (
    CodexSessionStore,
    capture,
    home_for,
    materialize_skills,
    restore_snapshot,
    state_action,
)
from .tools import ManagedToolInvoker

_T = TypeVar("_T")


async def _finish_cleanup(
    awaitable: Awaitable[_T], *, propagate_cancel: bool = False
) -> _T:
    """Join one bounded cleanup task despite repeated outer cancellation."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            continue
    result = task.result()
    if cancelled and propagate_cancel:
        raise asyncio.CancelledError()
    return result


class CodexEngine:
    """Drive one official app-server turn under a durable workspace owner fence."""

    def __init__(
        self,
        *,
        tools: ToolRegistry,
        accessor: WorkspaceAccessor,
        emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        auth: CodexAuthConfiguration,
        external_session_id: str = "",
        engine_state: dict[str, Any] | None = None,
        session_store: CodexSessionStore | None = None,
        on_binding: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_child_event: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_owner: Callable[[dict[str, Any], str], Awaitable[None]] | None = None,
        evaluator: PermissionEvaluator | None = None,
        effort: str = "low",
        refresh_auth: Callable[[], Awaitable[CodexAuthConfiguration]] | None = None,
    ) -> None:
        """Configure authorized tools, checkpoint storage and ownership callbacks."""
        if auth is None:
            raise ValueError("Codex requires resolved server-side authorization")
        self.tools, self.accessor, self.emit = tools, accessor, emit
        self.auth, self.refresh_auth = auth, refresh_auth
        self.external_session_id = str(external_session_id or "")
        if self.external_session_id:
            uuid.UUID(self.external_session_id)
        self.engine_state = dict(engine_state or {})
        self.session_store = session_store
        self.on_binding, self.on_owner = on_binding, on_owner
        self.on_child_event = on_child_event
        self.projector = CodexEventProjector(self)
        self._child_scope: ContextVar[str | None] = ContextVar(
            "codex_child", default=None
        )
        self.evaluator, self.effort = evaluator, effort
        self._owner_released = False
        self._running = False
        self.turn_id = ""
        self.status: str | None = None
        self.thread_id = self.external_session_id
        self.terminal = asyncio.Event()
        self.turn_ready = asyncio.Event()
        self.allowed = set()
        self.secret_values = [
            str(x) for x in auth.credentials.values() if isinstance(x, str)
        ]
        self.invoker = ManagedToolInvoker(
            tools, accessor, self._send, evaluator, secrets=lambda: self.secret_values
        )

    async def _send(self, event: dict[str, Any]) -> None:
        """Project all persisted/visible engine events through current secrets."""
        safe = redact_value(event, self.secret_values)
        parent = self._child_scope.get()
        if parent and self.on_child_event:
            await self.on_child_event(parent, safe)
        elif self.emit:
            await self.emit(safe)

    async def _bind(self, **state: Any) -> None:
        """Publish checkpoint metadata through the execution owner fence."""
        metadata = {**self.engine_state, **state}
        if self.on_binding is None:
            if self.session_store is not None:
                raise RuntimeError(
                    "Durable Codex requires an owner-fenced binding callback"
                )
        else:
            await self.on_binding(self.thread_id, metadata)
        self.engine_state = metadata

    async def _owner(self, lease: DesktopLease, phase: str) -> None:
        """Publish a workspace-owner lifecycle transition."""
        if self.on_owner:
            await self.on_owner({**lease.owner, "owner_id": lease.owner_id}, phase)

    async def _server_request(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Handle only current-turn tools and exact-account credential refresh."""
        if method == "account/chatgptAuthTokens/refresh":
            previous = params.get("previousAccountId")
            if previous and previous != self.auth.credentials.get("account_id"):
                raise RuntimeError("Codex authorization account mismatch")
            callback = self.refresh_auth or getattr(self.auth, "refresh", None)
            if self.auth.provider_id != "chatgpt" or callback is None:
                raise RuntimeError(
                    "Codex authorization refresh unavailable; reconnect account"
                )
            fresh = await asyncio.wait_for(callback(), 9)
            if (
                fresh.provider_id != self.auth.provider_id
                or fresh.connection_id != self.auth.connection_id
            ):
                raise RuntimeError("Codex authorization identity changed")
            self.auth = fresh
            self.secret_values.extend(
                str(x) for x in fresh.credentials.values() if isinstance(x, str)
            )
            data = fresh.login_data
            return {
                "accessToken": data["accessToken"],
                "chatgptAccountId": data["chatgptAccountId"],
            }
        if method == "item/tool/call" and not self.turn_ready.is_set():
            await asyncio.wait_for(self.turn_ready.wait(), 5)
        child = self.projector.children.get(params.get("threadId"))
        if (
            (params.get("threadId") != self.thread_id and child is None)
            or not self.turn_id
            or self.terminal.is_set()
            or (child is None and params.get("turnId") != self.turn_id)
            or (
                child
                and (
                    child["finished"]
                    or not params.get("turnId")
                    or child.get("turn_id") not in {None, params.get("turnId")}
                )
            )
        ):
            raise RuntimeError("Stale Codex server request rejected")
        if method == "item/tool/call":
            token = self._child_scope.set(
                child["parent_tool_use_id"] if child else None
            )
            try:
                return await self.invoker.invoke(
                    params,
                    agent=get_agent("general") if child else self.agent,
                    mode=self.mode,
                    opts=replace(self.opts, depth=self.opts.depth + 1)
                    if child
                    else self.opts,
                    model=self.model,
                    allowed=self.allowed,
                )
            finally:
                self._child_scope.reset(token)
        if method == "mcpServer/elicitation/request":
            return {"action": "decline"}  # No automatic consent to native MCP prompts.
        if method.endswith("/requestApproval"):
            return {"decision": "decline"}
        # Built-in user questions cannot represent OpenCuria's persistent gates.
        # Only the managed question tool is advertised/accepted.
        raise RuntimeError("Use an authorized OpenCuria managed tool instead")

    async def _authorize_read(self, path: str) -> bool:
        """Apply current tool read policy before hydrating user media."""
        decision = self.invoker._decide(self.agent, "read", path, self.mode)
        if decision == "allow":
            return True
        if decision == "deny":
            return False
        return await self.invoker._resolve_ask(
            tool_name="read",
            action=path,
            title=f"Read {path}",
            call_id=f"media-{uuid.uuid4()}",
            step=1,
            doom_loop=False,
            opts=self.opts,
        )

    async def _steer(self, rpc: CodexProtocol) -> None:
        """Deliver reserved steering messages only after upstream acceptance."""
        channel = self.opts.steering
        if channel is None:
            return
        while not self.terminal.is_set():
            # Do not call next()/take(): those mark delivery BEFORE acceptance.
            # Peek public pending(), then withdraw and deliver only on RPC ACK.
            pending = channel.pending()
            if not pending:
                await asyncio.sleep(0.05)
                continue
            entry = channel.reserve(pending[0].id)
            if entry is None:
                await asyncio.sleep(0.05)
                continue
            sent = False
            try:
                content = await user_input(
                    entry.content,
                    accessor=self.accessor,
                    opts=self.opts,
                    include_history=False,
                )
                sent = True
                await rpc.request(
                    "turn/steer",
                    {
                        "threadId": self.thread_id,
                        "expectedTurnId": self.turn_id,
                        "input": content,
                    },
                    timeout=15,
                )
            except CodexRPCError:
                channel.release(entry.id)
                return  # Explicit rejection: keep pending for a follow-up.
            except BaseException:
                channel.release(entry.id, uncertain=sent)
                raise
            await channel.acknowledge(entry.id)

    async def run(
        self,
        prompt: str,
        agent: str,
        model: str,
        mode: str = "build",
        opts: RunOptions | None = None,
    ) -> RunResult:
        """Run one non-overlapping turn, preserving resumable checkpoint identity."""
        if self._running:
            raise RuntimeError("Codex engine already running")
        self._running = True
        try:
            return await self._run(prompt, agent, model, mode, opts or RunOptions())
        finally:
            self._running = False

    async def _run(
        self, prompt: str, agent_name: str, model: str, mode: str, opts: RunOptions
    ) -> RunResult:
        """Compose context, launch/resume, execute, and fence checkpoint cleanup."""
        if mode not in {"plan", "build"} or not prompt.strip():
            raise ValueError("Codex requires a prompt and build|plan mode")
        self.opts, self.agent, self.model, self.mode = (
            opts,
            get_agent(agent_name),
            model,
            mode,
        )
        if self.agent.name == "computeruse" or self.agent.mode == "hidden":
            raise ValueError("Codex does not support computeruse or hidden agents")
        if opts.depth < 0 or opts.depth > opts.max_depth:
            raise ValueError("Codex subagent depth exceeded")
        provider = codex_provider(model, self.engine_state)
        if provider != self.auth.provider_id:
            raise ValueError("Codex account and model provider mismatch")
        if self.engine_state.get("codex_dirty"):
            raise CodexRecoveryError(
                "Codex previous execution was not checkpointed. "
                "Workspace effects may remain; fork or start a new conversation."
            )
        home = home_for(opts.session_id)
        cwd = sanitize_exec_workdir(opts.cwd)
        opts.once_approved = set()
        self.terminal = asyncio.Event()
        self.turn_ready = asyncio.Event()
        self.turn_id = ""
        self.status = None
        self._owner_released = False
        self.invoker.seen.clear()
        schemas = self.invoker.schemas(self.agent, mode, opts)
        fingerprint = hashlib.sha256(
            json.dumps(schemas, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            self.external_session_id
            and self.engine_state.get("codex_tools") != fingerprint
        ):
            raise CodexRecoveryError(
                "Codex tool signatures changed; fork or start a new conversation"
            )
        self.allowed = {spec["name"] for spec in schemas}
        # Native YOLO tools cannot consult arbitrary OpenCuria per-call rules.
        # With any explicit policy layer, disable native shell and keep native
        # edits read-only; managed tools retain the exact common evaluator.
        restricted = bool(
            self.evaluator
            and (
                self.evaluator._global or self.evaluator._agent or self.evaluator._modes
            )
        )
        readonly = (
            self.agent.name in {"plan", "explore"}
            or (self.agent.name == "build" and mode == "plan")
            or restricted
        )
        settings, provider_name, wire_model = runtime_settings(
            self.auth, model, readonly=readonly
        )
        # This is an authorized disposable workspace. Let Codex discover its
        # native project instructions, skills, MCP servers and configuration.
        settings["projects"] = {cwd: {"trust_level": "trusted"}}
        instructions = native_prompt_extension(self.agent)
        if opts.authorize_file_read is None:
            opts.authorize_file_read = self._authorize_read
        content = await user_input(
            prompt,
            accessor=self.accessor,
            opts=opts,
            include_history=not self.external_session_id,
        )
        binary = await self.accessor.ensure_runtime_artifact(
            "codex-agent", CODEX_VERSION
        )
        if isinstance(binary, dict):
            binary = binary.get("path")
        if not isinstance(binary, str) or not binary.startswith("/"):
            raise ValueError("Codex provisioner returned an invalid binary path")
        lease = DesktopLease(
            self.accessor,
            kind="agent",
            owner_id=str(uuid.UUID(opts.session_id)),
            on_failure=asyncio.current_task().cancel,
        )
        rpc = stream = steer = None
        error = None
        result = None
        self.items = {}
        self.streamed = {}
        self.redactors = {}
        self.projector = CodexEventProjector(self)
        self.usage = Usage()
        self.total_text_bytes = 0
        try:
            await lease.reserve()
            await self._owner(lease, "reserved")
            if self.external_session_id:
                if self.session_store is None:
                    raise CodexRecoveryError(
                        "Codex resume checkpoint unavailable; "
                        "fork or start a new conversation"
                    )
                checkpoint = await self.session_store.load(
                    self.external_session_id, home
                )
                if checkpoint["generation"] != self.engine_state.get(
                    "codex_generation"
                ):
                    raise CodexRecoveryError(
                        "Codex checkpoint generation mismatch; "
                        "fork or start a new conversation"
                    )
                await restore_snapshot(self.accessor, home, checkpoint["snapshot"])
            else:
                await state_action(self.accessor, "fresh", home)
            await materialize_skills(self.accessor, home, opts.skills)
            config = "\n".join(
                f"{key} = {_toml(value)}" for key, value in settings.items()
            )
            await state_action(
                self.accessor,
                "config",
                home,
                base64.b64encode(config.encode()).decode(),
            )
            # Empty external ID means a state-only owner-fenced update before
            # the first launch. Execution must not manufacture a thread binding.
            await self._bind(
                provider_id=provider, codex_dirty=True, codex_tools=fingerprint
            )
            argv = runtime_argv(binary, settings)
            stream = await asyncio.wait_for(
                self.accessor.open_process(
                    argv,
                    cwd,
                    {"CODEX_HOME": home, **self.auth.secret_env},
                    owner=lease.owner,
                    timeout=45,
                ),
                45,
            )
            rpc = CodexProtocol(stream, self._server_request)
            await rpc.request(
                "initialize",
                {
                    "clientInfo": {"name": "opencuria", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await rpc.send({"method": "initialized"})
            if self.auth.provider_id == "chatgpt":
                login = await rpc.request(
                    "account/login/start", self.auth.login_data, timeout=10
                )
                if login.get("type") != "chatgptAuthTokens":
                    raise RuntimeError("Codex rejected external ChatGPT authorization")
            params = {
                "model": wire_model,
                "modelProvider": provider_name,
                "cwd": cwd,
                "approvalPolicy": "never",
                "sandbox": "read-only" if readonly else "danger-full-access",
                "config": settings,
                "developerInstructions": instructions,
                "runtimeWorkspaceRoots": [cwd],
            }
            if self.external_session_id:
                response = await rpc.request(
                    "thread/resume", {**params, "threadId": self.external_session_id}
                )
            else:
                response = await rpc.request(
                    "thread/start",
                    {**params, "dynamicTools": schemas, "historyMode": "paginated"},
                )
            thread = response["thread"]
            self.thread_id = str(uuid.UUID(thread["id"]))
            await self._bind(
                provider_id=provider, codex_dirty=True, codex_tools=fingerprint
            )
            # Captures cumulative baseline without counting resumed historical
            # usage. Subsequent updates are absolute totals, not token deltas.
            totals = self.engine_state.get("codex_usage_total") or {}
            self.usage_baseline = Usage(
                prompt_tokens=int(totals.get("inputTokens", 0)),
                completion_tokens=int(totals.get("outputTokens", 0)),
                total_tokens=int(totals.get("totalTokens", 0)),
            )
            self.usage_total = totals
            await self._send({"type": "step_start", "step": 1})
            response = await rpc.request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": content,
                    "model": wire_model,
                    "effort": self.effort or None,
                    "approvalPolicy": "never",
                    "sandboxPolicy": {
                        "type": "readOnly" if readonly else "dangerFullAccess"
                    },
                    "cwd": cwd,
                },
            )
            self.turn_id = response["turn"]["id"]
            self.turn_ready.set()
            steer = asyncio.create_task(self._steer(rpc))
            while not self.terminal.is_set():
                await self._notification(await rpc.event())
                if steer.done():
                    steer.result()
            # Settle an in-flight steer ACK before ending the feed. Cancelling
            # it here could replay a message already accepted upstream.
            await asyncio.wait_for(asyncio.shield(steer), 16)
            if opts.steering:
                opts.steering.end_feed()
            output = "".join(
                value["text"]
                for value in self.items.values()
                if value["type"] == "agentMessage"
            )
            result = RunResult(
                output=redact_text(output, self.secret_values),
                steps=1,
                usage=self.usage,
                cost=0.0,
                finish_reason="aborted" if self.status == "interrupted" else "stop",
                metadata={
                    "harness_id": "codex",
                    "external_session_id": self.thread_id,
                    "cli_version": CODEX_VERSION,
                    "model": model,
                    "reasoning_effort": self.effort,
                    "context_sources": [],
                    "context_truncated": False,
                },
            )
        except BaseException as exc:
            error = exc
            if (
                isinstance(exc, asyncio.CancelledError)
                and rpc
                and self.turn_id
                and not self.terminal.is_set()
            ):

                async def interrupt() -> None:
                    """Request interruption and briefly drain terminal notifications."""
                    try:
                        await rpc.request(
                            "turn/interrupt",
                            {"threadId": self.thread_id, "turnId": self.turn_id},
                            timeout=3,
                        )
                        async with asyncio.timeout(5):
                            while not self.terminal.is_set():
                                await self._notification(await rpc.event())
                    except Exception:
                        pass

                await _finish_cleanup(interrupt())
            if isinstance(exc, CodexProtocolError):
                raise CodexRecoveryError(
                    "Codex protocol failed; execution was not checkpointed. "
                    "Workspace effects may remain; fork or start a new conversation."
                ) from None
            raise
        finally:

            async def cleanup() -> None:
                """Fence process death before taking and publishing a checkpoint."""
                if steer:
                    steer.cancel()
                    await asyncio.gather(steer, return_exceptions=True)
                # Capture protocol health before close deliberately marks it failed.
                # An uncertain RPC (including an in-flight steer) cannot certify a
                # fully observed turn, even if a terminal arrived earlier.
                interrupted_checkpoint = (
                    isinstance(error, asyncio.CancelledError)
                    and self.terminal.is_set()
                    and self.status in {"interrupted", "completed"}
                    and rpc is not None
                    and rpc.failure is None
                    and lease.healthy
                )
                callback_error = None
                try:
                    await asyncio.wait_for(self._owner(lease, "closing"), 5)
                except Exception as exc:
                    callback_error = exc
                transport_error = None
                try:
                    if rpc:
                        await rpc.close()
                    elif stream:
                        await asyncio.wait_for(stream.aclose(), 15)
                except Exception as exc:
                    transport_error = exc
                # Runner release confirms all tree members dead even if EOF or
                # stream close itself failed. No snapshot precedes this fence.
                await asyncio.wait_for(lease.aclose(), 20)
                self._owner_released = True
                try:
                    await asyncio.wait_for(self._owner(lease, "released"), 5)
                except Exception as exc:
                    callback_error = callback_error or exc
                await asyncio.wait_for(self.invoker.finish_abandoned(), 3)
                await asyncio.wait_for(self.projector.finish(), 5)
                if callback_error and error is None:
                    raise callback_error
                if (error is None and result is not None) or (
                    interrupted_checkpoint
                    and callback_error is None
                    and transport_error is None
                ):
                    generation = str(uuid.uuid4())
                    if self.session_store is not None:
                        snapshot = await capture(self.accessor, home)
                        await self.session_store.save(
                            self.thread_id, generation, snapshot
                        )
                    await self._bind(
                        codex_dirty=False,
                        codex_generation=generation,
                        codex_usage_total=self.usage_total,
                    )
                    self.external_session_id = self.thread_id
                    if error is not None:
                        return  # Preserve cancellation; do not invent a usage event.
                    await self._send(
                        {
                            "type": "step_finish",
                            "step": 1,
                            "tokens": {
                                "prompt_tokens": self.usage.prompt_tokens,
                                "completion_tokens": self.usage.completion_tokens,
                                "total_tokens": self.usage.total_tokens,
                            },
                            "cost": 0.0,
                        }
                    )
                elif transport_error and error is None:
                    raise RuntimeError("Codex process cleanup unconfirmed")

            try:
                await _finish_cleanup(cleanup(), propagate_cancel=error is None)
            except BaseException:
                if error is None:
                    raise
        return result

    async def _notification(self, message: dict[str, Any]) -> None:
        """Map only current-turn notifications into canonical harness events."""
        method, params = message.get("method"), message.get("params", {})
        if params.get("threadId") != self.thread_id:
            await self.projector.child_notification(message)
            return
        turn = params.get("turn") or {}
        event_turn = params.get("turnId") or turn.get("id")
        if not self.turn_id or event_turn != self.turn_id or self.terminal.is_set():
            return
        if method == "turn/completed":
            for item in turn.get("items", []):
                await self._item(item, completed=True)
            self.status = turn.get("status")
            self.terminal.set()
            if self.status not in {"completed", "interrupted"}:
                raise CodexRecoveryError(
                    "Codex turn failed; execution was not checkpointed. "
                    "Workspace effects may remain; fork or start a new conversation."
                )
        elif method in {
            "item/agentMessage/delta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
        }:
            key = params.get("itemId")
            text = params.get("delta")
            if isinstance(key, str) and isinstance(text, str):
                field = "text" if method == "item/agentMessage/delta" else "reasoning"
                accumulated = self.streamed.get(key, "") + text
                if len(accumulated.encode()) > 4 * 1024 * 1024:
                    raise ValueError("Codex item text exceeds limit")
                self.total_text_bytes += len(text.encode())
                if self.total_text_bytes > 16 * 1024 * 1024:
                    raise ValueError("Codex aggregate text exceeds limit")
                self.streamed[key] = accumulated
                redactor = self.redactors.setdefault(
                    key, StreamingRedactor(self.secret_values)
                )
                visible = redactor.push(text)
                if visible:
                    await self._send(
                        {"type": "part_updated", "step": 1, "delta": {field: visible}}
                    )
        elif method in {"item/started", "item/completed"}:
            await self._item(
                params.get("item") or {}, completed=method == "item/completed"
            )
        elif method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total") or {}
            if all(
                isinstance(total.get(k, 0), int) and total.get(k, 0) >= 0
                for k in ("inputTokens", "outputTokens", "totalTokens")
            ):
                self.usage_total = total
                self.usage = Usage(
                    prompt_tokens=max(
                        0,
                        total.get("inputTokens", 0) - self.usage_baseline.prompt_tokens,
                    ),
                    completion_tokens=max(
                        0,
                        total.get("outputTokens", 0)
                        - self.usage_baseline.completion_tokens,
                    ),
                    total_tokens=max(
                        0,
                        total.get("totalTokens", 0) - self.usage_baseline.total_tokens,
                    ),
                )
        elif method == "turn/plan/updated" and self.opts.depth == 0:
            await self._send(
                {
                    "type": "todo_updated",
                    "todos": [
                        {
                            "content": str(x.get("step", "")),
                            "status": {
                                "inProgress": "in_progress",
                                "completed": "completed",
                            }.get(x.get("status"), "pending"),
                            "priority": "medium",
                            "order": i,
                        }
                        for i, x in enumerate(params.get("plan", []))
                    ],
                }
            )
        elif method == "turn/diff/updated":
            await self._send(
                {
                    "type": "patch",
                    "step": 1,
                    "call_id": self.turn_id,
                    "tool": "edit",
                    "path": "",
                    "unified_diff": str(params.get("diff") or "")[:131072],
                }
            )

    async def _item(self, item: dict[str, Any], *, completed: bool) -> None:
        """Emit balanced tool lifecycles and authoritative final text snapshots."""
        key, kind = item.get("id"), item.get("type")
        if len(self.items) + len(self.projector.calls) > 4096:
            raise ValueError("Codex item count limit reached")
        if not key or kind == "userMessage":
            return
        if kind in {"agentMessage", "reasoning"} and completed:
            if kind == "agentMessage":
                text = str(item.get("text") or "")
            else:
                text = "\n".join(str(x) for x in item.get("summary", []))
            if len(text.encode()) > 4 * 1024 * 1024:
                raise ValueError("Codex completed item exceeds limit")
            previous = self.items.get(key, {}).get("text", "")
            self.items[key] = {"type": kind, "text": text}
            accumulated = self.streamed.get(key, "")
            redactor = self.redactors.pop(key, None)
            suffix = text[len(accumulated) :] if text.startswith(accumulated) else ""
            visible = redactor.push(suffix, final=True) if redactor else text
            citation = item.get("memoryCitation")
            if kind == "agentMessage" and isinstance(citation, dict):
                await self.projector.item(
                    {"type": "memory", "id": f"{key}:memory", **citation},
                    completed=True,
                )
            # Persisted final snapshots, not the deltas, form RunResult.
            # A non-prefix revision is never appended as duplicate text.
            if visible and not previous:
                await self._send(
                    {
                        "type": "part_updated",
                        "step": 1,
                        "delta": {
                            "text" if kind == "agentMessage" else "reasoning": visible
                        },
                    }
                )
            return
        if kind == "contextCompaction":
            if completed:
                await self._send({"type": "compaction", "auto": True})
            return
        await self.projector.item(item, completed=completed)
