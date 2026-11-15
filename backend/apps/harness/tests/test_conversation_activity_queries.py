"""Activity reads stay bounded while preserving root, turn and gate semantics."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.harness.conversation_activity_service import ConversationActivityService
from apps.harness.models import HarnessMessage, HarnessSession, QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.runners.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("width", [1, 30])
@pytest.mark.parametrize("depth", [1, 3])
def test_activity_queries_grow_with_depth_not_gate_count(
    harness_workspace: Workspace, width: int, depth: int
) -> None:
    """Both gate types share ancestry reads; response text never crosses SQL."""
    org_id = harness_workspace.runner.organization_id
    roots = [
        HarnessSession(
            workspace=harness_workspace, organization_id=org_id, status="busy"
        )
        for _ in range(width)
    ]
    HarnessSession.objects.bulk_create(roots)
    HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(session=root, role="assistant", content="x" * 65536)
            for root in roots
        ]
    )
    children = roots
    for _ in range(depth):
        children = [
            HarnessSession(
                workspace=harness_workspace,
                organization_id=org_id,
                parent=parent,
                status="busy",
            )
            for parent in children
        ]
        HarnessSession.objects.bulk_create(children)
    QuestionRequest.objects.bulk_create(
        [
            QuestionRequest(
                organization_id=org_id,
                workspace_id=harness_workspace.id,
                session_id=child.id,
            )
            for child in children
        ]
    )
    PermissionRequest.objects.bulk_create(
        [
            PermissionRequest(
                organization_id=org_id,
                workspace_id=harness_workspace.id,
                session_id=child.id,
                tool="exec",
            )
            for child in children[::2]
        ]
    )
    with CaptureQueriesContext(connection) as queries:
        rows = ConversationActivityService().active_rows(
            org_id, harness_workspace.created_by_id
        )

    assert len(queries) == 3 + depth + 1
    assert {row.id: phase for row, phase in rows} == {
        root.id: "waiting_for_approval" if index % 2 == 0 else "waiting_for_input"
        for index, root in enumerate(roots)
    }
    assert all('"content"' not in query["sql"] for query in queries)


def test_activity_uses_latest_assistant_position_and_preserves_root_order(
    harness_workspace: Workspace,
    django_assert_num_queries: Callable[[int], AbstractContextManager],
) -> None:
    """Missing assistants stay active; newer users cannot hide completed turns."""
    org_id = harness_workspace.runner.organization_id
    roots = [
        HarnessSession(
            workspace=harness_workspace, organization_id=org_id, status="busy"
        )
        for _ in range(3)
    ]
    HarnessSession.objects.bulk_create(roots)
    completed = timezone.now()
    old = completed - timedelta(days=1)
    for index, root in enumerate(roots):
        HarnessSession.objects.filter(id=root.id).update(
            updated_at=old + timedelta(minutes=index)
        )
    first = HarnessMessage.objects.create(
        session=roots[0], role="assistant", position=0, completed_at=completed
    )
    latest = HarnessMessage.objects.create(
        session=roots[0], role="assistant", position=1
    )
    # Creation timestamps deliberately disagree with the durable turn order.
    HarnessMessage.objects.filter(id=latest.id).update(created_at=old)
    HarnessMessage.objects.filter(id=first.id).update(created_at=completed)
    HarnessMessage.objects.create(
        session=roots[1], role="assistant", position=0, completed_at=completed
    )
    HarnessMessage.objects.create(session=roots[1], role="user", position=1)
    activity = ConversationActivityService()
    with django_assert_num_queries(3):
        rows = activity.active_rows(org_id, harness_workspace.created_by_id)
    assert [(row.id, phase) for row, phase in rows] == [
        (roots[2].id, "running"),
        (roots[0].id, "running"),
    ]
    # No cross-call cache: completion and later admission are visible immediately.
    HarnessMessage.objects.filter(id=latest.id).update(completed_at=completed)
    assert [
        row.id
        for row, _ in activity.active_rows(org_id, harness_workspace.created_by_id)
    ] == [roots[2].id]
    HarnessMessage.objects.create(session=roots[0], role="assistant", position=2)
    assert len(activity.active_rows(org_id, harness_workspace.created_by_id)) == 2


def test_batch_ancestry_rejects_cycles_and_missing_links() -> None:
    """Converging paths are read once and invalid graphs never gain a root."""
    root, child, grandchild, cycle_a, cycle_b, orphan, missing = [
        uuid.uuid4() for _ in range(7)
    ]
    parents = {
        root: None,
        child: root,
        grandchild: child,
        cycle_a: cycle_b,
        cycle_b: cycle_a,
        orphan: missing,
    }

    class Repository:
        calls: list[set[uuid.UUID]] = []

        @classmethod
        def session_parents(
            cls, session_ids: set[uuid.UUID]
        ) -> dict[uuid.UUID, uuid.UUID | None]:
            cls.calls.append(session_ids)
            return {key: parents[key] for key in session_ids if key in parents}

    activity = ConversationActivityService(repository=Repository)
    assert activity._root_ids({child, grandchild, cycle_a, orphan}) == {
        child: root,
        grandchild: root,
        cycle_a: None,
        orphan: None,
    }
    assert len(Repository.calls) == 2
    assert sum(map(len, Repository.calls)) == len(set().union(*Repository.calls))
    assert activity.root_id(grandchild) == root
    assert activity.root_id(cycle_b) is None
    assert activity.root_id(missing) is None
    parents[child] = None
    assert activity.root_id(grandchild) == child


def test_activity_empty_or_foreign_scope_stops_after_root_read(
    harness_workspace: Workspace,
    django_assert_num_queries: Callable[[int], AbstractContextManager],
) -> None:
    """No gate or message reads occur without currently owned busy roots."""
    org_id = harness_workspace.runner.organization_id
    HarnessSession.objects.create(
        workspace=harness_workspace, organization_id=org_id, status="busy"
    )
    activity = ConversationActivityService()
    with django_assert_num_queries(1):
        assert activity.active_rows(uuid.uuid4(), harness_workspace.created_by_id) == []
    with django_assert_num_queries(1):
        assert activity.active_rows(org_id, harness_workspace.created_by_id + 1) == []
    HarnessSession.objects.filter(workspace=harness_workspace).update(status="idle")
    with django_assert_num_queries(1):
        assert activity.active_rows(org_id, harness_workspace.created_by_id) == []


@pytest.mark.parametrize(
    "path,query_count", [("source", 2), ("message", 1), ("active", 3)]
)
@pytest.mark.parametrize("body_size", [0, 512 * 1024])
def test_activity_reads_only_metadata_without_deferred_queries(
    harness_workspace: Workspace, path: str, query_count: int, body_size: int
) -> None:
    """Answer and runtime payload sizes must not affect metadata materialization."""
    org_id = harness_workspace.runner.organization_id
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=org_id,
        title="Review changes",
        status="busy",
        engine_state={"private": "x" * body_size},
    )
    message = HarnessMessage.objects.create(
        session=session,
        role="assistant",
        content="x" * body_size,
        engine_meta={"private": "x" * body_size},
    )
    activity = ConversationActivityService()
    with CaptureQueriesContext(connection) as queries:
        if path == "active":
            rows = activity.active_rows(org_id, harness_workspace.created_by_id)
            conversation, phase = rows[0]
            assert phase == "running"
        else:
            conversation, assistant = (
                activity.source(session.id)
                if path == "source"
                else activity.message_source(message.id)
            )
            assert asdict(assistant) == {
                "id": message.id,
                "completed_at": None,
                "phase": "completed",
            }
        assert asdict(conversation) == {
            "id": session.id,
            "organization_id": org_id,
            "workspace_id": harness_workspace.id,
            "workspace_owner_id": harness_workspace.created_by_id,
            "workspace_name": harness_workspace.name,
            "title": "Review changes",
            "status": "busy",
        }
    assert len(queries) == query_count
    for query in queries:
        for field in ("content", "engine_meta", "engine_state", "repos", "tokens"):
            assert f'"{field}"' not in query["sql"], query["sql"]


def test_activity_metadata_revalidates_ownership_and_outcome(
    harness_workspace: Workspace,
) -> None:
    """Narrow projections still observe completion and workspace reassignment."""
    from apps.organizations.models import Organization
    from apps.runners.models import Runner

    org_id = harness_workspace.runner.organization_id
    session = HarnessSession.objects.create(
        workspace=harness_workspace, organization_id=org_id, status="busy"
    )
    message = HarnessMessage.objects.create(session=session, role="assistant")
    activity = ConversationActivityService()
    assert activity.source(session.id)[1].completed_at is None
    completed = timezone.now()
    HarnessMessage.objects.filter(pk=message.id).update(
        completed_at=completed, finish="error", error="Provider failed"
    )
    for result in (activity.source(session.id), activity.message_source(message.id)):
        assert result[1].completed_at == completed
        assert result[1].phase == "error"
    assert activity.active_rows(org_id, harness_workspace.created_by_id) == []

    foreign_org = Organization.objects.create(name="Foreign", slug="foreign-activity")
    foreign_runner = Runner.objects.create(
        organization=foreign_org, api_token_hash=uuid.uuid4().hex
    )
    Workspace.objects.filter(pk=harness_workspace.id).update(runner=foreign_runner)
    assert activity.source(session.id) is None
    assert activity.message_source(message.id) is None
    assert activity.active_rows(org_id, harness_workspace.created_by_id) == []
