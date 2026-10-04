"""Operator recovery must require exact finished execution and fresh resource proof."""

import uuid
from unittest.mock import AsyncMock

import pytest
from common.exceptions import ConflictError
from apps.runners.disposition_repository import DispositionRepository
from apps.runners.operations import OperationRepository
from apps.runners.repositories import TaskRepository
from apps.runners.services.disposition import DispositionService

pytestmark = pytest.mark.django_db


def test_acknowledge_preserves_failed_create_disk_and_releases_fence(runner, workspace):
    prior_pin = workspace.base_image_instance_id
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="create_workspace",
        operation_payload={"workspace_id": str(workspace.id), "repos": ["repo"]},
    )
    OperationRepository.intervene(task, "Unknown; manual intervention required")
    row = task.lifecyclecommand
    evidence = {
        "status": "terminal",
        "instance_id": "exclusive-current-process",
        "execution_finished": True,
        "outcome_known": False,
        "quiescent": True,
        "identity": OperationRepository.envelope(row, {}),
        "inventory": {"complete": True},
    }
    for missing in ["execution_finished", "quiescent", "instance_id"]:
        with pytest.raises(ConflictError):
            DispositionRepository.dispose(
                row, {**evidence, missing: False}, "acknowledge_interrupted"
            )
    result = DispositionRepository.dispose(row, evidence, "acknowledge_interrupted")
    workspace.refresh_from_db()
    assert result["resources_preserved"]
    assert workspace.current_task_id is None
    assert workspace.status == "failed"
    assert workspace.base_image_instance_id == prior_pin


def test_retry_new_identity_bounded_and_no_create_retry(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    row = task.lifecyclecommand
    evidence = {
        "status": "terminal",
        "instance_id": "process",
        "execution_finished": True,
        "outcome_known": True,
        "quiescent": True,
        "event": "workspace:error",
        "identity": OperationRepository.envelope(row, {}),
    }
    result = DispositionRepository.dispose(row, evidence, "retry")
    assert result["operation_id"] != str(task.id)
    next_task = TaskRepository.get_by_id(result["operation_id"])
    assert next_task.lifecyclecommand.payload["task_id"] == str(next_task.id)
    assert next_task.lifecyclecommand.payload["_retry_count"] == 1


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_offline_inspection_never_dispatches(
    runner, workspace, user, organization
):
    from asgiref.sync import sync_to_async

    def setup():
        from apps.organizations.models import Membership

        Membership.objects.get_or_create(
            user=workspace.created_by,
            organization=runner.organization,
            defaults={"role": "member"},
        )
        runner.status = "offline"
        runner.save()
        return TaskRepository.create(
            task_id=uuid.uuid4(),
            runner=runner,
            workspace=workspace,
            task_type="stop_workspace",
        )

    task = await sync_to_async(setup)()
    transport = AsyncMock()
    result = await DispositionService().inspect(
        transport, workspace.created_by, runner.organization_id, task.id
    )
    assert result["evidence"]["status"] == "unknown"
    transport._call_runner.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_rest_mcp_owner_admin_cross_org_and_scopes(runner, workspace, user):
    from apps.accounts.models import APIKeyPermission
    from apps.organizations.models import Membership
    from apps.runners.tests.test_workspace_access_controls import _make_client
    from apps.mcp_app.server import _call_inspect_lifecycle_operation, _TOOL_PERMISSIONS
    from types import SimpleNamespace

    Membership.objects.create(
        user=user, organization=runner.organization, role="member"
    )
    runner.status = "offline"
    runner.save()
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    client = _make_client(
        user=user, org=runner.organization, permissions=[APIKeyPermission.RUNNERS_READ]
    )
    response = client.get(f"/api/v1/runners/operations/{task.id}/")
    assert response.status_code == 200
    assert response.json()["evidence"]["status"] == "unknown"
    assert "payload" not in response.json()
    assert (
        client.post(f"/api/v1/runners/operations/{task.id}/retry/").status_code == 403
    )
    assert client.get("/api/v1/runners/operations/").status_code == 200
    assert (
        _TOOL_PERMISSIONS["inspect_lifecycle_operation"]
        == APIKeyPermission.RUNNERS_READ
    )
    result = _call_inspect_lifecycle_operation(
        SimpleNamespace(user=user),
        runner.organization_id,
        {"operation_id": str(task.id)},
    )
    assert "unknown" in result[0].text


def test_concrete_workspace_stop_payload_committed_at_allocation(runner, workspace):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    row = task.lifecyclecommand
    assert row.event == "task:stop_workspace"
    assert row.target == str(workspace.id)
    assert row.payload["workspace_id"] == str(workspace.id)
    assert row.payload["task_id"] == str(task.id)


def test_is_active_setter_cannot_revive_retired_definition(runner, user):
    from apps.runners.models import ImageDefinition

    definition = ImageDefinition.objects.create(
        organization=runner.organization,
        created_by=user,
        name="retired",
        runtime_type="docker",
        status="pending_deletion",
    )
    with pytest.raises(ConflictError):
        definition.is_active = True
    definition.refresh_from_db()
    assert definition.status == "pending_deletion"


def test_unrecorded_interrupted_allocation_release_requires_current_scan(
    runner, workspace
):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="create_workspace",
    )
    OperationRepository.intervene(task, "Unknown allocation interruption")
    row = task.lifecyclecommand
    evidence = {
        "status": "unknown",
        "instance_id": "exclusive",
        "execution_finished": True,
        "outcome_known": False,
        "quiescent": True,
        "identity": OperationRepository.envelope(row, {}),
    }
    with pytest.raises(ConflictError):
        DispositionRepository.dispose(
            row, {**evidence, "quiescent": False}, "acknowledge_interrupted"
        )
    assert DispositionRepository.dispose(row, evidence, "acknowledge_interrupted")[
        "released"
    ]
    workspace.refresh_from_db()
    assert workspace.status == "failed" and workspace.current_task_id is None


def test_other_owner_requires_admin_and_other_org_hidden(runner, workspace, user):
    from django.contrib.auth import get_user_model
    from apps.organizations.models import Membership, Organization
    from common.exceptions import AuthenticationError, NotFoundError

    other = get_user_model().objects.create_user(
        email="disposition-other@example.com", password="secret"
    )
    Membership.objects.create(
        user=other, organization=runner.organization, role="member"
    )
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    with pytest.raises(AuthenticationError):
        DispositionRepository.authorized(other, runner.organization_id, task.id)
    Membership.objects.filter(user=other).update(role="admin")
    assert (
        DispositionRepository.authorized(other, runner.organization_id, task.id).task_id
        == task.id
    )
    foreign = Organization.objects.create(
        name="foreign-disposition", slug="foreign-disposition"
    )
    Membership.objects.create(user=other, organization=foreign, role="admin")
    with pytest.raises(NotFoundError):
        DispositionRepository.authorized(other, foreign.id, task.id)


def test_permitted_actions_are_current_evidence_not_task_status(
    runner, workspace, organization
):
    task = TaskRepository.create(
        task_id=uuid.uuid4(),
        runner=runner,
        workspace=workspace,
        task_type="stop_workspace",
    )
    row = task.lifecyclecommand
    assert (
        DispositionRepository.permitted_actions(
            row, {}, workspace.created_by, organization.id
        )
        == []
    )
    evidence = {
        "status": "terminal",
        "instance_id": "exclusive",
        "quiescent": True,
        "outcome_known": True,
        "execution_finished": True,
        "identity": OperationRepository.envelope(row, {}),
        "event": "workspace:error",
    }
    assert DispositionRepository.permitted_actions(
        row, evidence, workspace.created_by, organization.id
    ) == ["reconcile", "retry"]
    assert "retry" not in DispositionRepository.permitted_actions(
        row, {**evidence, "quiescent": False}, workspace.created_by, organization.id
    )
