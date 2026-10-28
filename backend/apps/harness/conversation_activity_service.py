"""Conversation activity shared with consumers of harness lifecycle events.

The harness owns root ancestry, turn outcomes and pending-gate interpretation.
Consumers receive immutable metadata with no models, prompts or tool output.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from .conversation_activity_repository import ConversationActivityRepository
from .models import HarnessMessage, HarnessSession


@dataclass(frozen=True)
class ConversationActivity:
    """Conversation metadata and the workspace ownership observed at read time."""

    id: uuid.UUID
    organization_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_owner_id: int | None
    workspace_name: str
    title: str
    status: str


@dataclass(frozen=True)
class AssistantActivity:
    """Turn identity and outcome; completed_at distinguishes unfinished turns."""

    id: uuid.UUID
    completed_at: datetime | None
    phase: str


class ConversationActivityService:
    """Interpret durable conversation state independently of live execution."""

    def __init__(
        self, *, repository: type[ConversationActivityRepository] | None = None
    ) -> None:
        """Use harness persistence without depending on activity consumers."""
        self.repo = repository or ConversationActivityRepository

    def source(
        self, session_id: uuid.UUID
    ) -> tuple[ConversationActivity, AssistantActivity | None] | None:
        """Return an in-scope root conversation and its latest assistant turn."""
        session = self.repo.root_session(session_id)
        if session is None:
            return None
        message = self.repo.latest_assistant(session.id)
        assistant = self._assistant(message) if message else None
        return self._conversation(session), assistant

    def message_source(
        self, message_id: uuid.UUID
    ) -> tuple[ConversationActivity, AssistantActivity] | None:
        """Resolve a root assistant turn without exposing child completions."""
        message = self.repo.root_message(message_id)
        if message is None:
            return None
        return self._conversation(message.session), self._assistant(message)

    def root_id(self, session_id: uuid.UUID) -> uuid.UUID | None:
        """Resolve a child to its root without a depth cap; reject broken cycles."""
        return self._root_ids({session_id})[session_id]

    def _root_ids(
        self, session_ids: set[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID | None]:
        """Read shared ancestors once per call, one query per tree level."""
        parents: dict[uuid.UUID, uuid.UUID | None] = {}
        seen: set[uuid.UUID] = set()
        pending = session_ids
        while pending:
            rows = self.repo.session_parents(pending)
            parents.update(rows)
            seen.update(pending)
            pending = {
                parent
                for parent in rows.values()
                if parent is not None and parent not in seen
            }

        resolved: dict[uuid.UUID, uuid.UUID | None] = {}
        for session_id in session_ids:
            current = session_id
            path: set[uuid.UUID] = set()
            while current not in resolved:
                if current in path or current not in parents:
                    root = None
                    break
                path.add(current)
                parent = parents[current]
                if parent is None:
                    root = current
                    break
                current = parent
            else:
                root = resolved[current]
            resolved.update((ancestor, root) for ancestor in path)
        return {session_id: resolved.get(session_id) for session_id in session_ids}

    def active_rows(
        self, org_id: uuid.UUID, user_id: int
    ) -> list[tuple[ConversationActivity, str]]:
        """Derive busy root activity, attributing child gates to their own root."""
        sessions = self.repo.busy_roots(org_id, user_id)
        if not sessions:
            return []
        workspace_ids = [session.workspace_id for session in sessions]
        permission_ids = set(
            self.repo.pending_session_ids(org_id, workspace_ids, permissions=True)
        )
        question_ids = set(
            self.repo.pending_session_ids(org_id, workspace_ids, permissions=False)
        )
        roots = self._root_ids(permission_ids | question_ids)
        permission_roots = {roots[session_id] for session_id in permission_ids}
        question_roots = {roots[session_id] for session_id in question_ids}
        result = []
        for session in sessions:
            if session.latest_assistant_completed_at is not None:
                continue
            phase = "running"
            if session.id in permission_roots:
                phase = "waiting_for_approval"
            elif session.id in question_roots:
                phase = "waiting_for_input"
            result.append((self._conversation(session), phase))
        return result

    def pending_gate(
        self, request_id: str, phase: str, org_id: uuid.UUID, workspace_id: str
    ) -> bool:
        """Revalidate an exact pending gate independently of activity list limits."""
        try:
            request_uuid = uuid.UUID(request_id)
            workspace_uuid = uuid.UUID(workspace_id)
            return self.repo.pending_gate(
                request_uuid,
                org_id,
                workspace_uuid,
                permissions=phase == "waiting_for_approval",
            )
        except (ValueError, TypeError):
            return False

    def recent_completions(self) -> list[uuid.UUID]:
        """Find root completions for repair after interrupted event delivery."""
        return self.repo.recent_completions(timezone.now() - timedelta(minutes=15))

    def recent_attention_events(self) -> list[tuple[str, dict[str, str]]]:
        """Reconstruct fresh unanswered gate events from durable harness state."""
        result = []
        for permissions, event in (
            (True, "harness.permission_required"),
            (False, "harness.question_required"),
        ):
            for request_id, session_id in self.repo.recent_pending_requests(
                timezone.now() - timedelta(minutes=15), permissions=permissions
            ):
                result.append(
                    (
                        event,
                        {"request_id": str(request_id), "session_id": str(session_id)},
                    )
                )
        return result

    @staticmethod
    def _conversation(session: HarnessSession) -> ConversationActivity:
        return ConversationActivity(
            id=session.id,
            organization_id=session.organization_id,
            workspace_id=session.workspace_id,
            workspace_owner_id=session.workspace.created_by_id,
            workspace_name=session.workspace.name,
            title=session.title,
            status=session.status,
        )

    @staticmethod
    def _assistant(message: HarnessMessage) -> AssistantActivity:
        if message.finish in {"abort", "aborted", "cancelled", "interrupted"}:
            phase = "interrupted"
        elif message.error or message.finish == "error":
            phase = "error"
        else:
            phase = "completed"
        return AssistantActivity(message.id, message.completed_at, phase)
