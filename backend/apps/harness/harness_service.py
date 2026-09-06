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
import contextvars
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import ThreadSensitiveContext, sync_to_async

from common.exceptions import ConflictError, NotFoundError

from .agents.definitions import get_agent
from .compaction import CHECKPOINT_PREFIX
from .images import hydrate_user_messages
from .models import (
    HarnessMessage,
    HarnessPart,
    HarnessSession,
    HarnessSessionStatus,
)
from .permissions.service import PermissionService
from .providers.base import ChatOptions, LLMMessage, ProviderAdapter, Usage
from .providers.models_catalog import normalize_reasoning_effort
from .repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
    QuestionRequestRepository,
    TodoRepository,
)
from .runner import HarnessRunner, RunOptions
from .tools import default_tool_registry
from .tools.subagents import TaskArgs
from .tools.todos import TodoWriteTool, repository_for_session
from .tools.truncate import truncate_tool_output

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

FRONTEND_EVENT_PART = "harness.part_updated"
FRONTEND_EVENT_PERMISSION = "harness.permission_required"
FRONTEND_EVENT_QUESTION = "harness.question_required"
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
        self._pending_questions: dict[str, asyncio.Future[list[Any]]] = {}
        self._event_locks: dict[str, asyncio.Lock] = {}
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
        reasoning_effort: str = "",
        title: str = "",
        parent_id: uuid.UUID | None = None,
        skill_ids: list[str] | None = None,
        user_id: int | None = None,
    ) -> HarnessSession:
        """Create a session row (prompt persisted on run start)."""
        normalized_mode = (mode or "build").strip().lower()
        if normalized_mode not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        requested = (agent_name or "").strip().lower()
        # Root sessions: agent_name follows mode (plan|build). Child sessions
        # honor subagent agent_name while keeping parent plan|build mode.
        resolved_agent = normalized_mode
        if parent_id is not None and requested:
            definition = get_agent(requested)
            if definition.mode == "hidden":
                raise ValueError(
                    f"Agent '{requested}' cannot be used as a subagent child"
                )
            if definition.mode == "subagent":
                resolved_agent = requested
        get_agent(resolved_agent)  # raises KeyError for unknown agents
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        normalized_skills = _normalize_skill_ids(skill_ids)
        if normalized_skills and user_id is not None:
            resolve_skill_bodies(
                normalized_skills,
                user_id=user_id,
                organization_id=organization_id,
            )
        session = self.sessions.create(
            workspace_id=workspace_id,
            organization_id=organization_id,
            title=title or _title_from_prompt(prompt),
            mode=normalized_mode,
            agent_name=resolved_agent,
            model=(model or "").strip(),
            reasoning_effort=normalize_reasoning_effort(reasoning_effort),
            parent_id=parent_id,
            skill_ids=normalized_skills,
        )
        log.info("harness_session_created", session_id=str(session.id))
        return session

    def get_session(self, session_id: uuid.UUID) -> HarnessSession:
        """Return a session or raise NotFoundError."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return session

    def ensure_user_promptable(self, session: HarnessSession) -> None:
        """Raise when users must not send follow-up prompts to *session*.

        Subagent child sessions (``parent_id`` set) are launched internally
        and are not a user-addressable chat. ``start_run`` itself stays
        allowed so the task tool can start the child run.
        """
        if session.parent_id is not None:
            raise ValueError("Cannot send messages to a subagent session")

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

    def set_mode(self, session_id: uuid.UUID, mode: str) -> HarnessSession:
        """Persist a mode change (plan|build) and align primary agent_name."""
        normalized = (mode or "").strip().lower()
        if normalized not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        session = self.get_session(session_id)
        return self.sessions.set_mode(session, normalized)

    def set_model(self, session_id: uuid.UUID, model: str) -> HarnessSession:
        """Persist a model override for subsequent runs."""
        session = self.get_session(session_id)
        return self.sessions.set_model(session, (model or "").strip())

    def set_reasoning_effort(
        self, session_id: uuid.UUID, reasoning_effort: str
    ) -> HarnessSession:
        """Persist a reasoning-effort override for subsequent runs."""
        session = self.get_session(session_id)
        return self.sessions.set_reasoning_effort(
            session, normalize_reasoning_effort(reasoning_effort)
        )

    def update_title(self, session_id: uuid.UUID, title: str) -> HarnessSession:
        """Rename a session (title only)."""
        session = self.get_session(session_id)
        normalized = (title or "").strip()
        if not normalized:
            raise ValueError("title must not be empty")
        return self.sessions.set_title(session, normalized)

    def update_skill_ids(
        self,
        session_id: uuid.UUID,
        skill_ids: list[str],
        *,
        user_id: int,
        organization_id: uuid.UUID,
    ) -> HarnessSession:
        """Persist skill selection after validating visibility."""
        session = self.get_session(session_id)
        normalized = _normalize_skill_ids(skill_ids)
        if normalized:
            resolve_skill_bodies(
                normalized,
                user_id=user_id,
                organization_id=organization_id,
            )
        return self.sessions.set_skill_ids(session, normalized)

    async def delete_session(self, session_id: uuid.UUID) -> None:
        """Delete a session, aborting any active run first."""
        session = await sync_to_async(self.get_session)(session_id)
        if self.is_running(session.id):
            await self.abort_run(session.id)
        await sync_to_async(self.sessions.delete)(session)

    def list_conversations(
        self,
        *,
        organization_id: uuid.UUID,
        workspace_ids: list[uuid.UUID],
    ) -> list[dict[str, Any]]:
        """List root sessions across owned workspaces (conversations feed)."""
        sessions = self.sessions.list_for_workspaces(workspace_ids)
        workspace_names: dict[str, str] = {}
        if sessions:
            from apps.runners.models import Workspace

            rows = Workspace.objects.filter(
                id__in={session.workspace_id for session in sessions}
            ).only("id", "name")
            workspace_names = {str(row.id): row.name for row in rows}
        unread_map = self.unread_for_sessions(sessions)
        return [
            {
                "session_id": str(session.id),
                "workspace_id": str(session.workspace_id),
                "workspace_name": workspace_names.get(str(session.workspace_id), ""),
                "title": session.title or "",
                "status": session.status,
                "mode": session.mode,
                "agent_name": session.agent_name,
                "model": session.model or "",
                "reasoning_effort": session.reasoning_effort or "",
                "unread": unread_map.get(session.id, False),
                "updated_at": session.updated_at.isoformat(),
            }
            for session in sessions
        ]

    def mark_session_read(self, session_id: uuid.UUID) -> HarnessSession:
        """Persist that the user opened a harness session."""
        session = self.get_session(session_id)
        return self.sessions.mark_read(session)

    def unread_for_sessions(
        self, sessions: list[HarnessSession]
    ) -> dict[uuid.UUID, bool]:
        """Return unread flags keyed by session id."""
        if not sessions:
            return {}
        latest_assistant_at = self.messages.latest_assistant_completed_at_by_session(
            [session.id for session in sessions]
        )
        return {
            session.id: self._is_conversation_unread(
                session,
                latest_assistant_at.get(session.id),
            )
            for session in sessions
        }

    def is_session_unread(self, session: HarnessSession) -> bool:
        """Return whether a single session has unread assistant work."""
        return self.unread_for_sessions([session]).get(session.id, False)

    @staticmethod
    def _is_conversation_unread(
        session: HarnessSession,
        latest_assistant_completed_at,
    ) -> bool:
        """Return True when idle sessions have unread assistant work."""
        if session.status != HarnessSessionStatus.IDLE:
            return False
        if latest_assistant_completed_at is None:
            return False
        if session.last_read_at is None:
            return True
        return latest_assistant_completed_at > session.last_read_at

    def validate_provider_for_run(
        self,
        organization_id: uuid.UUID,
        session: HarnessSession,
        *,
        provider: ProviderAdapter | None = None,
    ) -> str:
        """Ensure provider config and model are present before starting a run.

        Returns the resolved model id for this turn. An empty session model
        (Auto) is filled from the org default without pinning ``session.model``.

        Raises:
            NotFoundError: When no ProviderConfig exists for the org.
            ValueError: When API key or model is missing.
        """
        session_model = (session.model or "").strip()
        if provider is not None or self._provider_factory is not None:
            return session_model
        from .services import ProviderConfigService

        config_service = ProviderConfigService()
        config = config_service.get_config(organization_id)
        if not config.api_key_encrypted:
            raise ValueError("Provider API key not configured")
        model = session_model or (config.default_model or "").strip()
        if not model:
            raise ValueError("No model configured for harness run")
        return model

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

    def list_pending_permissions(
        self, session_id: uuid.UUID, *, include_descendants: bool = False
    ) -> list[dict[str, Any]]:
        """Return pending permission gates for *session_id* as dicts.

        When *include_descendants* is true, also include pending gates of
        child (and nested) sessions so a parent parts fetch can surface
        subagent asks.
        """
        self.get_session(session_id)
        session_ids = (
            self.sessions.list_descendant_ids(session_id)
            if include_descendants
            else [session_id]
        )
        rows = self.permissions.requests.list_pending_for_sessions(session_ids)
        names = {
            row.id: row.agent_name for row in self.sessions.list_by_ids(session_ids)
        }
        return [
            {
                "request_id": str(row.id),
                "session_id": str(row.session_id),
                "workspace_id": str(row.workspace_id) if row.workspace_id else "",
                "tool": row.tool,
                "pattern": row.pattern,
                "title": row.title or "",
                "call_id": row.call_id or "",
                "status": "pending",
                "agent_name": names.get(row.session_id, ""),
            }
            for row in rows
        ]

    def list_pending_questions(
        self, session_id: uuid.UUID, *, include_descendants: bool = False
    ) -> list[dict[str, Any]]:
        """Return pending question gates for *session_id* as dicts.

        When *include_descendants* is true, also include pending gates of
        child (and nested) sessions.
        """
        self.get_session(session_id)
        session_ids = (
            self.sessions.list_descendant_ids(session_id)
            if include_descendants
            else [session_id]
        )
        rows = QuestionRequestRepository.list_pending_for_sessions(session_ids)
        names = {
            row.id: row.agent_name for row in self.sessions.list_by_ids(session_ids)
        }
        return [
            {
                "request_id": str(row.id),
                "session_id": str(row.session_id),
                "workspace_id": str(row.workspace_id) if row.workspace_id else "",
                "questions": list(row.questions or []),
                "call_id": row.call_id or "",
                "status": "pending",
                "agent_name": names.get(row.session_id, ""),
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
        user_id: int | None = None,
        skill_ids: list[str] | None = None,
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
        if skill_ids is not None and user_id is not None:
            session = await sync_to_async(self.update_skill_ids)(
                session.id,
                skill_ids,
                user_id=user_id,
                organization_id=org_id,
            )
        resolved_model = await sync_to_async(self.validate_provider_for_run)(
            org_id, session, provider=provider
        )
        prior_user_messages = await sync_to_async(
            lambda: self.messages.model.objects.filter(
                session_id=session.id, role="user"
            ).count()
        )()
        user_message = await sync_to_async(self.messages.create)(
            session_id=session.id,
            role="user",
            content=prompt.strip(),
        )
        assistant = await sync_to_async(self.messages.create)(
            session_id=session.id,
            role="assistant",
            content="",
            model=resolved_model,
            reasoning_effort=session.reasoning_effort or "",
            provider=(provider.name if provider is not None else ""),
        )
        await sync_to_async(self.sessions.mark_status)(
            session, HarnessSessionStatus.BUSY
        )
        session.status = HarnessSessionStatus.BUSY
        history = await self._build_history(session, exclude_message_id=assistant.id)
        skill_bodies: list[str] = []
        if session.skill_ids and user_id is not None:
            skill_bodies = await sync_to_async(resolve_skill_bodies)(
                list(session.skill_ids or []),
                user_id=user_id,
                organization_id=org_id,
            )
        run_ctx: dict[str, Any] = {
            "session_id": key,
            "workspace_id": workspace_id or str(session.workspace_id),
            "organization_id": str(org_id),
            "message_id": str(assistant.id),
            "user_message_id": str(user_message.id),
            "text_part_id": None,
            "reasoning_part_id": None,
            "tool_parts": {},
            "step_parts": {},
            "subtask_parts": {},
            "skill_bodies": skill_bodies,
        }
        self._runs[key] = run_ctx
        task = self._spawn_background(
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
        task.add_done_callback(lambda t, k=key: self._on_run_task_done(t, k))
        if session.parent_id is None and prior_user_messages == 0:
            self._spawn_background(
                self._generate_title(
                    session_id=session.id,
                    prompt=prompt.strip(),
                    organization_id=org_id,
                )
            )
        await self._emit_frontend(
            FRONTEND_EVENT_STATUS,
            self._session_status_payload(
                session,
                "busy",
                model=resolved_model,
            ),
            str(session.workspace_id),
        )
        log.info(
            "harness_run_started",
            session_id=key,
            user_message_id=str(user_message.id),
        )
        return assistant

    async def abort_run(self, session_id: uuid.UUID) -> HarnessSession:
        """Cancel the active run task, reject pending user gates, and mark aborted."""
        session = await sync_to_async(self.get_session)(session_id)
        children = await sync_to_async(self.sessions.list_children)(session_id)
        key = str(session.id)
        task = self._tasks.get(key)
        if task is None or task.done():
            # Still ensure idle status (e.g. task already finished).
            if session.status != HarnessSessionStatus.IDLE:
                await sync_to_async(self.sessions.mark_status)(
                    session, HarnessSessionStatus.IDLE
                )
                session.status = HarnessSessionStatus.IDLE
        else:
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
        await self._reject_pending_user_gates(session)
        for child in children:
            await self.abort_run(child.id)
        return session

    async def abort_busy_computeruse_for_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[HarnessSession]:
        """Abort busy computer-use sessions for *workspace_id*.

        Parent build/plan sessions are left running. Each aborted child
        still releases its desktop lease in the computer-use ``finally``.
        """
        sessions = await sync_to_async(
            self.sessions.list_busy_computeruse_for_workspace
        )(workspace_id)
        aborted: list[HarnessSession] = []
        for session in sessions:
            aborted.append(await self.abort_run(session.id))
        return aborted

    async def resolve_permission(
        self, *, session: HarnessSession, request_id: uuid.UUID, response: str
    ) -> dict[str, Any]:
        """Resolve a permission request via the M3 service + wake the loop."""
        record = await sync_to_async(self.permissions.requests.get_by_id)(request_id)
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
        if str(response).strip().lower() == "always":
            await self._resolve_sibling_permissions(
                session=session,
                tool=record.tool,
                skip_id=request_id,
                decision=result.decision,
                remember=result.remember,
            )
        return {"decision": result.decision, "remember": result.remember}

    async def resolve_question(
        self,
        *,
        session: HarnessSession,
        question_id: uuid.UUID,
        answers: list[Any],
        reject: bool = False,
    ) -> dict[str, Any]:
        """Resolve a question request and resume the waiting tool."""
        record = await sync_to_async(QuestionRequestRepository.get_by_id)(question_id)
        if record is None or record.status != "pending":
            raise NotFoundError("QuestionRequest", str(question_id))
        if str(record.session_id) != str(session.id):
            raise NotFoundError("QuestionRequest", str(question_id))
        status = "rejected" if reject else "answered"
        await sync_to_async(QuestionRequestRepository.resolve)(
            record,
            answers=list(answers or []),
            status=status,
        )
        future = self._pending_questions.pop(str(question_id), None)
        if future is not None and not future.done():
            if reject:
                future.set_exception(ValueError("Question rejected by user"))
            else:
                future.set_result(list(answers or []))
        await self._emit_frontend(
            FRONTEND_EVENT_QUESTION,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": str(session.id),
                "request_id": str(question_id),
                "status": status,
            },
            str(session.workspace_id),
        )
        return {"request_id": str(question_id), "status": status}

    # -- internals ----------------------------------------------------------

    async def _build_history(
        self,
        session: HarnessSession,
        *,
        exclude_message_id: uuid.UUID | None = None,
    ) -> list[LLMMessage]:
        """Build provider history from persisted messages and parts.

        When a completed compaction part exists, only the checkpoint summary
        plus messages from ``tail_start_id`` onward are sent to the provider
        (OpenCode ``filterCompacted`` equivalent). Full history stays in the DB.
        """
        import json

        stored = await sync_to_async(self.messages.list_for_session)(session.id)
        if exclude_message_id is not None:
            stored = [
                message
                for message in stored
                if message.id != exclude_message_id
            ]
        compaction = await self._find_latest_compaction(stored)
        checkpoint_summary = ""
        compaction_msg_id: uuid.UUID | None = None
        if compaction is not None:
            compaction_msg, compaction_part = compaction
            compaction_msg_id = compaction_msg.id
            checkpoint_summary = str(compaction_part.output or "").strip()
            stored = self._slice_messages_for_compaction(stored, compaction)
        history: list[LLMMessage] = []
        if checkpoint_summary:
            history.append(
                LLMMessage(
                    role="user",
                    content=f"{CHECKPOINT_PREFIX}\n{checkpoint_summary}",
                )
            )
        for message in stored:
            if message.role == "user":
                history.append(
                    LLMMessage(
                        role="user",
                        content=message.content or "",
                        message_id=str(message.id),
                    )
                )
                continue
            if message.role != "assistant":
                continue
            parts = await sync_to_async(self.parts.list_for_message)(message.id)
            tool_parts = [
                part
                for part in parts
                if part.type == "tool"
                and part.state in ("completed", "error")
                and part.call_id
            ]
            assistant_content = self._assistant_history_content(
                message,
                parts,
                summary=checkpoint_summary if message.id == compaction_msg_id else "",
            )
            if not tool_parts:
                history.append(
                    LLMMessage(
                        role="assistant",
                        content=assistant_content,
                        message_id=str(message.id),
                    )
                )
                continue
            tool_calls: list[dict[str, Any]] = []
            for part in tool_parts:
                tool_input = dict(part.input or {})
                tool_name = str(tool_input.get("tool", "") or part.title or "tool")
                raw_args = tool_input.get("arguments", "")
                if isinstance(raw_args, dict):
                    raw_args = json.dumps(raw_args)
                tool_calls.append(
                    {
                        "id": part.call_id,
                        "name": tool_name,
                        "arguments": str(raw_args or "{}"),
                    }
                )
            history.append(
                LLMMessage(
                    role="assistant",
                    content=assistant_content,
                    tool_calls=tool_calls,
                    message_id=str(message.id),
                )
            )
            for part in tool_parts:
                clipped = truncate_tool_output(part.output or "")
                history.append(
                    LLMMessage(
                        role="tool",
                        content=clipped.content,
                        tool_call_id=part.call_id,
                    )
                )
        return history

    async def _find_latest_compaction(
        self, messages: list[HarnessMessage]
    ) -> tuple[HarnessMessage, HarnessPart] | None:
        """Return the latest completed compaction part among *messages*."""
        latest: tuple[HarnessMessage, HarnessPart] | None = None
        for message in messages:
            if message.role != "assistant":
                continue
            parts = await sync_to_async(self.parts.list_for_message)(message.id)
            for part in parts:
                if part.type == "compaction" and part.state == "completed":
                    latest = (message, part)
        return latest

    @staticmethod
    def _slice_messages_for_compaction(
        stored: list[HarnessMessage],
        compaction: tuple[HarnessMessage, HarnessPart],
    ) -> list[HarnessMessage]:
        """Keep tail messages from the latest compaction checkpoint onward."""
        compaction_msg, part = compaction
        tail_start_id = str((part.meta or {}).get("tail_start_id", "") or "").strip()
        if tail_start_id:
            try:
                tail_uuid = uuid.UUID(tail_start_id)
            except ValueError:
                tail_uuid = None
            if tail_uuid is not None:
                for index, message in enumerate(stored):
                    if message.id == tail_uuid:
                        return stored[index:]
        for index, message in enumerate(stored):
            if message.id == compaction_msg.id:
                return stored[index:]
        return stored

    @staticmethod
    def _assistant_history_content(
        message: HarnessMessage,
        parts: list[HarnessPart],
        *,
        summary: str = "",
    ) -> str | None:
        """Resolve assistant text for provider history (never the compaction summary)."""
        text_output = "".join(
            part.output for part in parts if part.type == "text" and part.output
        )
        if text_output:
            return text_output
        content = (message.content or "").strip()
        if not content:
            return None
        if summary and content == summary.strip():
            return None
        return message.content

    @staticmethod
    def _resolve_run_model_limits(
        organization_id: uuid.UUID, model_id: str
    ) -> tuple[int, int]:
        """Return ``(context_length, max_output_tokens)`` from the org catalog."""
        from .services import ProviderConfigService

        try:
            models = ProviderConfigService().list_models(organization_id)
        except Exception:
            return 0, 0
        for model in models:
            if model.id == model_id:
                return model.context_length, model.max_output_tokens
        return 0, 0

    @staticmethod
    def _last_assistant_step_tokens(
        session_id: uuid.UUID, exclude_message_id: uuid.UUID
    ) -> tuple[int, int, int]:
        """Return last-step prompt/completion/total from the prior assistant turn."""
        prev = (
            HarnessMessage.objects.filter(
                session_id=session_id,
                role="assistant",
            )
            .exclude(id=exclude_message_id)
            .order_by("-created_at")
            .first()
        )
        if prev is None:
            return 0, 0, 0
        part = (
            HarnessPart.objects.filter(
                message_id=prev.id,
                type="step-finish",
            )
            .order_by("-created_at")
            .first()
        )
        if part is not None:
            meta = dict(part.meta or {})
            tokens = dict(meta.get("tokens") or {})
            return (
                int(tokens.get("prompt_tokens", 0) or 0),
                int(tokens.get("completion_tokens", 0) or 0),
                int(tokens.get("total_tokens", 0) or 0),
            )
        message_tokens = dict(prev.tokens or {})
        return (
            int(message_tokens.get("prompt", 0) or 0),
            int(message_tokens.get("completion", 0) or 0),
            int(message_tokens.get("total", 0) or 0),
        )

    def _spawn_background(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        """Start *coro* detached from the HTTP request's asgiref executor.

        ``sync_to_async`` inherits the request's ``CurrentThreadExecutor``
        via contextvars. When the response is sent that executor is marked
        broken, so a fire-and-forget run would fail with
        ``CurrentThreadExecutor already quit or is broken``. A fresh
        context plus ``ThreadSensitiveContext`` gives the task its own
        ORM thread for the rest of the run.
        """
        return asyncio.create_task(
            self._await_in_thread_sensitive_context(coro),
            context=contextvars.Context(),
        )

    def _on_run_task_done(self, task: asyncio.Task[None], key: str) -> None:
        """Drop the run task and retrieve any leftover exception."""
        self._tasks.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "harness_background_task_failed",
                session_id=key,
                error=str(exc),
            )

    @staticmethod
    async def _await_in_thread_sensitive_context(coro: Awaitable[None]) -> None:
        """Await *coro* with a dedicated thread-sensitive ORM executor."""
        async with ThreadSensitiveContext():
            await coro

    def _tools_for_session(self, session_id: str, agent_name: str = "build"):  # type: ignore[no-untyped-def]
        """Build the tool registry for a session agent."""
        from .tools import computeruse_tool_registry, default_tool_registry

        if (agent_name or "").strip().lower() == "computeruse":
            return computeruse_tool_registry()
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
        small_model = ""
        computer_use_model = ""
        default_model = ""
        if active_provider is None:
            if self._provider_factory is not None:
                active_provider = self._provider_factory(organization_id)
            else:
                config_service = ProviderConfigService()
                config = await sync_to_async(config_service.get_config)(organization_id)
                active_provider = config_service.adapter_from_config(config)
                small_model = (config.small_model or "").strip()
                computer_use_model = (config.computer_use_model or "").strip()
                default_model = (config.default_model or "").strip()
                if session.model:
                    model_default = session.model
                else:
                    model_default = config.default_model
                if not model_default:
                    raise ValueError("No model configured for harness run")
                session.model = model_default
        model = session.model or "default"
        context_length = 0
        model_max_output_tokens = 0
        last_step_prompt_tokens = 0
        last_step_completion_tokens = 0
        last_step_total_tokens = 0
        context_length, model_max_output_tokens = await sync_to_async(
            self._resolve_run_model_limits
        )(organization_id, model)
        last_step_prompt_tokens, last_step_completion_tokens, last_step_total_tokens = (
            await sync_to_async(self._last_assistant_step_tokens)(
                session.id, assistant.id
            )
        )
        accessor = None
        if self._accessor_factory is not None:
            accessor = await self._accessor_factory(str(session.workspace_id))
        if accessor is not None:
            history = await hydrate_user_messages(history, accessor)
        tools = self._tools_for_session(key, session.agent_name or "build")
        if self._runner_factory is not None:
            loop_runner = self._runner_factory(
                provider=active_provider,
                tools=tools,
                accessor=accessor,
                emit=lambda event: self._on_runner_event(session, assistant, event),
            )
        else:
            effort = (session.reasoning_effort or "").strip() or None
            loop_runner = HarnessRunner(
                provider=active_provider,
                tools=tools,
                accessor=accessor,
                emit=lambda event: self._on_runner_event(session, assistant, event),
                chat_options=ChatOptions(reasoning_effort=effort),
            )
        opts = RunOptions(
            history=history,
            session_id=key,
            workspace_id=str(session.workspace_id),
            organization_id=str(organization_id),
            small_model=small_model,
            current_user_message_id=str(
                self._runs.get(key, {}).get("user_message_id", "")
            ),
            skills=list(self._runs.get(key, {}).get("skill_bodies", [])),
            context_length=context_length,
            max_output_tokens=model_max_output_tokens,
            last_step_prompt_tokens=last_step_prompt_tokens,
            last_step_completion_tokens=last_step_completion_tokens,
            last_step_total_tokens=last_step_total_tokens,
            on_permission=lambda **kw: self._on_permission(session, assistant, **kw),
            on_question=lambda **kw: self._on_question(session, assistant, **kw),
            run_subagent=lambda args, ctx, subtask_id: self._run_subagent_tool(
                parent=session,
                args=args,
                ctx=ctx,
                subtask_id=subtask_id,
                organization_id=organization_id,
                small_model=small_model,
                computer_use_model=computer_use_model,
                default_model=default_model,
            ),
        )
        try:
            result = await loop_runner.run(
                prompt, session.agent_name or "build", model, session.mode, opts
            )
            # Text deltas were already appended to the assistant message
            # incrementally (see _persist_part_updated); only the tail
            # after the last delta still needs persisting.
            await sync_to_async(assistant.refresh_from_db)()
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
            # Message usage is accumulated per step_finish so abort still
            # keeps completed steps. Session usage is applied once here.
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
            await self._settle_open_stream_parts(assistant)
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
        finally:
            self._runs.pop(key, None)
            self._tasks.pop(key, None)
            self._event_locks.pop(key, None)
            await sync_to_async(self.sessions.mark_status)(
                session, HarnessSessionStatus.IDLE
            )
            await self._emit_frontend(
                FRONTEND_EVENT_STATUS,
                self._session_status_payload(
                    session,
                    "idle",
                    model=assistant.model or "",
                ),
                str(session.workspace_id),
            )

    async def _settle_open_stream_parts(self, assistant: HarnessMessage) -> None:
        """Mark leftover running text/reasoning parts completed.

        The runner closes these in-memory on ``step_finish``, but the DB
        rows stay ``running`` unless we persist the completed state.
        """
        open_parts = await sync_to_async(
            lambda: list(
                self.parts.model.objects.filter(
                    message_id=assistant.id,
                    type__in=("text", "reasoning"),
                    state__in=("pending", "running"),
                )
            )
        )()
        for part in open_parts:
            await sync_to_async(self.parts.mark_state)(part, "completed")

    async def _fail_open_parts(
        self, assistant: HarnessMessage, *, state: str, output: str
    ) -> None:
        """Mark pending/running parts of *assistant* as failed/aborted.

        Text and reasoning keep their streamed content and are marked
        completed — aborting a run does not abort a thought that already
        happened. Tool/subtask parts become *state* and keep any partial
        output already written.
        """
        open_parts = await sync_to_async(
            lambda: list(
                self.parts.model.objects.filter(
                    message_id=assistant.id,
                    state__in=("pending", "running"),
                )
            )
        )()
        for part in open_parts:
            if part.type in ("text", "reasoning"):
                await sync_to_async(self.parts.mark_state)(part, "completed")
                continue
            await sync_to_async(self.parts.mark_state)(
                part, state, output=part.output or output
            )

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
                "agent_name": session.agent_name or "",
            },
            str(session.workspace_id),
        )
        try:
            decision = await future
        finally:
            self._pending_permissions.pop(str(request.id), None)
        return "once" if decision == "allow" else "reject"

    async def _on_question(
        self, session: HarnessSession, assistant: HarnessMessage, **kwargs: Any
    ) -> list[Any]:
        """Persist a question request and wait for user answers."""
        questions = list(kwargs.get("questions") or [])
        call_id = str(kwargs.get("call_id", ""))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[Any]] = loop.create_future()
        request = await sync_to_async(QuestionRequestRepository.create)(
            organization_id=session.organization_id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            message_id=assistant.id,
            call_id=call_id,
            questions=questions,
        )
        self._pending_questions[str(request.id)] = future
        await self._emit_frontend(
            FRONTEND_EVENT_QUESTION,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": str(session.id),
                "request_id": str(request.id),
                "questions": questions,
                "call_id": call_id,
                "status": "pending",
                "agent_name": session.agent_name or "",
            },
            str(session.workspace_id),
        )
        try:
            return await future
        finally:
            self._pending_questions.pop(str(request.id), None)

    async def _on_runner_event(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Persist a runner streaming event and forward it to frontend."""
        async with self._event_lock(str(session.id)):
            await self._persist_runner_event(session, assistant, event)

    async def _persist_runner_event(
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
                cost=float(event.get("cost", 0.0) or 0.0),
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
                    "delta": {
                        "step_finish": event.get("step"),
                        "cost": event.get("cost", 0.0),
                        "tokens": tokens,
                    },
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
                input={
                    "tool": event.get("tool", ""),
                    "arguments": event.get("arguments", ""),
                },
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
                        "call_id": event.get("call_id", ""),
                        "arguments": event.get("arguments", ""),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_completed":
            part = await self._finish_tool_part(assistant, event, state="completed")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {
                        "tool_completed": event.get("tool", ""),
                        "call_id": event.get("call_id", ""),
                        "output": event.get("output", ""),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id) if part is not None else None,
                },
                workspace_id,
            )
        elif etype == "tool_error":
            part = await self._finish_tool_part(assistant, event, state="error")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {
                        "tool_error": event.get("error", ""),
                        "call_id": event.get("call_id", ""),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id) if part is not None else None,
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
                    "child_session_id": event.get("child_session_id", ""),
                    "model": event.get("model", ""),
                    "reasoning_effort": event.get("reasoning_effort", ""),
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
                    "child_session_id": event.get("child_session_id", ""),
                    "agent": event.get("agent", ""),
                    "description": event.get("description", ""),
                    "part_id": str(part.id),
                    "model": event.get("model", ""),
                    "reasoning_effort": event.get("reasoning_effort", ""),
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
                    "child_session_id": event.get("child_session_id", ""),
                    "status": event.get("status", ""),
                    "summary": event.get("summary", ""),
                },
                workspace_id,
            )
        elif etype == "patch":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="patch",
                state="completed",
                call_id=str(event.get("call_id", "")),
                title=str(event.get("title", "")),
                output=str(event.get("unified_diff", "") or ""),
                meta={
                    "path": event.get("path", ""),
                    "step": event.get("step"),
                    "tool": event.get("tool", ""),
                    "old_content": event.get("old_content", ""),
                    "new_content": event.get("new_content", ""),
                },
            )
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {"patch": event.get("path", "")},
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "compaction":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="compaction",
                state="completed",
                title="Session compacted",
                output=str(event.get("summary", "") or ""),
                meta={
                    "auto": bool(event.get("auto", True)),
                    "overflow": bool(event.get("overflow", False)),
                    "tail_start_id": str(event.get("tail_start_id", "") or ""),
                },
            )
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "delta": {"compaction": True},
                    "part_id": str(part.id),
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
    ) -> HarnessPart:
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
            part = await sync_to_async(
                self.parts.model.objects.filter(id=part_id).first
            )()
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
        return part

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
            part = await sync_to_async(
                self.parts.model.objects.filter(id=part_id).first
            )()
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
                "child_session_id": event.get("child_session_id", ""),
            },
        )

    def _event_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock for runner-event persistence."""
        lock = self._event_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._event_locks[session_id] = lock
        return lock

    async def _reject_pending_user_gates(self, session: HarnessSession) -> None:
        """Reject leftover permission and question gates after abort."""
        pending_perms = await sync_to_async(
            self.permissions.requests.list_pending_for_session
        )(session.id)
        pending_questions = await sync_to_async(
            QuestionRequestRepository.list_pending_for_session
        )(session.id)
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        for request in pending_perms:
            await sync_to_async(self.permissions.requests.mark_resolved)(
                request, approved=False, remember="once"
            )
            future = self._pending_permissions.pop(str(request.id), None)
            if future is not None and not future.done():
                future.cancel()
            await self._emit_frontend(
                FRONTEND_EVENT_PERMISSION,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "request_id": str(request.id),
                    "decision": "reject",
                    "remember": "once",
                },
                workspace_id,
            )
        for request in pending_questions:
            await sync_to_async(QuestionRequestRepository.resolve)(
                request,
                answers=[],
                status="rejected",
            )
            future = self._pending_questions.pop(str(request.id), None)
            if future is not None and not future.done():
                future.cancel()
            await self._emit_frontend(
                FRONTEND_EVENT_QUESTION,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "request_id": str(request.id),
                    "status": "rejected",
                },
                workspace_id,
            )

    async def _resolve_sibling_permissions(
        self,
        *,
        session: HarnessSession,
        tool: str,
        skip_id: uuid.UUID,
        decision: str,
        remember: str,
    ) -> None:
        """Approve remaining pending asks for the same tool in this session."""
        pending = await sync_to_async(
            self.permissions.requests.list_pending_for_session
        )(session.id, tool=tool)
        for sibling in pending:
            if sibling.id == skip_id:
                continue
            await sync_to_async(self.permissions.requests.mark_resolved)(
                sibling, approved=True, remember="always"
            )
            sid = str(sibling.id)
            future = self._pending_permissions.pop(sid, None)
            if future is not None and not future.done():
                future.set_result(decision)
            await self._emit_frontend(
                FRONTEND_EVENT_PERMISSION,
                {
                    "workspace_id": str(session.workspace_id),
                    "session_id": str(session.id),
                    "request_id": sid,
                    "decision": decision,
                    "remember": remember,
                },
                str(session.workspace_id),
            )

    def _session_status_payload(
        self,
        session: HarnessSession,
        status: str,
        *,
        model: str = "",
    ) -> dict[str, Any]:
        """Shape a ``harness.session_status`` frontend payload."""
        return {
            "workspace_id": str(session.workspace_id),
            "session_id": str(session.id),
            "status": status,
            "model": (model or session.model or "").strip(),
            "reasoning_effort": session.reasoning_effort or "",
        }

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
            log.warning("harness_frontend_emit_failed", event_name=event)

    async def _generate_title(
        self,
        *,
        session_id: uuid.UUID,
        prompt: str,
        organization_id: uuid.UUID,
    ) -> None:
        """Run the hidden title agent asynchronously (never raises)."""
        try:
            from .services import ProviderConfigService

            config_service = ProviderConfigService()
            config = await sync_to_async(config_service.get_config)(organization_id)
            small_model = (config.small_model or "").strip()
            if not small_model:
                return
            if self._provider_factory is not None:
                provider = self._provider_factory(organization_id)
            else:
                provider = config_service.adapter_from_config(config)
            runner = HarnessRunner(provider=provider, tools=default_tool_registry())
            result = await runner.run(
                prompt,
                "title",
                small_model,
                "build",
                RunOptions(auto_approve=True),
            )
            title = _normalize_generated_title(result.output or "")
            if not title:
                return
            session = await sync_to_async(self.sessions.get_by_id)(session_id)
            if session is None:
                return
            auto_title = _title_from_prompt(prompt)
            if (session.title or "").strip() != auto_title:
                return
            await sync_to_async(self.sessions.set_title)(session, title)
        except Exception:  # pragma: no cover - title must never break runs
            log.warning("harness_title_generation_failed", session_id=str(session_id))

    async def _run_subagent_tool(
        self,
        *,
        parent: HarnessSession,
        args: TaskArgs,
        ctx: Any,
        subtask_id: str,
        organization_id: uuid.UUID,
        small_model: str,
        computer_use_model: str = "",
        default_model: str = "",
    ) -> Any:
        """Create a child session, run it to completion, return tool output."""
        from .computeruse_loop import sanitize_run_id, truncate_task_output
        from .tools.base import ToolError, ToolResult
        from .tools.subagents import TASK_OUTPUT_MAX_CHARS

        agent = (args.agent or args.subagent_type or "general").strip().lower()
        if agent == "computeruse":
            model = (
                args.model_override
                or computer_use_model
                or ctx.model
                or parent.model
                or default_model
                or ""
            ).strip()
        else:
            model = (args.model_override or ctx.model or parent.model or "").strip()
        if not model:
            raise ToolError(
                "No model available for subagent run",
                tool="task",
            )
        parent_ctx = self._runs.get(str(parent.id), {})
        message_id = parent_ctx.get("message_id")
        if not message_id:
            raise ToolError(
                "Parent assistant message missing for subagent run",
                tool="task",
            )
        parent_assistant = await sync_to_async(self.messages.model.objects.get)(
            id=message_id
        )
        child = await sync_to_async(self.create_session)(
            workspace_id=parent.workspace_id,
            organization_id=parent.organization_id,
            prompt=args.prompt,
            agent_name=agent,
            mode=parent.mode,
            model=model,
            title=args.description[:255],
            parent_id=parent.id,
        )
        await self._on_runner_event(
            parent,
            parent_assistant,
            {
                "type": "subtask_started",
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "description": args.description,
                "model": child.model or "",
                "reasoning_effort": child.reasoning_effort or "",
            },
        )
        status = "completed"
        child_task: asyncio.Task[None] | None = None
        try:
            assistant = await self.start_run(
                child,
                args.prompt,
                organization_id=organization_id,
                workspace_id=str(parent.workspace_id),
            )
            child_task = self._tasks.get(str(child.id))
            if child_task is not None:
                await child_task
            assistant = await sync_to_async(self.messages.model.objects.get)(
                id=assistant.id
            )
            output = assistant.content or ""
            if assistant.finish in ("error", "aborted"):
                status = "error"
        except asyncio.CancelledError:
            tracked = child_task or self._tasks.get(str(child.id))
            if tracked is not None and not tracked.done():
                tracked.cancel()
                try:
                    await tracked
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        except Exception as exc:
            await self._on_runner_event(
                parent,
                parent_assistant,
                {
                    "type": "subtask_finished",
                    "subtask_id": subtask_id,
                    "child_session_id": str(child.id),
                    "agent": agent,
                    "status": "error",
                    "summary": str(exc)[:500],
                },
            )
            raise ToolError(f"Subagent '{agent}' failed: {exc}", tool="task") from exc
        if agent == "computeruse":
            display_output, truncated = truncate_task_output(
                output,
                sanitize_run_id(str(child.id)),
                TASK_OUTPUT_MAX_CHARS,
            )
        else:
            truncated = len(output) > TASK_OUTPUT_MAX_CHARS
            display_output = output
            if truncated:
                display_output = (
                    output[:TASK_OUTPUT_MAX_CHARS]
                    + f"\n…[truncated {len(output)} chars total]"
                )
        await self._on_runner_event(
            parent,
            parent_assistant,
            {
                "type": "subtask_finished",
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "status": status,
                "summary": output[:500],
            },
        )
        return ToolResult(
            output=display_output,
            truncated=truncated,
            metadata={
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "status": status,
            },
        )


def _normalize_skill_ids(skill_ids: list[str] | None) -> list[str]:
    """Normalize skill ID strings (deduped, trimmed)."""
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in skill_ids or []:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def resolve_skill_bodies(
    skill_ids: list[str],
    *,
    user_id: int,
    organization_id: uuid.UUID,
) -> list[str]:
    """Load skill bodies visible to *user_id* in *organization_id*.

    Raises:
        ValueError: When an ID is invalid or not accessible.
    """
    from apps.skills.repositories import SkillRepository

    if not skill_ids:
        return []
    parsed: list[uuid.UUID] = []
    for raw in skill_ids:
        try:
            parsed.append(uuid.UUID(str(raw).strip()))
        except ValueError:
            raise ValueError(f"Invalid skill_id: {raw}")
    visible = set(
        SkillRepository.list_for_user_in_org(user_id, organization_id).values_list(
            "id", flat=True
        )
    )
    by_id = {skill.id: skill for skill in SkillRepository.get_many_by_ids(parsed)}
    bodies: list[str] = []
    for skill_id in parsed:
        if skill_id not in visible:
            raise ValueError(f"Skill not found or not accessible: {skill_id}")
        skill = by_id.get(skill_id)
        if skill is None:
            raise ValueError(f"Skill not found or not accessible: {skill_id}")
        bodies.append(f"## {skill.name}\n{skill.body.strip()}")
    return bodies


def _normalize_generated_title(text: str) -> str:
    """Clamp a generated title to at most eight words and 255 chars."""
    title = " ".join((text or "").strip().split())
    words = title.split()
    if len(words) > 8:
        title = " ".join(words[:8])
    return title[:255]


def _title_from_prompt(prompt: str) -> str:
    """Derive a short session title from the first prompt line."""
    first_line = (prompt or "").strip().splitlines()[0] if prompt.strip() else ""
    title = first_line.strip()[:80]
    return title or "Harness session"


_default_harness_service: HarnessService | None = None


def create_default_harness_service() -> HarnessService:
    """Build the production service with runner-backed workspace access.

    REST and MCP both use :func:`get_harness_service` so abort, permissions,
    and in-flight runs share one task registry.
    """
    from apps.runners.sio_server import get_runner_service

    from .access.runner_accessor import create_harness_accessor

    runner_service = get_runner_service()

    async def accessor_factory(workspace_id: str) -> Any:
        return await create_harness_accessor(runner_service, workspace_id)

    return HarnessService(accessor_factory=accessor_factory)


def get_harness_service() -> HarnessService:
    """Return the process-wide HarnessService singleton."""
    global _default_harness_service
    if _default_harness_service is None:
        _default_harness_service = create_default_harness_service()
    return _default_harness_service


def reset_default_harness_service() -> None:
    """Drop the process singleton (tests)."""
    global _default_harness_service
    _default_harness_service = None


#: Backwards-friendly alias used by tests for the double-run conflict.
HarnessBusyError = ConflictError
