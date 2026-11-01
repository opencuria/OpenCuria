"""Scope, freshness and bounded query work for combined pending gate reads."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.harness.interaction_service import InteractionService
from apps.harness.models import HarnessSession
from apps.harness.permissions.repositories import PermissionRequestRepository
from apps.harness.repositories import (
    HarnessSessionRepository,
    QuestionRequestRepository,
)
from common.exceptions import NotFoundError

pytestmark = pytest.mark.django_db


@pytest.fixture
def interactions() -> InteractionService:
    """Read persisted gates without execution or transport dependencies."""

    async def emit(_event: str, _data: dict[str, Any], _workspace: str) -> None:
        pass

    return InteractionService(emit=emit, is_running=lambda _session: False)


def _gates(session: HarnessSession) -> tuple[Any, Any]:
    values = {
        "organization_id": session.organization_id,
        "workspace_id": session.workspace_id,
        "session_id": session.id,
        "call_id": "call-1",
    }
    return (
        PermissionRequestRepository.create(**values, tool="bash", pattern="pwd"),
        QuestionRequestRepository.create(**values, questions=[{"question": "Go?"}]),
    )


@pytest.mark.parametrize("depth", [0, 1, 3])
@pytest.mark.parametrize("width", [1, 6])
@pytest.mark.parametrize("has_gates", [False, True])
def test_pending_read_walks_tree_once_and_projects_only_gate_names(
    harness_session: HarnessSession,
    interactions: InteractionService,
    django_assert_num_queries: Any,
    depth: int,
    width: int,
    has_gates: bool,
) -> None:
    """Work scales with depth, not fan-out; empty trees need no name read."""
    level = [harness_session]
    for index in range(depth):
        level = HarnessSession.objects.bulk_create(
            [
                HarnessSession(
                    workspace_id=harness_session.workspace_id,
                    organization_id=harness_session.organization_id,
                    parent_id=parent.id,
                    agent_name=f"agent-{index}",
                    engine_state={"synthetic_state": "x" * 4096},
                )
                for parent in level
                for _ in range(width)
            ]
        )
    if has_gates:
        permission, question = _gates(level[-1])

    # Root validation + one query per level (including the empty leaf level)
    # + two gate reads + optional scalar attribution query.
    with django_assert_num_queries(depth + 4 + int(has_gates)) as queries:
        result = interactions.list_pending_interactions(
            harness_session.id, include_descendants=True
        )
    if has_gates:
        assert [row["request_id"] for row in result.permissions] == [str(permission.id)]
        assert [row["request_id"] for row in result.questions] == [str(question.id)]
        assert result.questions[0]["agent_name"] == level[-1].agent_name
        projection = queries.captured_queries[-1]["sql"].split(" FROM ", 1)[0]
        assert '"agent_name"' in projection
        assert '"engine_state"' not in projection
        assert '"tokens"' not in projection
    else:
        assert result.permissions == result.questions == []


@pytest.mark.parametrize("include_descendants", [False, True])
def test_pending_read_preserves_scope_order_and_payloads(
    harness_session: HarnessSession,
    interactions: InteractionService,
    include_descendants: bool,
) -> None:
    """Root/child gates retain attribution; resolved and sibling gates stay out."""
    root = harness_session
    child = HarnessSessionRepository.create(
        workspace_id=root.workspace_id,
        organization_id=root.organization_id,
        parent_id=root.id,
        agent_name="explore",
    )
    sibling = HarnessSessionRepository.create(
        workspace_id=root.workspace_id,
        organization_id=root.organization_id,
    )
    root_permission, root_question = _gates(root)
    child_permission, child_question = _gates(child)
    _gates(sibling)
    resolved_permission, resolved_question = _gates(child)
    PermissionRequestRepository.mark_resolved(
        resolved_permission, approved=True, remember="once"
    )
    QuestionRequestRepository.resolve(
        resolved_question, answers=["yes"], status="answered"
    )

    result = interactions.list_pending_interactions(
        root.id, include_descendants=include_descendants
    )
    expected_permissions = (
        [root_permission, child_permission]
        if include_descendants
        else [root_permission]
    )
    expected_questions = (
        [root_question, child_question] if include_descendants else [root_question]
    )
    assert [row["request_id"] for row in result.permissions] == [
        str(row.id) for row in expected_permissions
    ]
    assert [row["request_id"] for row in result.questions] == [
        str(row.id) for row in expected_questions
    ]
    assert result.permissions[0] == {
        "request_id": str(root_permission.id),
        "session_id": str(root.id),
        "workspace_id": str(root.workspace_id),
        "tool": "bash",
        "pattern": "pwd",
        "title": "",
        "call_id": "call-1",
        "status": "pending",
        "agent_name": root.agent_name,
    }
    assert result.questions[0] == {
        "request_id": str(root_question.id),
        "session_id": str(root.id),
        "workspace_id": str(root.workspace_id),
        "questions": [{"question": "Go?"}],
        "call_id": "call-1",
        "status": "pending",
        "agent_name": root.agent_name,
    }
    if include_descendants:
        assert result.questions[-1]["agent_name"] == "explore"

    # No shared cache: gate resolutions, newly spawned children and renamed
    # agents must be reflected on the very next request.
    QuestionRequestRepository.resolve(root_question, answers=["yes"], status="answered")
    HarnessSession.objects.filter(pk=root.id).update(agent_name="renamed")
    new_child = HarnessSessionRepository.create(
        workspace_id=root.workspace_id,
        organization_id=root.organization_id,
        parent_id=child.id,
        agent_name="review",
    )
    _, new_question = _gates(new_child)
    refreshed = interactions.list_pending_interactions(
        root.id, include_descendants=include_descendants
    )
    assert refreshed.permissions[0]["agent_name"] == "renamed"
    assert all(
        row["request_id"] != str(root_question.id) for row in refreshed.questions
    )
    assert (
        any(row["request_id"] == str(new_question.id) for row in refreshed.questions)
        == include_descendants
    )


def test_missing_session_stops_before_reading_gates(
    interactions: InteractionService,
    django_assert_num_queries: Any,
) -> None:
    """Unknown scope preserves NotFoundError and never reads unrelated gates."""
    with django_assert_num_queries(1), pytest.raises(NotFoundError):
        interactions.list_pending_interactions(uuid.uuid4(), include_descendants=True)
