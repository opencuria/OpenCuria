"""User permission and question interactions shared by all harness engines.

Own durable pending requests, their live waiters and frontend gate events.
Run orchestration supplies event delivery and a run-status query; this service
never imports or manages run execution.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from apps.runners.services.workspace_admission import WorkspaceAdmissionService
from common.exceptions import NotFoundError

from .agents.definitions import get_agent
from .engines.shared.security import redact_action, redact_title
from .models import HarnessMessage, HarnessSession
from .permissions.evaluator import ASK, DENY, PermissionEvaluator
from .permissions.scopes import (
    is_permission_scope,
    permission_display_action,
    permission_scope,
)
from .permissions.service import PermissionService
from .repositories import HarnessSessionRepository, QuestionRequestRepository

log = structlog.get_logger(__name__)

FRONTEND_EVENT_PERMISSION = "harness.permission_required"
FRONTEND_EVENT_QUESTION = "harness.question_required"

EmitFn = Callable[[str, dict[str, Any], str], Awaitable[None]]


def _normalize_question_answers(answers: list[Any]) -> list[Any]:
    """Decode NUL-joined multi-select answers into ``", "``-joined labels.

    NUL cannot be stored in PostgreSQL JSON/text columns and is meaningless
    to the model, so answers are decoded before they are persisted or resumed.
    """
    return [
        ", ".join(part for part in answer.split("\x00") if part)
        if isinstance(answer, str)
        else answer
        for answer in answers
    ]


class InteractionService:
    """Persist, present and settle user interactions without owning agent runs."""

    def __init__(
        self,
        *,
        emit: EmitFn,
        is_running: Callable[[uuid.UUID], bool],
        sessions: type[HarnessSessionRepository] | None = None,
        questions: type[QuestionRequestRepository] | None = None,
        permissions: PermissionService | None = None,
        workspace_admission: WorkspaceAdmissionService | None = None,
    ) -> None:
        """Compose persistence, the workspace fence and the caller's ports."""
        self.sessions = sessions or HarnessSessionRepository
        self.workspace_admission = workspace_admission or WorkspaceAdmissionService()
        self.questions = questions or QuestionRequestRepository
        self.permissions = permissions or PermissionService()
        self._emit = emit
        self._is_running = is_running
        self._pending_permissions: dict[str, asyncio.Future[str]] = {}
        self._pending_questions: dict[str, asyncio.Future[list[Any]]] = {}

    def _session_ids(
        self, session_id: uuid.UUID, include_descendants: bool
    ) -> list[uuid.UUID]:
        """Validate the session and select the requested interaction scope."""
        if self.sessions.get_by_id(session_id) is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return (
            self.sessions.list_descendant_ids(session_id)
            if include_descendants
            else [session_id]
        )

    def cancel_waiters(
        self, permission_ids: list[uuid.UUID], question_ids: list[uuid.UUID]
    ) -> None:
        """Cancel live waiters for requests removed by a committed message edit."""
        for pending, request_ids in (
            (self._pending_permissions, permission_ids),
            (self._pending_questions, question_ids),
        ):
            for request_id in request_ids:
                future = pending.pop(str(request_id), None)
                if future is not None and not future.done():
                    future.cancel()

    def list_pending_permissions(
        self, session_id: uuid.UUID, *, include_descendants: bool = False
    ) -> list[dict[str, Any]]:
        """Return pending permission gates for *session_id* as dicts.

        When *include_descendants* is true, also include pending gates of
        child (and nested) sessions so a parent parts fetch can surface
        subagent asks.
        """
        session_ids = self._session_ids(session_id, include_descendants)
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
                "pattern": permission_display_action(row.pattern),
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
        session_ids = self._session_ids(session_id, include_descendants)
        rows = self.questions.list_pending_for_sessions(session_ids)
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

    async def resolve_permission(
        self, *, session: HarnessSession, request_id: uuid.UUID, response: str
    ) -> dict[str, Any]:
        """Resolve a permission request via the M3 service + wake the loop."""
        record = await sync_to_async(self.permissions.requests.get_by_id)(request_id)
        if record is None or record.status != "pending":
            raise NotFoundError("PermissionRequest", str(request_id))
        if str(record.session_id) != str(session.id):
            raise NotFoundError("PermissionRequest", str(request_id))
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        result = await sync_to_async(self.permissions.resolve)(request_id, response)
        future = self._pending_permissions.pop(str(request_id), None)
        if future is not None and not future.done():
            future.set_result(result.decision)
        await self._emit(
            FRONTEND_EVENT_PERMISSION,
            {
                **(await self._gate_ids(session)),
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
        record = await sync_to_async(self.questions.get_by_id)(question_id)
        if record is None or record.status != "pending":
            raise NotFoundError("QuestionRequest", str(question_id))
        if str(record.session_id) != str(session.id):
            raise NotFoundError("QuestionRequest", str(question_id))
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        status = "rejected" if reject else "answered"
        answers = _normalize_question_answers(list(answers or []))
        await sync_to_async(self.questions.resolve)(
            record,
            answers=answers,
            status=status,
        )
        future = self._pending_questions.pop(str(question_id), None)
        resumed = False
        if future is not None and not future.done():
            if reject:
                future.set_exception(ValueError("Question rejected by user"))
            else:
                future.set_result(answers)
            resumed = True
        if resumed:
            log.info(
                "harness_question_resumed",
                session_id=str(session.id),
                request_id=str(question_id),
                status=status,
            )
        else:
            # No live waiter: the run task is gone (abort/cancel race or
            # process restart dropped the in-memory future). The DB row is
            # resolved, but nothing will wake up — log loudly instead of
            # silently returning 200 while the session stays busy.
            log.warning(
                "harness_question_no_waiter",
                session_id=str(session.id),
                request_id=str(question_id),
                status=status,
                running=self._is_running(session.id),
            )
        await self._emit(
            FRONTEND_EVENT_QUESTION,
            {
                **(await self._gate_ids(session)),
                "request_id": str(question_id),
                "status": status,
            },
            str(session.workspace_id),
        )
        return {
            "request_id": str(question_id),
            "status": status,
            "resumed": resumed,
        }

    async def authorize_file_read(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        path: str,
    ) -> bool:
        """Authorize a Claude user-media read through existing permission gates."""
        action = str(path)
        agent = get_agent(session.agent_name or session.mode or "build")
        agent_decision = PermissionEvaluator(
            agent_rules=dict(agent.permissions or {})
        ).evaluate("read", action, mode=session.mode or "")
        decision = self.permissions.evaluator.evaluate(
            "read", action, mode=session.mode or ""
        )
        # Explicit policy denials remain authoritative, including agent hard bans.
        if DENY in (agent_decision, decision):
            return False
        if await sync_to_async(self.permissions.is_allowlisted)(
            session.organization_id, "read", action, session.workspace_id
        ):
            return True
        if ASK in (agent_decision, decision):
            approved = await self.request_permission(
                session,
                assistant,
                tool="read",
                action=action,
                title=f"Read {action}"[:240],
                key="permission_read",
            )
            return approved in {"once", "always", "allow", "approved", "yes"}
        return True

    async def request_permission(
        self, session: HarnessSession, assistant: HarnessMessage, **kwargs: Any
    ) -> str:
        """Persist an effective ask unless its exact action is allowlisted."""
        tool = str(kwargs.get("tool", ""))
        display_action = redact_action(str(kwargs.get("action", "")))
        provided_action = str(kwargs.get("permission_action", ""))
        if is_permission_scope(provided_action):
            scope = provided_action
        else:
            raw_action = provided_action or str(kwargs.get("action", ""))
            scope = permission_scope(raw_action, display_action)
        title = redact_title(str(kwargs.get("title", "")))
        call_id = str(kwargs.get("call_id", ""))
        allowlisted = await sync_to_async(self.permissions.is_allowlisted)(
            session.organization_id,
            tool,
            scope,
            session.workspace_id,
        )
        if allowlisted:
            return "once"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        request = await sync_to_async(self.permissions.requests.create)(
            organization_id=session.organization_id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            tool=tool,
            pattern=scope,
            title=title,
            message_id=assistant.id,
            call_id=call_id,
        )
        self._pending_permissions[str(request.id)] = future
        await self._emit(
            FRONTEND_EVENT_PERMISSION,
            {
                **(await self._gate_ids(session)),
                "request_id": str(request.id),
                "tool": tool,
                "pattern": display_action,
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

    async def ask_question(
        self, session: HarnessSession, assistant: HarnessMessage, **kwargs: Any
    ) -> list[Any]:
        """Persist a question request and wait for user answers."""
        questions = list(kwargs.get("questions") or [])
        call_id = str(kwargs.get("call_id", ""))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[Any]] = loop.create_future()
        request = await sync_to_async(self.questions.create)(
            organization_id=session.organization_id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            message_id=assistant.id,
            call_id=call_id,
            questions=questions,
        )
        self._pending_questions[str(request.id)] = future
        await self._emit(
            FRONTEND_EVENT_QUESTION,
            {
                **(await self._gate_ids(session)),
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
        except asyncio.CancelledError:
            # Abort while waiting: mark the gate so it never stays
            # pending after the run is gone. resolve_question() would
            # 404 on it otherwise, and the question sheet would linger.
            await sync_to_async(self.questions.resolve)(
                request,
                answers=[],
                status="rejected",
            )
            await self._emit(
                FRONTEND_EVENT_QUESTION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": str(request.id),
                    "status": "rejected",
                },
                str(session.workspace_id),
            )
            raise
        finally:
            self._pending_questions.pop(str(request.id), None)

    async def reject_pending(self, session: HarnessSession) -> None:
        """Reject leftover permission and question gates after abort."""
        pending_perms = await sync_to_async(
            self.permissions.requests.list_pending_for_session
        )(session.id)
        pending_questions = await sync_to_async(
            self.questions.list_pending_for_session
        )(session.id)
        workspace_id = str(session.workspace_id)
        for request in pending_perms:
            await sync_to_async(self.permissions.requests.mark_resolved)(
                request, approved=False, remember="once"
            )
            future = self._pending_permissions.pop(str(request.id), None)
            if future is not None and not future.done():
                future.cancel()
            await self._emit(
                FRONTEND_EVENT_PERMISSION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": str(request.id),
                    "decision": "reject",
                    "remember": "once",
                },
                workspace_id,
            )
        for request in pending_questions:
            await sync_to_async(self.questions.resolve)(
                request,
                answers=[],
                status="rejected",
            )
            future = self._pending_questions.pop(str(request.id), None)
            if future is not None and not future.done():
                future.cancel()
            await self._emit(
                FRONTEND_EVENT_QUESTION,
                {
                    **(await self._gate_ids(session)),
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
            allowlisted = await sync_to_async(self.permissions.is_allowlisted)(
                session.organization_id,
                sibling.tool,
                sibling.pattern,
                session.workspace_id,
            )
            if not allowlisted:
                continue
            await sync_to_async(self.permissions.requests.mark_resolved)(
                sibling, approved=True, remember="always"
            )
            sid = str(sibling.id)
            future = self._pending_permissions.pop(sid, None)
            if future is not None and not future.done():
                future.set_result(decision)
            await self._emit(
                FRONTEND_EVENT_PERMISSION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": sid,
                    "decision": decision,
                    "remember": remember,
                },
                str(session.workspace_id),
            )

    async def _gate_ids(self, session: HarnessSession) -> dict[str, str]:
        """Workspace, session, and root session ids for gate events."""
        root_id = await sync_to_async(self.sessions.get_root_id)(session)
        return {
            "workspace_id": str(session.workspace_id),
            "session_id": str(session.id),
            "root_session_id": str(root_id),
        }

    async def reject_sessions(self, keys: set[str]) -> None:
        """Reject permission and question gates for every session in *keys*."""
        ids: list[uuid.UUID] = []
        for key in keys:
            try:
                ids.append(uuid.UUID(key))
            except ValueError:
                continue
        if not ids:
            return
        rows = await sync_to_async(self.sessions.list_by_ids)(ids)
        for row in rows:
            await self.reject_pending(row)
