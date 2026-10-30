"""Characterize saved history reads independently of live runs."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.conversation_service import ConversationService
from apps.harness.models import Todo
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from common.exceptions import NotFoundError

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "action",
    ["get_session", "list_timeline_messages", "list_timeline_parts", "list_todos"],
)
def test_saved_read_requires_an_existing_session(action: str) -> None:
    """Missing histories raise the session error instead of returning empties."""
    session_id = uuid.uuid4()
    with pytest.raises(NotFoundError) as error:
        getattr(ConversationService(), action)(session_id)
    assert error.value.message == f"HarnessSession '{session_id}' not found"


def test_saved_reads_preserve_empty_sessions_and_workspace_scope(
    harness_workspace,
) -> None:
    """An empty saved session is navigable without admitting a live run."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    service = ConversationService()
    assert service.get_session(session.id).id == session.id
    assert [row.id for row in service.list_sessions(harness_workspace.id)] == [
        session.id
    ]
    assert service.list_sessions(uuid.uuid4()) == []
    assert service.list_timeline_messages(session.id) == []
    assert service.list_timeline_parts(session.id) == []
    assert service.list_todos(session.id) == []


def test_part_detail_preserves_session_scope_and_not_found_precedence(
    harness_workspace,
) -> None:
    """A part is readable only in its saved session; missing sessions win."""
    session, other = [
        HarnessSessionRepository.create(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
        )
        for _ in range(2)
    ]
    message = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="answer"
    )
    part = HarnessPartRepository.create(
        message_id=message.id,
        type="tool",
        state="completed",
        output="full tool output",
    )
    service = ConversationService()
    assert service.get_part(session.id, part.id).output == "full tool output"
    missing_session_id = uuid.uuid4()
    missing_part_id = uuid.uuid4()
    for session_id, part_id, resource, expected_id in (
        (other.id, part.id, "HarnessPart", part.id),
        (session.id, missing_part_id, "HarnessPart", missing_part_id),
        (missing_session_id, part.id, "HarnessSession", missing_session_id),
    ):
        with pytest.raises(NotFoundError) as error:
            service.get_part(session_id, part_id)
        assert error.value.message == f"{resource} '{expected_id}' not found"


def test_todos_preserve_ids_order_and_display_fields(harness_workspace) -> None:
    """The history view keeps the persisted todo shape and display order."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    later = Todo.objects.create(
        session=session, content="Later", status="pending", priority="low", order=1
    )
    first = Todo.objects.create(
        session=session, content="First", status="completed", priority="high", order=0
    )
    assert ConversationService().list_todos(session.id) == [
        {
            "id": str(row.id),
            "content": row.content,
            "status": row.status,
            "priority": row.priority,
            "order": row.order,
        }
        for row in (first, later)
    ]
