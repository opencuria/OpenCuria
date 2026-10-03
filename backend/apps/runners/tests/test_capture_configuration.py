"""Configuration and ordinary credential sync respect capture's parent hold."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.credentials.services import CredentialSvc, ResolvedCredentials
from apps.runners.enums import WorkspaceStatus
from apps.runners.services import RunnerService, workspace_configuration
from apps.runners.services.workspace_configuration import WorkspaceConfigurationService
from common.exceptions import ConflictError, NotFoundError


@pytest.mark.django_db
@pytest.mark.parametrize("selection", ["name", "credentials", "plugins"])
def test_configuration_rechecks_capture_hold_after_stale_preflight(
    workspace, user, monkeypatch, selection
):
    service = WorkspaceConfigurationService()
    stale = service.workspaces.get_by_id(workspace.id)
    assert not stale.active_operation and not stale.current_task_id
    original_lock = workspace_configuration.lock_runner

    def acquire_capture_before_lock(runner_id):
        # Deterministically admit capture after the unlocked read, then observe
        # the inter-child gap at the authoritative lock-boundary read.
        workspace.active_operation = "capturing_image"
        workspace.current_task_id = None
        workspace.save(update_fields=["active_operation", "current_task"])
        return original_lock(runner_id)

    monkeypatch.setattr(
        workspace_configuration, "lock_runner", acquire_capture_before_lock
    )
    resolve = Mock()
    monkeypatch.setattr(service, "_resolve_final_credentials", resolve)
    kwargs = {"credentials": None, "plugin_ids": None}
    kwargs.update(
        {
            "name": {"name": "Changed"},
            "credentials": {"credentials": []},
            "plugins": {"plugin_ids": []},
        }[selection]
    )
    with pytest.raises(ConflictError, match="lifecycle outcome unresolved"):
        service.update(
            workspace_id=workspace.id,
            user=user,
            organization_id=workspace.runner.organization_id,
            **kwargs,
        )
    resolve.assert_not_called()
    workspace.refresh_from_db()
    assert workspace.name == "Fixture Workspace"
    assert not workspace.tasks.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("foreign", ["user", "organization"])
def test_configuration_capture_guard_preserves_authorization(workspace, user, foreign):
    workspace.active_operation = "capturing_image"
    workspace.save(update_fields=["active_operation"])
    with pytest.raises(NotFoundError):
        WorkspaceConfigurationService().update(
            workspace_id=workspace.id,
            user=SimpleNamespace(id=uuid.uuid4()) if foreign == "user" else user,
            organization_id=(
                uuid.uuid4()
                if foreign == "organization"
                else workspace.runner.organization_id
            ),
            credentials=None,
            plugin_ids=None,
            name="Unauthorized",
        )
    workspace.refresh_from_db()
    assert workspace.name == "Fixture Workspace"


@pytest.mark.django_db
def test_configuration_updates_after_capture_releases_hold(workspace, user):
    updated = WorkspaceConfigurationService().update(
        workspace_id=workspace.id,
        user=user,
        organization_id=workspace.runner.organization_id,
        credentials=[],
        plugin_ids=[],
        name="After capture",
    )
    assert updated.name == "After capture"


@pytest.fixture
def credential_service(monkeypatch):
    service = RunnerService(sio_server=AsyncMock())
    runner = SimpleNamespace(id=uuid.uuid4(), is_online=True)
    workspace = SimpleNamespace(
        id=uuid.uuid4(),
        runner=runner,
        status=WorkspaceStatus.RUNNING,
        active_operation="capturing_image",
        current_task_id=None,
        credentials_present=False,
    )
    service.workspaces = Mock()
    service.workspaces.list_by_runner.return_value = [workspace]
    service.workspaces.get_by_id.return_value = workspace
    service.runners = Mock()
    service.tasks = Mock()
    service._emit_to_runner = AsyncMock()
    service.auto_stop_inactive_workspaces = AsyncMock(return_value=[])
    monkeypatch.setattr(
        CredentialSvc,
        "resolve_workspace_credentials",
        Mock(return_value=ResolvedCredentials(env_vars={"TOKEN": "test-secret"})),
    )
    return service, workspace


def test_heartbeat_defers_credentials_until_capture_parent_settles(credential_service):
    service, workspace = credential_service
    report = [{"workspace_id": str(workspace.id), "status": "running"}]
    assert service.handle_heartbeat(workspace.runner, report) == []
    workspace.active_operation = None
    workspace.current_task_id = uuid.uuid4()
    assert service.handle_heartbeat(workspace.runner, report) == []
    workspace.current_task_id = None
    assert service.handle_heartbeat(workspace.runner, report) == [workspace.id]
    service.tasks.create.assert_not_called()
    service._emit_to_runner.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["capture", "unresolved"])
async def test_queued_reconciliation_rechecks_fresh_state(credential_service, hold):
    service, workspace = credential_service
    idle = SimpleNamespace(**vars(workspace))
    idle.active_operation = None
    if hold == "unresolved":
        workspace.active_operation = None
        workspace.current_task_id = uuid.uuid4()
    # The queue flush itself sees the old idle snapshot. The final fresh read
    # inside ordinary injection must still prevent task creation and emission.
    service.workspaces.get_by_id.side_effect = [idle, workspace]
    await service.dispatch_credential_reconcile([workspace.id])
    service.tasks.create.assert_not_called()
    service._emit_to_runner.assert_not_awaited()
    assert service._pending_credential_inject == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("material", [True, False])
async def test_ordinary_reconciliation_resumes_including_removal(
    credential_service, material
):
    service, workspace = credential_service
    workspace.active_operation = None
    resolved = ResolvedCredentials(
        env_vars={"TOKEN": "test-secret"} if material else {}
    )
    service._credential_resolver = Mock()
    service._credential_resolver.resolve_workspace_credentials.return_value = resolved
    await service.dispatch_credential_reconcile([workspace.id])
    service._emit_to_runner.assert_awaited_once()
    assert service._emit_to_runner.await_args.args[2]["env_vars"] == resolved.env_vars
    service.tasks.create.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_lifecycle_credential_delivery_is_not_fenced(credential_service):
    service, workspace = credential_service
    service._call_runner = AsyncMock(
        return_value={"ok": True, "credentials_present": True}
    )
    service._forward_workspace_status = Mock()
    service.workspaces.update_credentials_present.return_value = workspace
    assert (
        await service._dispatch_credential_inject(
            workspace,
            resolved=ResolvedCredentials(env_vars={"TOKEN": "test-secret"}),
            wait=True,
        )
        is True
    )
    service._call_runner.assert_awaited_once()
    service.tasks.complete.assert_called_once()
