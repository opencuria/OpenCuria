"""Tests for M6 harness persistence: models and repositories."""

from __future__ import annotations

import pytest

from apps.harness.models import (
    HarnessMessage,
    HarnessSession,
    Todo,
)
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
    TodoRepository,
    TodoRepositoryDjango,
)
from apps.harness.tools.todos import TodoItem


@pytest.mark.django_db
def test_session_message_part_crud(harness_workspace) -> None:
    """Sessions, messages and parts round-trip through repositories."""
    workspace = harness_workspace
    org_id = workspace.runner.organization_id
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=org_id,
        title="do it",
        agent_name="build",
        mode="build",
        model="m",
    )
    assert session.status == "idle"
    assert HarnessSession.objects.count() == 1

    listed = HarnessSessionRepository.list_for_workspace(workspace.id)
    assert [s.id for s in listed] == [session.id]
    scoped = HarnessSessionRepository.get_for_workspace(session.id, workspace.id)
    assert scoped is not None

    user_message = HarnessMessageRepository.create(
        session_id=session.id, role="user", content="do it"
    )
    assistant = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", model="m"
    )
    HarnessMessageRepository.append_content(assistant, "hel")
    HarnessMessageRepository.append_content(assistant, "lo")
    assistant.refresh_from_db()
    assert assistant.content == "hello"
    assert HarnessMessage.objects.count() == 2

    part = HarnessPartRepository.create(
        message_id=assistant.id, type="text", state="running"
    )
    HarnessPartRepository.append_output(part, "hel")
    HarnessPartRepository.append_output(part, "lo")
    part.refresh_from_db()
    assert part.output == "hello"
    HarnessPartRepository.mark_state(part, "completed")
    part.refresh_from_db()
    assert part.state == "completed"

    HarnessMessageRepository.add_usage(
        assistant, prompt_tokens=3, completion_tokens=4, total_tokens=7
    )
    HarnessSessionRepository.add_usage(
        session, prompt_tokens=3, completion_tokens=4, total_tokens=7, cost=0.5
    )
    session.refresh_from_db()
    assert session.tokens == {"prompt": 3, "completion": 4, "total": 7}
    assert session.cost == 0.5

    HarnessMessageRepository.complete(assistant, finish="stop")
    assistant.refresh_from_db()
    assert assistant.finish == "stop"
    assert assistant.completed_at is not None

    assert len(HarnessMessageRepository.list_for_session(session.id)) == 2
    assert [p.id for p in HarnessPartRepository.list_for_session(session.id)] == [
        part.id
    ]
    assert user_message.id is not None


@pytest.mark.django_db
def test_django_todo_repository_replaces_list(harness_workspace) -> None:
    """TodoRepositoryDjango persists via the Todo model (M3 seam)."""
    workspace = harness_workspace
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        title="todos",
    )
    repo = TodoRepositoryDjango()
    stored = repo.save(
        str(session.id),
        [
            TodoItem(content="a", status="pending", priority="high", order=0),
            TodoItem(content="b", status="completed", priority="low", order=1),
        ],
    )
    assert [item.content for item in stored.items] == ["a", "b"]
    assert Todo.objects.filter(session_id=session.id).count() == 2

    again = repo.list(str(session.id))
    assert [item.status for item in again.items] == ["pending", "completed"]

    replaced = repo.save(
        str(session.id),
        [TodoItem(content="only", status="in_progress", order=0)],
    )
    assert [item.content for item in replaced.items] == ["only"]
    assert Todo.objects.filter(session_id=session.id).count() == 1

    rows = TodoRepository.list_for_session(session.id)
    assert [(r.content, r.order) for r in rows] == [("only", 0)]

    empty = repo.list("not-a-uuid")
    assert empty.items == []


@pytest.mark.django_db
def test_todo_save_rejects_non_uuid_session() -> None:
    """Saving todos without a real session UUID raises ValueError."""
    repo = TodoRepositoryDjango()
    with pytest.raises(ValueError, match="real session UUID"):
        repo.save("plain-session", [TodoItem(content="x")])
