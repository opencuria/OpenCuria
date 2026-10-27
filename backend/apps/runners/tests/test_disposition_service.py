"""Characterize authorization, fresh evidence and atomic disposition outcomes."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from apps.organizations.models import Membership
from apps.runners.disposition_repository import DispositionRepository
from apps.runners.models import LifecycleCommand, Runner, Workspace
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services.disposition import DispositionService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

pytestmark = pytest.mark.django_db


def _operation(
    runner: Runner, workspace: Workspace, **payload: Any
) -> LifecycleCommand:
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
        operation_payload=payload,
    )
    return LifecycleCommand.objects.select_related(
        "task__runner", "task__workspace"
    ).get(task_id=task.id)


def _evidence(row: LifecycleCommand, *, known: bool = True) -> dict[str, Any]:
    return {
        "status": "terminal",
        "instance_id": "exclusive-current-process",
        "execution_finished": True,
        "outcome_known": known,
        "quiescent": True,
        "event": "workspace:error",
        "identity": OperationRepository.envelope(row, {}),
    }


def test_listing_scopes_and_sanitizes_operations(
    runner: Runner, workspace: Workspace, user: Any, django_user_model: Any
) -> None:
    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    other = django_user_model.objects.create_user(email="disposition-list@example.com")
    row = _operation(runner, workspace, repos=["private-repo"], token="private-token")
    workspace.created_by = other
    workspace.save(update_fields=["created_by"])
    service = DispositionService()
    assert service.list_operations(user, runner.organization_id) == []
    Membership.objects.filter(user=user).update(role="admin")
    assert service.list_operations(user, runner.organization_id) == [
        {
            "operation_id": str(row.task_id),
            "task_type": "stop_workspace",
            "status": row.task.status,
            "phase": row.phase,
            "runner_id": str(runner.id),
            "target": row.target,
            "attempt": row.attempt,
            "diagnostic": "",
            "intervention_required": False,
        }
    ]
    Membership.objects.filter(user=user).delete()
    with pytest.raises(NotFoundError):
        service.list_operations(user, runner.organization_id)


@pytest.mark.parametrize("admin", [False, True])
def test_unknown_outcome_action_requires_admin(
    runner: Runner, workspace: Workspace, user: Any, admin: bool
) -> None:
    Membership.objects.create(
        user=user, organization=runner.organization, role="admin" if admin else "member"
    )
    row = _operation(runner, workspace)
    assert DispositionService.permitted_actions(
        DispositionRepository.get(runner.organization_id, row.task_id),
        _evidence(row, known=False),
        user,
        runner.organization_id,
    ) == (["acknowledge_interrupted"] if admin else [])


@pytest.mark.parametrize(
    "change", ["retry_limit", "runner_sid", "workspace_fence", "resolved"]
)
def test_disposition_rechecks_locked_state(
    runner: Runner, workspace: Workspace, change: str
) -> None:
    row = _operation(runner, workspace)
    evidence = _evidence(row)
    state = DispositionRepository.get(runner.organization_id, row.task_id)
    if change == "retry_limit":
        LifecycleCommand.objects.filter(pk=row.pk).update(payload={"_retry_count": 2})
    elif change == "runner_sid":
        type(runner).objects.filter(pk=runner.pk).update(sid="replacement-session")
    elif change == "workspace_fence":
        workspace.current_task = None
        workspace.save(update_fields=["current_task"])
        _operation(runner, workspace)
    else:
        LifecycleCommand.objects.filter(pk=row.pk).update(phase="disposed")
    with pytest.raises(ConflictError):
        DispositionService._dispose(state, evidence, "retry")
    row.task.refresh_from_db()
    assert row.task.status != "failed"


def test_retry_allocation_failure_rolls_back_disposition(
    runner: Runner, workspace: Workspace
) -> None:
    row = _operation(runner, workspace)
    prior_status = row.task.status
    prior_phase = row.phase
    with patch.object(TaskRepository, "create", side_effect=RuntimeError("allocation")):
        with pytest.raises(RuntimeError, match="allocation"):
            DispositionService._dispose(
                DispositionRepository.get(runner.organization_id, row.task_id),
                _evidence(row),
                "retry",
            )
    row.refresh_from_db()
    row.task.refresh_from_db()
    workspace.refresh_from_db()
    assert row.phase == prior_phase and row.task.status == prior_status
    assert workspace.current_task_id == row.task_id


@pytest.mark.django_db(transaction=True)
async def test_retry_workflow_keeps_rpc_and_response_contract(
    runner: Runner, workspace: Workspace, user: Any
) -> None:
    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    row = _operation(runner, workspace)
    transport = AsyncMock()
    transport._call_runner.return_value = _evidence(row)
    result = await DispositionService().act(
        transport, user, runner.organization_id, row.task_id, "retry"
    )
    assert result["previous_operation_id"] == str(row.task_id)
    assert result["operation_id"] != str(row.task_id)
    transport._call_runner.assert_awaited_once_with(
        runner, "operation:inspect", OperationRepository.envelope(row, {}), timeout=60
    )
    row.refresh_from_db()
    assert row.phase == "disposed"


@pytest.mark.django_db(transaction=True)
async def test_member_cannot_acknowledge_before_contacting_runner(
    runner: Runner, workspace: Workspace, user: Any
) -> None:
    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    row = _operation(runner, workspace)
    transport = AsyncMock()
    with pytest.raises(AuthenticationError):
        await DispositionService().act(
            transport,
            user,
            runner.organization_id,
            row.task_id,
            "acknowledge_interrupted",
        )
    transport._call_runner.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_reconcile_passes_the_exact_runner_outcome(
    runner: Runner, workspace: Workspace, user: Any
) -> None:
    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    row = _operation(runner, workspace)
    outcome = {**OperationRepository.envelope(row, {}), "error": "runner failure"}
    transport = AsyncMock()
    transport._call_runner.return_value = {**_evidence(row), "result": outcome}
    with patch(
        "apps.runners.services.disposition.apply_result", return_value=True
    ) as apply:
        assert await DispositionService().act(
            transport, user, runner.organization_id, row.task_id, "reconcile"
        ) == {"operation_id": str(row.task_id), "reconciled": True}
    apply.assert_called_once_with(transport, str(runner.id), "workspace:error", outcome)


@pytest.mark.django_db(transaction=True)
async def test_acknowledge_seals_unrecorded_identity_before_release(
    runner: Runner, workspace: Workspace, user: Any
) -> None:
    Membership.objects.create(user=user, organization=runner.organization, role="admin")
    row = _operation(runner, workspace)
    evidence = _evidence(row, known=False)
    transport = AsyncMock()
    transport._call_runner.side_effect = [
        {**evidence, "status": "unknown", "diagnostic_code": "identity_not_recorded"},
        evidence,
    ]
    assert await DispositionService().act(
        transport, user, runner.organization_id, row.task_id, "acknowledge_interrupted"
    ) == {
        "operation_id": str(row.task_id),
        "released": True,
        "resources_preserved": True,
    }
    assert [call.args[1] for call in transport._call_runner.await_args_list] == [
        "operation:inspect",
        "operation:seal",
    ]
    workspace.refresh_from_db()
    assert workspace.current_task_id is None


@pytest.mark.parametrize("field", ["operation_id", "runner_id", "attempt", "target"])
def test_mismatched_evidence_never_mutates_operation(
    runner: Runner, workspace: Workspace, field: str
) -> None:
    row = _operation(runner, workspace)
    evidence = _evidence(row)
    evidence["identity"][field] = "wrong-identity"
    with pytest.raises(ConflictError, match="Runner evidence identity mismatch"):
        DispositionService._dispose(
            DispositionRepository.get(runner.organization_id, row.task_id),
            evidence,
            "retry",
        )
    workspace.refresh_from_db()
    assert workspace.current_task_id == row.task_id


def test_loaded_state_never_queries_orm_during_policy_or_formatting(
    runner: Runner, workspace: Workspace, user: Any, django_assert_num_queries: Any
) -> None:
    row = _operation(runner, workspace)
    state = DispositionRepository.get(runner.organization_id, row.task_id)
    evidence = _evidence(row)
    with django_assert_num_queries(0):
        assert DispositionService._summary(state)["operation_id"] == str(row.task_id)
        assert DispositionService.permitted_actions(
            state, evidence, user, runner.organization_id
        ) == ["reconcile", "retry"]


@pytest.mark.django_db(transaction=True)
def test_postgres_duplicate_retries_allocate_only_one_successor(
    runner: Runner, workspace: Workspace
) -> None:
    """Two dispositions must serialize before releasing the same reservation."""
    from django.db import connection

    from apps.runners.tests.test_postgres_lifecycle_concurrency import race

    if connection.vendor != "postgresql":
        pytest.skip("requires real PostgreSQL row locks")
    row = _operation(runner, workspace)
    state = DispositionRepository.get(runner.organization_id, row.task_id)
    evidence = _evidence(row)

    def attempt() -> dict | None:
        try:
            return DispositionService._dispose(state, evidence, "retry")
        except ConflictError:
            return None

    results = race(runner, attempt, attempt)
    assert sum(result is not None for result in results) == 1
    assert LifecycleCommand.objects.count() == 2
    workspace.refresh_from_db()
    assert workspace.current_task_id != row.task_id
    assert workspace.current_task_id is not None
