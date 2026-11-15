"""Attention flags read session IDs without materializing gate payloads."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.harness.conversation_service import ConversationService
from apps.harness.models import QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.harness.permissions.repositories import PermissionRequestRepository
from apps.harness.repositories import (
    HarnessSessionRepository,
    QuestionRequestRepository,
)
from apps.runners.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture(params=["permission", "question"])
def gates(request: pytest.FixtureRequest) -> tuple[Any, Any, dict[str, Any]]:
    """Exercise the same projection contract for both kinds of pending gates."""
    if request.param == "permission":
        return (
            PermissionRequest,
            PermissionRequestRepository,
            {
                "tool": "exec",
                "pattern": "npm test",
            },
        )
    return (
        QuestionRequest,
        QuestionRequestRepository,
        {
            "questions": [{"question": "context " * 500}],
        },
    )


def test_pending_session_ids_are_scoped_distinct_and_payload_free(
    gates: tuple[Any, Any, dict[str, Any]],
) -> None:
    """Duplicates collapse; resolved and unrequested sessions cannot flag a chat."""
    model, repository, fields = gates
    session_id, other_id, resolved_id = (uuid.uuid4() for _ in range(3))
    for scope, status in [
        (session_id, "pending"),
        (session_id, "pending"),
        (other_id, "pending"),
        (resolved_id, "rejected"),
    ]:
        model.objects.create(
            organization_id=uuid.uuid4(), session_id=scope, status=status, **fields
        )

    with CaptureQueriesContext(connection) as queries:
        ids = repository.pending_session_ids([session_id, resolved_id, uuid.uuid4()])

    assert ids == {session_id}
    assert len(queries) == 1
    select = queries[0]["sql"].split(" FROM ")[0]
    assert select.removesuffix(' AS "session_id"') == (
        f'SELECT DISTINCT "{model._meta.db_table}"."session_id"'
    )
    assert "ORDER BY" not in queries[0]["sql"]

    # Re-read after resolution: no retained state or cache may keep the flag.
    model.objects.filter(session_id=session_id).update(status="rejected")
    assert repository.pending_session_ids([session_id]) == set()


def test_empty_pending_session_scope_does_not_query(
    gates: tuple[Any, Any, dict[str, Any]], django_assert_num_queries: Any
) -> None:
    """An empty scope does not accidentally read every organization's gates."""
    _, repository, _ = gates
    with django_assert_num_queries(0):
        assert repository.pending_session_ids([]) == set()


def test_attention_reads_only_gate_ids_and_keeps_descendant_flags(
    harness_workspace: Workspace,
) -> None:
    """Root/child lists use the projection and observe answers on the next read."""
    root = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    child = HarnessSessionRepository.create(
        workspace_id=root.workspace_id,
        organization_id=root.organization_id,
        parent_id=root.id,
    )
    question = QuestionRequest.objects.create(
        organization_id=root.organization_id,
        session_id=child.id,
        questions=[{"question": "large " * 1000}],
    )
    permission = PermissionRequest.objects.create(
        organization_id=root.organization_id,
        session_id=child.id,
        tool="exec",
        pattern="npm test",
    )
    service = ConversationService()
    with CaptureQueriesContext(connection) as queries:
        flags = service.attention_for_sessions([root, child])
    assert flags == {
        session.id: {"needs_attention": True, "attention_kind": "both"}
        for session in [root, child]
    }
    assert len(queries) == 3  # One ancestry read and two ID projections.
    for query in queries.captured_queries[1:]:
        assert query["sql"].startswith("SELECT DISTINCT ")
        assert "questions" not in query["sql"]
        assert "pattern" not in query["sql"]

    QuestionRequest.objects.filter(pk=question.pk).update(status="answered")
    assert service.attention_for_sessions([root])[root.id] == {
        "needs_attention": True,
        "attention_kind": "permission",
    }
    PermissionRequestRepository.mark_resolved(
        permission, approved=False, remember="once"
    )
    assert service.attention_for_sessions([root])[root.id] == {
        "needs_attention": False,
        "attention_kind": "",
    }
