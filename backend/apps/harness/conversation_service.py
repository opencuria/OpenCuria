"""Saved conversation feeds, unread state, attention and notice dismissal.

REST and MCP share these operations without constructing a live agent harness.
Repositories own persistence; conversation events publish feed invalidations.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from common.exceptions import NotFoundError

from .conversation_events import ConversationEvents
from .models import HarnessMessage, HarnessSession, HarnessSessionStatus
from .permissions.repositories import PermissionRequestRepository
from .repositories import (
    HarnessMessageRepository,
    HarnessSessionRepository,
    QuestionRequestRepository,
)


class ConversationService:
    """Read and update saved conversation state independently of live runs."""

    def __init__(
        self,
        *,
        sessions: type[HarnessSessionRepository] | None = None,
        messages: type[HarnessMessageRepository] | None = None,
        permission_requests: type[PermissionRequestRepository] | None = None,
        questions: type[QuestionRequestRepository] | None = None,
        events: ConversationEvents | None = None,
    ) -> None:
        """Inject persistence and notifications without depending on execution."""
        self.sessions = sessions or HarnessSessionRepository
        self.messages = messages or HarnessMessageRepository
        self.permission_requests = permission_requests or PermissionRequestRepository
        self.questions = questions or QuestionRequestRepository
        self.events = events or ConversationEvents()

    def get_session(self, session_id: uuid.UUID) -> HarnessSession:
        """Return a saved session or raise NotFoundError."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return session

    def list_conversations(
        self,
        *,
        organization_id: uuid.UUID,
        workspace_ids: list[uuid.UUID],
    ) -> list[dict[str, Any]]:
        """List root sessions across owned workspaces (conversations feed)."""
        sessions = self.sessions.list_for_workspaces(workspace_ids)
        unread_map = self.unread_for_sessions(sessions)
        attention_map = self.attention_for_sessions(sessions)
        return [
            {
                "session_id": str(session.id),
                "workspace_id": str(session.workspace_id),
                "workspace_name": session.workspace_name or "",
                "title": session.title or "",
                "status": session.status,
                "mode": session.mode,
                "agent_name": session.agent_name,
                "harness_id": session.harness_id or "native",
                "model": session.model or "",
                "reasoning_effort": session.reasoning_effort or "",
                "unread": unread_map.get(session.id, False),
                "manual_unread": session.manual_unread_at is not None,
                "needs_attention": attention_map.get(session.id, {}).get(
                    "needs_attention", False
                ),
                "attention_kind": attention_map.get(session.id, {}).get(
                    "attention_kind", ""
                ),
                "updated_at": session.updated_at.isoformat(),
                "last_message_at": (
                    session.last_message_at or session.created_at
                ).isoformat(),
            }
            for session in sessions
        ]

    def mark_session_read(self, session_id: uuid.UUID) -> HarnessSession:
        """Persist that the user opened a harness session."""
        session = self.get_session(session_id)
        updated = self.sessions.mark_read(session)
        if updated.parent_id is None:
            self.events.emit_sync(updated.workspace_id)
        return updated

    def mark_session_unread(self, session_id: uuid.UUID) -> HarnessSession:
        """Persist that the user explicitly marked a session unread."""
        session = self.get_session(session_id)
        updated = self.sessions.mark_unread(session)
        if updated.parent_id is None:
            self.events.emit_sync(updated.workspace_id)
        return updated

    def dismiss_notice(
        self, session_id: uuid.UUID, message_id: uuid.UUID
    ) -> HarnessMessage:
        """Dismiss the stopped/failed notice of one session message."""
        session = self.get_session(session_id)
        message = self.messages.get_by_id(message_id)
        if message is None or message.session_id != session.id:
            raise NotFoundError("HarnessMessage", str(message_id))
        return self.messages.dismiss_notice(message)

    def discard_unstarted(
        self,
        session_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        release: Callable[[], bool],
    ) -> bool:
        """Delete an empty, idle root chat whose first run was never admitted.

        The creator rolls back its own admission: ``release`` detaches the
        creator's references under the admission locks and returns False to
        keep the chat. Returns whether the chat was deleted; the caller
        publishes the feed change through its conversation events.
        """
        return self.sessions.delete_unstarted_root(
            session_id,
            workspace_id=workspace_id,
            organization_id=organization_id,
            release=release,
        )

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
        latest_assistant_completed_at: datetime | None,
    ) -> bool:
        """Return True when the session is manually unread or has new work."""
        if session.manual_unread_at is not None:
            return True
        if session.status != HarnessSessionStatus.IDLE:
            return False
        if latest_assistant_completed_at is None:
            return False
        if session.last_read_at is None:
            return True
        return latest_assistant_completed_at > session.last_read_at

    def attention_for_sessions(
        self, sessions: list[HarnessSession]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        """Return pending-gate attention flags keyed by session id.

        Child (subagent) gates count toward every ancestor in *sessions*,
        so a root conversation is marked when a descendant is waiting.
        """
        if not sessions:
            return {}
        workspace_ids = list({session.workspace_id for session in sessions})
        id_parents = self.sessions.list_id_parent_for_workspaces(workspace_ids)
        children_map: dict[uuid.UUID, list[uuid.UUID]] = {}
        all_ids: list[uuid.UUID] = []
        for session_id, parent_id in id_parents:
            all_ids.append(session_id)
            if parent_id is None:
                continue
            children_map.setdefault(parent_id, []).append(session_id)
        pending_perm_ids = {
            row.session_id
            for row in self.permission_requests.list_pending_for_sessions(all_ids)
        }
        pending_question_ids = {
            row.session_id for row in self.questions.list_pending_for_sessions(all_ids)
        }
        result: dict[uuid.UUID, dict[str, Any]] = {}
        for session in sessions:
            tree = _descendant_ids(session.id, children_map)
            has_permission = any(sid in pending_perm_ids for sid in tree)
            has_question = any(sid in pending_question_ids for sid in tree)
            if has_permission and has_question:
                kind = "both"
            elif has_permission:
                kind = "permission"
            elif has_question:
                kind = "question"
            else:
                kind = ""
            result[session.id] = {
                "needs_attention": bool(kind),
                "attention_kind": kind,
            }
        return result


def _descendant_ids(
    session_id: uuid.UUID,
    children_map: dict[uuid.UUID, list[uuid.UUID]],
) -> list[uuid.UUID]:
    """Return *session_id* followed by descendant ids from *children_map*."""
    ids: list[uuid.UUID] = [session_id]
    queue: list[uuid.UUID] = [session_id]
    seen: set[uuid.UUID] = {session_id}
    while queue:
        current = queue.pop(0)
        for child_id in children_map.get(current, []):
            if child_id in seen:
                continue
            seen.add(child_id)
            ids.append(child_id)
            queue.append(child_id)
    return ids
