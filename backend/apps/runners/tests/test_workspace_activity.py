"""Characterize activity flags on public workspace reads."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.models import HarnessSession
from apps.runners.exceptions import WorkspaceNotFoundError
from apps.runners.models import Workspace
from apps.runners.services import RunnerService


@pytest.fixture
def activity_workspaces(runner, user) -> dict[uuid.UUID, bool]:
    """Mix empty, idle, busy-root and busy-child workspaces."""
    expected = {}
    for kind in ("empty", "idle", "busy_root", "busy_child"):
        workspace = Workspace.objects.create(runner=runner, created_by=user, name=kind)
        expected[workspace.id] = kind.startswith("busy")
        if kind == "empty":
            continue
        root = HarnessSession.objects.create(
            workspace=workspace,
            organization_id=runner.organization_id,
            status="busy" if kind == "busy_root" else "idle",
        )
        if kind == "busy_child":
            HarnessSession.objects.create(
                workspace=workspace,
                organization_id=runner.organization_id,
                parent=root,
                status="busy",
            )
    return expected


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["all", "runner", "organization", "user"])
def test_workspace_lists_report_any_busy_session(
    activity_workspaces, runner, user, scope
) -> None:
    """All list paths preserve idle/empty flags and include busy children."""
    filters = {
        "all": {},
        "runner": {"runner_id": runner.id},
        "organization": {"organization_id": runner.organization_id},
        "user": {"organization_id": runner.organization_id, "user": user},
    }
    workspaces = RunnerService().list_workspaces(**filters[scope])
    assert {
        workspace.id: workspace.has_active_harness_session for workspace in workspaces
    } == activity_workspaces


@pytest.mark.django_db
@pytest.mark.parametrize("read", ["get_workspace", "get_workspace_or_none"])
def test_workspace_details_refresh_activity(activity_workspaces, read) -> None:
    """Details include current activity rather than a cached session state."""
    lookup = getattr(RunnerService(), read)
    for workspace_id, expected in activity_workspaces.items():
        assert lookup(workspace_id).has_active_harness_session is expected
    HarnessSession.objects.update(status="idle")
    for workspace_id in activity_workspaces:
        assert lookup(workspace_id).has_active_harness_session is False


@pytest.mark.django_db
def test_missing_workspace_read_contract() -> None:
    """The optional lookup returns None; the required lookup still raises."""
    service = RunnerService()
    missing = uuid.uuid4()
    assert service.get_workspace_or_none(missing) is None
    with pytest.raises(WorkspaceNotFoundError):
        service.get_workspace(missing)
