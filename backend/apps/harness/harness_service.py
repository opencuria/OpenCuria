"""
Harness session lifecycle service (M6, additive).

``HarnessService`` owns the persistent side of agent runs while
``HarnessRunner`` (M4/M5) owns the streaming loop. The service:

- creates/gets/lists ``HarnessSession`` rows per workspace,
- persists a user message + assistant message shell on run start,
- builds message history from the DB (not from in-memory parameters),
- starts ``HarnessRunner.run`` as an ``asyncio.Task`` tracked per
  session (one active run per session; a second start is rejected
  with :class:`ConflictError`, surfaced as HTTP 409),
- writes runner events back to ``HarnessMessage``/``HarnessPart`` rows
  (text deltas appended, tool states transitioned, step-finish with
  cost/tokens) and forwards them to subscribed frontend clients via
  ``emit_to_frontend``,
- aborts runs by cancelling the task and marking message/parts aborted,
- binds the M3 permission flow: ask gates persist a
  ``PermissionRequest`` and resolve through ``PermissionService``.

No ORM outside repositories. No real LLM calls (provider injected).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from common.exceptions import ConflictError, NotFoundError

from .agents.definitions import get_agent
from .models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    HarnessSessionStatus,
)
from .permissions.service import PermissionService
from .providers.base import LLMMessage, ProviderAdapter, Usage
from .repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
    TodoRepository,
)
from .runner import HarnessRunner, RunOptions
from .tools import default_tool_registry
from .tools.todos import TodoWriteTool, repository_for_session

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

FRONTEND_EVENT_PART = "harness.part_updated"
FRONTEND_EVENT_PERMISSION = "harness.permission_required"
FRONTEND_EVENT_STATUS = "harness.session_status"
FRONTEND_EVENT_TODO = "harness.todo_updated"
FRONTEND_EVENT_SUBTASK_STARTED = "harness.subtask_started"
FRONTEND_EVENT_SUBTASK_FINISHED = "harness.subtask_finished"


class HarnessService:
    """Persistent session lifecycle + run orchestration for the harness."""

    def __init__(
        self,
        *,
        sessions: type[HarnessSessionRepository] | None = None,
        messages: type[HarnessMessageRepository] | None = None,
        parts: type[HarnessPartRepository] | None = None,
        todos: type[TodoRepository] | None = None,
        permissions: PermissionService | None = None,
        emit: EmitFn | None = None,
        runner_factory: Callable[..., HarnessRunner] | None = None,
        provider_factory: Callable[[uuid.UUID], ProviderAdapter] | None = None,
        accessor_factory: Callable[[str], Any] | None = None,
    ) -> None:
        """Create the service with injectable seams (tests fake them)."""
        self.sessions = sessions or HarnessSessionRepository
        self.messages = messages or HarnessMessageRepository
        self.parts = parts or HarnessPartRepository
        self.todos = todos or TodoRepository
        self.permissions = permissions or PermissionService()
        self._emit = emit
        self._runner_factory = runner_factory
        self._provider_factory = provider_factory
        self._accessor_factory = accessor_factory
        self._tasks: dict[str, asyncio.Task] = {}
        self._pending_permissions: dict[str, asyncio.Future[str]] = {}
        # run context kept in memory: session_id -> dict
        self._runs: dict[str, dict[str, Any]] = {}

    # -- session lifecycle ------------------------------------------------

    def create_session(
        self,
        *,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        prompt: str,
        agent_name: str = "build",
        mode: str = "build",
        model: str = "",
        title: str = "",
        parent_id: uuid.UUID | None = None,
    ) -> HarnessSession:
        """Create a session row (prompt persisted on run start)."""
        if mode not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        get_agent(agent_name)  # raises KeyError for unknown agents
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        session = self.sessions.create(
            workspace_id=workspace_id,
            organization_id=organization_id,
            title=title or _title_from_prompt(prompt),
            mode=mode,
            agent_name=agent_name.strip().lower(),
            model=(model or "").strip(),
            parent_id=parent_id,
        )
        log.info("harness_session_created", session_id=str(session.id))
        return session

    def get_session(self, session_id: uuid.UUID) -> HarnessSession:
        """Return a session or raise NotFoundError."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return session

    def get_session_for_workspace(
        self,
        session_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> HarnessSession:
        """Return a workspace-scoped session or raise NotFoundError."""
        session = self.sessions.get_for_workspace(session_id, workspace_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return session

    def list_sessions(self, workspace_id: uuid.UUID) -> list[HarnessSession]:
        """Return all sessions of a workspace."""
        return self.sessions.list_for_workspace(workspace_id)

    def list_parts(self, session_id: uuid.UUID) -> list[HarnessPart]:
        """Return all parts of a session."""
        self.get_session(session_id)
        return self.parts.list_for_session(session_id)

    def list_messages(self, session_id: uuid.UUID) -> list[HarnessMessage]:
        """Return all messages of a session."""
        self.get_session(session_id)
        return self.messages.list_for_session(session_id)

    def list_todos(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return persisted todos of a session as plain dicts."""
        session = self.get_session(session_id)
        rows = self.todos.list_for_session(session.id)
        return [
            {
                "id": str(row.id),
                "content": row.content,
                "status": row.status,
                "priority": row.priority,
                "order": row.order,
            }
            for row in rows
        ]

    # -- run orchestration --------------------------------------------------

    def is_running(self, session_id: uuid.UUID) -> bool:
        """Return True when a run task is active for *session_id*."""
        task = self._tasks.get(str(session_id))
        return task is not None and not task.done()

    async def start_run(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        organization_id: uuid.UUID | None = None,
        provider: ProviderAdapter | None = None,
        workspace_id: str = "",
    ) -> HarnessMessage:
        """Persist user+assistant messages and start the runner task.

        Raises:
            ConflictError: When a run is already active for the session.
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        key = str(session.id)
        if self.is_running(session.id):
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        org_id = organization_id or session.organization_id
        user_message = await sync_to_async(self.messages.create)(
            session_id=session.id,
            role="user",
            content=prompt.strip(),
        )
        assistant = await sync_to_async(self.messages.create)(
            session_id=session.id,
            role="assistant",
            content="",
            model=session.model or "",
            provider=(provider.name if provider is not None else ""),
        )
        await sync_to_async(self.sessions.mark_status)(
            session, HarnessSessionStatus.BUSY
        )
        session.status = HarnessSessionStatus.BUSY
        history = await self._build_history(session, exclude_message_id=assistant.id)
        run_ctx: dict[str, Any] = {
            "session_id": key,
            "workspace_id": workspace_id or str(session.workspace_id),
            "organization_id": str(org_id),
            "message_id": str(assistant.id),
            "text_part_id": None,
            "reasoning_part_id": None,
            "tool_parts": {},
            "step_parts": {},
            "subtask_parts": {},
        }
        self._runs[key] = run_ctx
        task = asyncio.create_task(
            self._execute_run(
                session=session,
                prompt=prompt.strip(),
                history=history,
                assistant=assistant,
                provider=provider,
                organization_id=org_id,
            )
        )
        self._tasks[key] = task
        task.add_done_callback(lambda _t, _k=key: self._tasks.pop(_k, None))
        await self._emit_frontend(
            FRONTEND_EVENT_STATUS,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": key,
                "status": "busy",
            },
            str(session.workspace_id),
        )
        log.info(
            "harness_run_started",
            session_id=key,
            user_message_id=str(user_message.id),
        )
        return assistant

    async def abort_run(self, session_id: uuid.UUID) -> HarnessSession:
        """Cancel the active run task and mark message/parts aborted."""
        session = self.get_session(session_id)
        key = str(session.id)
        task = self._tasks.get(key)
        if task is None or task.done():
            # Still ensure idle status (e.g. task already finished).
            if session.status != HarnessSessionStatus.IDLE:
                await sync_to_async(self.sessions.mark_status)(
                    session, HarnessSessionStatus.IDLE
                )
                session.status = HarnessSessionStatus.IDLE
            return session
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - abort must not raise
            log.exception("harness_abort_run_error", session_id=key)
        # Belt-and-braces: _execute_run's finally already marked idle;
        # ensure state even if cancellation raced.
        fresh = await sync_to_async(self.sessions.get_by_id)(session.id)
        if fresh is not None:
            session = fresh
        if session.status != HarnessSessionStatus.IDLE:
            await sync_to_async(self.sessions.mark_status)(
                session, HarnessSessionStatus.IDLE
            )
            session.status = HarnessSessionStatus.IDLE
        return session

    async def resolve_permission(
        self, *, session: HarnessSession, request_id: uuid.UUID, response: str
    ) -> dict[str, Any]:
        """Resolve a permission request via the M3 service + wake the loop."""
        record = self.permissions.requests.get_by_id(request_id)
        if record is None or record.status != "pending":
            raise NotFoundError("PermissionRequest", str(request_id))
        if str(record.session_id) != str(session.id):
            raise NotFoundError("PermissionRequest", str(request_id))
        result = await sync_to_async(self.permissions.resolve)(request_id, response)
        future = self._pending_permissions.pop(str(request_id), None)
        if future is not None and not future.done():
            future.set_result(result.decision)
        await self._emit_frontend(
            FRONTEND_EVENT_PERMISSION,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": str(session.id),
                "request_id": str(request_id),
                "decision": result.decision,
                "remember": result.remember,
            },
            str(session.workspace_id),
        )
        return {"decision": result.decision, "remember": result.remember}

    # -- internals ----------------------------------------------------------

    async def _build_history(
        self,
        session: HarnessSession,
        *,
        exclude_message_id: uuid.UUID | None = None,
    ) -> list[LLMMessage]:
        """Build provider history from persisted DB messages."""
        stored = await sync_to_async(self.messages.list_for_session)(session.id)
        history: list[LLMMessage] = []
        for message in stored:
            if exclude_message_id is not None and message.id == (exclude_message_id):
                continue
            if message.role == "user":
                history.append(LLMMessage(role="user", content=message.content or ""))
            elif message.role == "assistant":
                history.append(
                    LLMMessage(role="assistant", content=message.content or None)
                )
        return history

    def _tools_for_session(self, session_id: str):  # type: ignore[no-untyped-def]
        """Build the standard tool registry with the Django todo repo."""
        registry = default_tool_registry()
        try:
            registry._tools["todowrite"] = TodoWriteTool(
                repository=repository_for_session(session_id)
            )
        except KeyError:  # pragma: no cover - registry always has todowrite
            pass
        return registry

    async def _execute_run(
        self,
        *,
        session: HarnessSession,
        prompt: str,
        history: list[LLMMessage],
        assistant: HarnessMessage,
        provider: ProviderAdapter | None,
        organization_id: uuid.UUID,
    ) -> None:
        """Run the loop, persist events, and finalize the assistant message."""
        from .services import ProviderConfigService

        key = str(session.id)
        active_provider = provider
        if active_provider is None:
            if self._provider_factory is not None:
                active_provider = self._provider_factory(organization_id)
            else:
                config_service = ProviderConfigService()
                config = await sync_to_async(config_service.get_config)(organization_id)
                active_provider = config_service.build_adapter(organization_id)
                if session.model:
                    model_default = session.model
                else:
                    model_default = config.default_model
                if not model_default:
                    raise ValueError("No model configured for harness run")
                session.model = model_default
        model = session.model or "default"
        accessor = None
        if self._accessor_factory is not None:
            accessor = await self._accessor_factory(str(session.workspace_id))
        tools = self._tools_for_session(key)
        if self._runner_factory is not None:
            loop_runner = self._runner_factory(
                provider=active_provider,
                tools=tools,
                accessor=accessor,
                emit=lambda event: self._on_runner_event(session, assistant, event),
            )
        else:
            loop_runner = HarnessRunner(
                provider=active_provider,
                tools=tools,
                accessor=accessor,
                emit=lambda event: self._on_runner_event(session, assistant, event),
            )
        opts = RunOptions(
            history=history,
            session_id=key,
            workspace_id=str(session.workspace_id),
            organization_id=str(organization_id),
            on_permission=lambda **kw: self._on_permission(session, assistant, **kw),
        )
        try:
            result = await loop_runner.run(
                prompt, session.agent_name or "build", model, session.mode, opts
            )
            # Text deltas were already appended to the assistant message
            # incrementally (see _persist_part_updated); only the tail
            # after the last delta still needs persisting.
            assistant.refresh_from_db()
            existing = assistant.content or ""
            tail = result.output or ""
            if tail and not existing.endswith(tail):
                remainder = tail
                if existing and tail.startswith(existing):
                    remainder = tail[len(existing) :]
                elif existing and existing.startswith(tail):
                    remainder = ""
                if remainder:
                    await sync_to_async(self.messages.append_content)(
                        assistant, remainder
                    )
            await sync_to_async(self.messages.add_usage)(
                assistant,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
                cost=result.cost,
            )
            await sync_to_async(self.sessions.add_usage)(
                session,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
                cost=result.cost,
            )
            await sync_to_async(self.messages.complete)(
                assistant, finish=result.finish_reason
            )
        except asyncio.CancelledError:
            await sync_to_async(self.messages.complete)(
                assistant, finish="aborted", error="aborted by user"
            )
            await self._fail_open_parts(assistant, state="error", output="aborted")
            raise
        except Exception as exc:
            await sync_to_async(self.messages.complete)(
                assistant, finish="error", error=str(exc)
            )
            await self._fail_open_parts(assistant, state="error", output=str(exc))
            log.exception("harness_run_failed", session_id=key)
            raise
        finally:
            self._runs.pop(key, None)
            self._tasks.pop(key, None)
            await sync_to_async(self.sessions.mark_status)(
                session, HarnessSessionStatus.IDLE
            )
            await self._emit_frontend(
                FRONTEND_EVENT_STATUS,
                {
                    "workspace_id": str(session.workspace_id),
                    "session_id": key,
                    "status": "idle",
                },
                str(session.workspace_id),
            )

    async def _fail_open_parts(
        self, assistant: HarnessMessage, *, state: str, output: str
    ) -> None:
        """Mark pending/running parts of *assistant* as failed/aborted."""
        open_parts = await sync_to_async(
            lambda: list(
                self.parts.model.objects.filter(
                    message_id=assistant.id,
                    state__in=("pending", "running"),
                )
            )
        )()
        for part in open_parts:
            await sync_to_async(self.parts.mark_state)(part, state, output=output)

    async def _on_permission(
        self, session: HarnessSession, assistant: HarnessMessage, **kwargs: Any
    ) -> str:
        """Persist an ask gate as PermissionRequest and wait for resolve."""
        tool = str(kwargs.get("tool", ""))
        action = str(kwargs.get("action", ""))
        title = str(kwargs.get("title", ""))
        call_id = str(kwargs.get("call_id", ""))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        request = await sync_to_async(self.permissions.requests.create)(
            organization_id=session.organization_id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            tool=tool,
            pattern=action,
            title=title,
            message_id=assistant.id,
            call_id=call_id,
        )
        self._pending_permissions[str(request.id)] = future
        await self._emit_frontend(
            FRONTEND_EVENT_PERMISSION,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": str(session.id),
                "request_id": str(request.id),
                "tool": tool,
                "pattern": action,
                "title": title,
                "call_id": call_id,
            },
            str(session.workspace_id),
        )
        try:
            decision = await future
        finally:
            self._pending_permissions.pop(str(request.id), None)
        return "once" if decision == "allow" else "reject"

    async def _on_runner_event(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Persist a runner streaming event and forward it to frontend."""
        etype = str(event.get("type", ""))
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        if etype == "part_updated":
            await self._persist_part_updated(session, assistant, event)
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": event.get("delta", {}),
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "step_start":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="step-start",
                state="running",
                meta={"step": event.get("step")},
            )
            self._runs.get(session_id, {}).get("step_parts", {})[
                str(event.get("step"))
            ] = str(part.id)
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {"step_start": event.get("step")},
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "step_finish":
            tokens = event.get("tokens", {}) or {}
            usage = Usage(
                prompt_tokens=int(tokens.get("prompt_tokens", 0)),
                completion_tokens=int(tokens.get("completion_tokens", 0)),
                total_tokens=int(tokens.get("total_tokens", 0)),
            )
            await sync_to_async(self.messages.add_usage)(
                assistant,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cost=float(event.get("cost", 0.0) or 0.0),
            )
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="step-finish",
                state="completed",
                meta={
                    "step": event.get("step"),
                    "tokens": tokens,
                    "cost": event.get("cost", 0.0),
                },
            )
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {"step_finish": event.get("step")},
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_started":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="tool",
                state="running",
                call_id=str(event.get("call_id", "")),
                title=str(event.get("title", "")),
                input={"tool": event.get("tool", "")},
                meta={"step": event.get("step")},
            )
            self._runs.get(session_id, {}).get("tool_parts", {})[
                str(event.get("call_id", ""))
            ] = str(part.id)
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {
                        "tool_started": event.get("tool", ""),
                        "title": event.get("title", ""),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_completed":
            await self._finish_tool_part(assistant, event, state="completed")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {"tool_completed": event.get("tool", "")},
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "tool_error":
            await self._finish_tool_part(assistant, event, state="error")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {"tool_error": event.get("error", "")},
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "todo_updated":
            await self._emit_frontend(
                FRONTEND_EVENT_TODO,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "todos": event.get("todos", []),
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "subtask_started":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="subtask",
                state="running",
                title=str(event.get("description", "")),
                meta={
                    "subtask_id": event.get("subtask_id", ""),
                    "agent": event.get("agent", ""),
                },
            )
            self._runs.get(session_id, {}).get("subtask_parts", {})[
                str(event.get("subtask_id", ""))
            ] = str(part.id)
            await self._emit_frontend(
                FRONTEND_EVENT_SUBTASK_STARTED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "subtask_id": event.get("subtask_id", ""),
                    "agent": event.get("agent", ""),
                    "description": event.get("description", ""),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "subtask_finished":
            await self._finish_subtask_part(session, assistant, event)
            await self._emit_frontend(
                FRONTEND_EVENT_SUBTASK_FINISHED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "subtask_id": event.get("subtask_id", ""),
                    "status": event.get("status", ""),
                    "summary": event.get("summary", ""),
                },
                workspace_id,
            )
        elif etype == "permission_required":
            # Runner-level gate only notifies; persistence happens in
            # _on_permission when the interactive callback fires.
            await self._emit_frontend(
                FRONTEND_EVENT_PERMISSION,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "tool": event.get("tool", ""),
                    "pattern": event.get("action", ""),
                    "title": event.get("title", ""),
                    "call_id": event.get("call_id", ""),
                    "key": event.get("key", "permission"),
                },
                workspace_id,
            )

    async def _persist_part_updated(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Append text/reasoning deltas to the running text part."""
        delta = event.get("delta", {}) or {}
        text = str(delta.get("text", "") or "")
        reasoning = str(delta.get("reasoning", "") or "")
        key = str(session.id)
        run_ctx = self._runs.get(key, {})
        if text:
            part_id = run_ctx.get("text_part_id")
            if part_id is None:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="text",
                    state="running",
                )
                run_ctx["text_part_id"] = str(part.id)
                part_id = str(part.id)
            part = await sync_to_async(self.parts.model.objects.get)(id=part_id)
            await sync_to_async(self.parts.append_output)(part, text)
            await sync_to_async(self.messages.append_content)(assistant, text)
        if reasoning:
            part_id = run_ctx.get("reasoning_part_id")
            if part_id is None:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="reasoning",
                    state="running",
                )
                run_ctx["reasoning_part_id"] = str(part.id)
                part_id = str(part.id)
            part = await sync_to_async(self.parts.model.objects.get)(id=part_id)
            await sync_to_async(self.parts.append_output)(part, reasoning)

    async def _finish_tool_part(
        self, assistant: HarnessMessage, event: dict[str, Any], *, state: str
    ) -> None:
        """Transition the matching tool part to completed/error."""
        call_id = str(event.get("call_id", ""))
        output = str(event.get("output", "") or event.get("error", "") or "")
        part_id = (
            self._runs.get(str(assistant.session_id), {})
            .get("tool_parts", {})
            .get(call_id)
        )
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.model.objects.filter)(
                id=part_id
            ).first()
        if part is None and call_id:
            part = await sync_to_async(
                self.parts.model.objects.filter(
                    message_id=assistant.id, call_id=call_id
                ).first
            )()
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="tool",
                state="pending",
                call_id=call_id,
                title=str(event.get("tool", "")),
            )
        await sync_to_async(self.parts.mark_state)(
            part, state, output=output or part.output
        )

    async def _finish_subtask_part(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Transition the matching subtask part to completed/error."""
        subtask_id = str(event.get("subtask_id", ""))
        status = str(event.get("status", ""))
        part_id = (
            self._runs.get(str(session.id), {}).get("subtask_parts", {}).get(subtask_id)
        )
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.model.objects.filter)(
                id=part_id
            ).first()
        if part is None:
            return
        state = "completed" if status == "completed" else "error"
        await sync_to_async(self.parts.mark_state)(
            part,
            state,
            meta={
                "subtask_id": subtask_id,
                "agent": event.get("agent", ""),
                "status": status,
            },
        )

    async def _emit_frontend(
        self, event: str, data: dict[str, Any], workspace_id: str
    ) -> None:
        """Forward an event to subscribed frontend clients (never breaks)."""
        try:
            if self._emit is not None:
                await self._emit(event, data)
                return
            from apps.runners.sio_server import emit_to_frontend

            await emit_to_frontend(event, data, workspace_id)
        except Exception:  # pragma: no cover - forwarding is best-effort
            log.warning("harness_frontend_emit_failed", event=event)


def _title_from_prompt(prompt: str) -> str:
    """Derive a short session title from the first prompt line."""
    first_line = (prompt or "").strip().splitlines()[0] if prompt.strip() else ""
    title = first_line.strip()[:80]
    return title or "Harness session"


#: Backwards-friendly alias used by tests for the double-run conflict.
HarnessBusyError = ConflictError
