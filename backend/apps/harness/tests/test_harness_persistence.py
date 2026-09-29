"""Tests for M6 harness persistence: models and repositories."""

from __future__ import annotations

import pytest

from apps.harness.models import (
    HarnessMessage,
    HarnessPart,
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

    reasoning = HarnessPartRepository.create(
        message_id=assistant.id, type="reasoning", state="running"
    )
    HarnessPartRepository.append_output(reasoning, "old line\n")
    HarnessPartRepository.append_output(reasoning, "last line")
    reasoning.refresh_from_db()
    assert reasoning.display["summary"] == "last line"
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
    assert {p.id for p in HarnessPartRepository.list_for_session(session.id)} == {
        part.id,
        reasoning.id,
    }
    assert user_message.id is not None


@pytest.mark.django_db
def test_timeline_migration_backfills_and_scrubs_patch_metadata(
    harness_workspace,
) -> None:
    """The additive data migration is bounded/idempotent and removes file copies."""
    import importlib
    from types import SimpleNamespace

    from django.apps import apps as django_apps
    from django.db import connection

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    message = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    legacy_patch = HarnessPart.objects.create(
        message=message,
        type="patch",
        position=0,
        title="Patch file",
        output="--- a/f\n+++ b/f\n-old\n+new",
        meta={"path": "f", "old_content": "old", "new_content": "new"},
    )
    oversized_patch_output = "x" * 600_000
    oversized_patch = HarnessPart.objects.create(
        message=message,
        type="patch",
        position=1,
        title="Oversized patch",
        output=oversized_patch_output,
        meta={"path": "huge.py"},
    )
    migration = importlib.import_module(
        "apps.harness.migrations.0026_backfill_harnesspart_display"
    )
    schema_editor = SimpleNamespace(connection=connection)
    migration.backfill_part_display(django_apps, schema_editor)
    migration.backfill_part_display(django_apps, schema_editor)
    legacy_patch.refresh_from_db()
    assert "old_content" not in legacy_patch.meta
    assert "new_content" not in legacy_patch.meta
    assert legacy_patch.display["path"] == "f"
    assert legacy_patch.display["additions"] == 1
    assert legacy_patch.display["deletions"] == 1
    assert legacy_patch.display["preview"] == [
        {"type": "del", "oldNo": 1, "newNo": None, "content": "old"},
        {"type": "add", "oldNo": None, "newNo": 1, "content": "new"},
    ]
    oversized_patch.refresh_from_db()
    assert oversized_patch.output == oversized_patch_output
    assert oversized_patch.display["preview"] == []
    assert oversized_patch.display["additions"] == 0
    assert migration.Migration.atomic is False
    assert "apps.harness.timeline" not in open(migration.__file__).read()


@pytest.mark.django_db
def test_reasoning_stream_append_does_not_read_the_growing_output(harness_workspace):
    """Reasoning projection refresh reads display only, never full output."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    message = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    part = HarnessPartRepository.create(
        message_id=message.id, type="reasoning", state="running"
    )
    HarnessPart.objects.filter(id=part.id).update(output="x" * 100_000)
    with CaptureQueriesContext(connection) as captured:
        HarnessPartRepository.append_output_by_id(part.id, "\nlatest")
    selects = [
        query["sql"]
        for query in captured
        if query["sql"].lstrip().upper().startswith("SELECT")
    ]
    assert len(selects) == 1
    assert "output" not in selects[0].lower()
    part.refresh_from_db()
    assert part.output.endswith("\nlatest")
    assert part.display["summary"] == "latest"


@pytest.mark.django_db
def test_timeline_query_defers_heavy_part_fields(
    harness_workspace, django_assert_num_queries
) -> None:
    """Timeline list loads display only; detail fields remain deferred and grouped."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    message = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    part = HarnessPartRepository.create(
        message_id=message.id,
        type="tool",
        title="Read file",
        input={"tool": "read", "arguments": "x" * 10000},
        output="y" * 10000,
        meta={"huge": "z" * 10000},
    )
    with django_assert_num_queries(1):
        rows = HarnessPartRepository.list_timeline_for_session(session.id)
    row = next(item for item in rows if item.id == part.id)
    assert row.timeline_output == ""
    assert row.display["tool"] == "read"
    assert {"input", "output", "meta"} <= row.get_deferred_fields()

    text = HarnessPartRepository.create(
        message_id=message.id, type="text", output="body from text part"
    )
    with django_assert_num_queries(1):
        text_row = next(
            item
            for item in HarnessPartRepository.list_timeline_for_session(session.id)
            if item.id == text.id
        )
    assert text_row.timeline_output == "body from text part"
    assert text_row.output == "body from text part"


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
