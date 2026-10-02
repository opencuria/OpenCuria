import pytest

from apps.harness.models import HarnessSessionStatus
from apps.harness.repositories import HarnessSessionRepository
from apps.runners.enums import WorkspaceOperation


@pytest.mark.django_db(transaction=True)
def test_scheduled_run_admission_rejects_active_workspace_lifecycle_operation(
    harness_workspace,
):
    workspace = harness_workspace
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
    )
    workspace.active_operation = WorkspaceOperation.STOPPING
    workspace.save(update_fields=["active_operation"])

    reserved = HarnessSessionRepository.reserve_workspace_run(
        session.id, scheduled=True
    )
    assert not reserved
    session.refresh_from_db()
    assert session.status == HarnessSessionStatus.IDLE


@pytest.mark.django_db(transaction=True)
def test_scheduled_run_admission_accepts_idle_running_workspace(harness_workspace):
    workspace = harness_workspace
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
    )

    assert HarnessSessionRepository.reserve_workspace_run(session.id, scheduled=True)
    session.refresh_from_db()
    assert session.status == HarnessSessionStatus.BUSY
