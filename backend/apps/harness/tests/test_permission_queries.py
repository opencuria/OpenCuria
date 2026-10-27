"""Keep pending permission reads scoped, ordered and independent of old history."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.harness.permissions.models import PermissionRequest
from apps.harness.permissions.repositories import PermissionRequestRepository

pytestmark = pytest.mark.django_db


@pytest.fixture
def pending_requests() -> list[PermissionRequest]:
    """Interleave pending/resolved rows, tools and session scopes in time."""
    session_id, other_id = uuid.uuid4(), uuid.uuid4()
    rows = []
    for index, (scope, status, tool) in enumerate(
        [
            (session_id, "pending", "bash"),
            (session_id, "approved", "bash"),
            (other_id, "pending", "bash"),
            (session_id, "pending", "read"),
            (session_id, "rejected", "read"),
            (session_id, "pending", "bash"),
        ]
    ):
        row = PermissionRequest.objects.create(
            organization_id=uuid.uuid4(),
            session_id=scope,
            tool=tool,
            pattern=f"action-{index}",
            status=status,
        )
        # Reverse insertion order to exercise the repository's chronological sort.
        row.created_at = timezone.now() - timedelta(days=index)
        PermissionRequest.objects.filter(pk=row.pk).update(created_at=row.created_at)
        rows.append(row)
    return rows


@pytest.mark.parametrize("multiple", [False, True])
@pytest.mark.parametrize("tool", [None, "bash", "missing"])
def test_pending_scope_status_tool_and_order(
    pending_requests: list[PermissionRequest], multiple: bool, tool: str | None
) -> None:
    """Both query shapes retain scope, status, optional tool and oldest-first order."""
    first, _, other, read, _, last = pending_requests
    if multiple:
        actual = PermissionRequestRepository.list_pending_for_sessions(
            [first.session_id, other.session_id], tool=tool
        )
        expected = [last, read, other, first]
    else:
        actual = PermissionRequestRepository.list_pending_for_session(
            first.session_id, tool=tool
        )
        expected = [last, read, first]
    if tool:
        expected = [row for row in expected if row.tool == tool]
    assert [row.id for row in actual] == [row.id for row in expected]


def test_empty_scope_does_not_query(django_assert_num_queries: Any) -> None:
    """An empty authorized session scope cannot load another session's gates."""
    with django_assert_num_queries(0):
        assert PermissionRequestRepository.list_pending_for_sessions([]) == []


@pytest.mark.parametrize("approved", [False, True])
def test_resolved_request_leaves_pending_reads(
    pending_requests: list[PermissionRequest], approved: bool
) -> None:
    """Status changes become visible immediately without stale pending entries."""
    first = pending_requests[0]
    PermissionRequestRepository.mark_resolved(first, approved=approved, remember="once")
    assert first.id not in {
        row.id
        for row in PermissionRequestRepository.list_pending_for_session(
            first.session_id
        )
    }
    assert PermissionRequestRepository.get_by_id(first.id).status == (
        "approved" if approved else "rejected"
    )


@pytest.mark.parametrize("shape", ["single", "multiple", "tool"])
def test_pending_reads_use_index_with_retained_history(shape: str) -> None:
    """The actual repository SQL must avoid scanning retained permission history."""
    if connection.vendor not in {"sqlite", "postgresql"}:
        pytest.skip("query plan assertions cover the supported database engines")
    session_id, other_id = uuid.uuid4(), uuid.uuid4()
    PermissionRequest.objects.bulk_create(
        [
            PermissionRequest(
                organization_id=uuid.uuid4(),
                session_id=session_id if index % 2 else uuid.uuid4(),
                status="approved" if index % 3 else "rejected",
                tool="bash",
                pattern="resolved",
            )
            for index in range(2048)
        ]
    )
    pending = PermissionRequestRepository.create(
        organization_id=uuid.uuid4(), session_id=session_id, tool="bash", pattern="new"
    )
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE harness_permission_request")
    with CaptureQueriesContext(connection) as queries:
        if shape == "single":
            rows = PermissionRequestRepository.list_pending_for_session(session_id)
        else:
            rows = PermissionRequestRepository.list_pending_for_sessions(
                [session_id, other_id], tool="bash" if shape == "tool" else None
            )
    assert [row.id for row in rows] == [pending.id]
    assert len(queries) == 1
    prefix = "EXPLAIN QUERY PLAN " if connection.vendor == "sqlite" else "EXPLAIN "
    with connection.cursor() as cursor:
        cursor.execute(prefix + queries[0]["sql"])
        plan = str(cursor.fetchall())
    assert "harness_perm_session_state_idx" in plan, plan
    if shape == "single" and connection.vendor == "sqlite":
        assert "TEMP B-TREE" not in plan, plan
