"""Runner read behavior across presence, workspace visibility and recovery."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.organizations.models import Organization
from apps.runners.enums import RunnerStatus, TaskType, WorkspaceStatus
from apps.runners.models import (
    ImageBuildJob,
    ImageDefinition,
    LifecycleCommand,
    Runner,
    Task,
    Workspace,
)
from apps.runners.services import RunnerService
from apps.runners.services.recovery import RecoveryService
from common.exceptions import ConflictError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("connection", ["current", "unknown", "superseded", "offline"])
def test_disconnect_only_affects_the_current_online_session(
    runner, monkeypatch, connection
) -> None:
    """Unknown, stale and already-offline sessions have no disconnect effects."""
    sid = runner.sid
    if connection == "superseded":
        runner.sid = "replacement-session"
    elif connection == "offline":
        runner.status = RunnerStatus.OFFLINE
    elif connection == "unknown":
        sid = "unknown-session"
    runner.save()
    service = RunnerService()
    fail_streams = Mock()
    notify = Mock()
    monkeypatch.setattr(service, "fail_streams_for_runner", fail_streams)
    monkeypatch.setattr(service, "_forward_runner_status_to_frontend", notify)

    service.unregister_runner(sid)

    runner.refresh_from_db()
    if connection == "current":
        assert runner.status == RunnerStatus.OFFLINE
        assert runner.sid == ""
        fail_streams.assert_called_once_with(str(runner.id))
        notify.assert_called_once_with(runner, "offline")
    else:
        assert runner.status == (
            RunnerStatus.OFFLINE if connection == "offline" else RunnerStatus.ONLINE
        )
        assert runner.sid == (
            "replacement-session" if connection == "superseded" else "test-sid-123"
        )
        fail_streams.assert_not_called()
        notify.assert_not_called()


@pytest.mark.parametrize("scope", ["all", "organization", "runner", "runner_priority"])
@pytest.mark.parametrize("owned_only", [False, True])
def test_workspace_listing_preserves_scope_owner_and_order(
    runner, offline_runner, user, scope, owned_only
) -> None:
    """Runner scope takes precedence; owner filtering applies in every scope."""
    other_user = get_user_model().objects.create_user(email="other-owner@example.com")
    foreign_org = Organization.objects.create(name="Foreign", slug="foreign")
    foreign_runner = Runner.objects.create(
        name="Foreign", organization=foreign_org, api_token_hash="foreign-token"
    )
    rows = [
        Workspace.objects.create(runner=host, name="Workspace", created_by=owner)
        for host in (runner, offline_runner, foreign_runner)
        for owner in (user, other_user)
    ]
    filters = {}
    if scope == "organization":
        filters["organization_id"] = runner.organization_id
    elif scope in {"runner", "runner_priority"}:
        filters["runner_id"] = runner.id
        if scope == "runner_priority":
            filters["organization_id"] = foreign_org.id
    if owned_only:
        filters["user"] = user
    expected = [
        row.id
        for row in reversed(rows)
        if (not owned_only or row.created_by_id == user.id)
        and (scope != "organization" or row.runner_id != foreign_runner.id)
        and (scope not in {"runner", "runner_priority"} or row.runner_id == runner.id)
    ]

    result = RunnerService().list_workspaces(**filters)

    assert [row.id for row in result] == expected
    assert all(hasattr(row, "has_active_harness_session") for row in result)


@pytest.mark.parametrize("status", ["online", "offline"])
def test_runner_status_events_preserve_workspace_recipients(
    runner, offline_runner, user, monkeypatch, status
) -> None:
    """Only removed/failed workspaces are excluded, and runner scope is exact."""
    workspaces = [
        Workspace.objects.create(
            runner=runner, name=state, status=state, created_by=user
        )
        for state in WorkspaceStatus.values
    ]
    Workspace.objects.create(
        runner=offline_runner, name="Other runner", created_by=user
    )
    forwarded = Mock()
    service = RunnerService()
    monkeypatch.setattr(service, "_forward_to_frontend", forwarded)

    service._forward_runner_status_to_frontend(runner, status)

    assert [call.args for call in forwarded.call_args_list] == [
        (
            f"runner:{status}",
            {"workspace_id": str(row.id), "runner_id": str(runner.id)},
            str(row.id),
        )
        for row in reversed(workspaces)
        if row.status not in {WorkspaceStatus.REMOVED, WorkspaceStatus.FAILED}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", ImageBuildJob.Status.values)
async def test_restore_failed_definition_waits_only_for_its_live_deletions(
    runner, child_status
) -> None:
    """Pending/deleting child jobs block restore; all other child states allow it."""
    definition = ImageDefinition.objects.create(
        organization=runner.organization,
        name="Restore",
        status=ImageDefinition.Status.DELETE_FAILED,
    )
    ImageBuildJob.objects.create(
        image_definition=definition, runner=runner, status=child_status
    )
    other = ImageDefinition.objects.create(
        organization=runner.organization, name="Unrelated"
    )
    ImageBuildJob.objects.create(
        image_definition=other, runner=runner, status=ImageBuildJob.Status.DELETING
    )
    blocked = child_status in {
        ImageBuildJob.Status.PENDING_DELETION,
        ImageBuildJob.Status.DELETING,
    }

    if blocked:
        with pytest.raises(ConflictError, match="removal is still in progress"):
            await RunnerService().activate_image_definition(definition.id)
    else:
        await RunnerService().activate_image_definition(definition.id)

    definition.refresh_from_db()
    assert definition.status == (
        ImageDefinition.Status.DELETE_FAILED
        if blocked
        else ImageDefinition.Status.ACTIVE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "runner_status", "sid", "inspect"),
    [
        ("intervention", "online", "connected", True),
        ("intervention", "online", "", False),
        ("intervention", "offline", "connected", False),
        ("intent", "online", "connected", False),
        ("executing", "online", "connected", False),
        ("result", "online", "connected", False),
    ],
)
async def test_recovery_inspects_only_interventions_on_connected_online_runners(
    runner, phase, runner_status, sid, inspect
) -> None:
    """Inspection preserves the attempt envelope even when the task has failed."""
    runner.status = runner_status
    runner.sid = sid
    runner.last_heartbeat_at = timezone.now()
    runner.save()
    task = Task.objects.create(
        runner=runner, type=TaskType.STOP_WORKSPACE, status="failed"
    )
    command = LifecycleCommand.objects.create(
        task=task,
        phase=phase,
        target="original-target",
        attempt=3,
        deadline_at=timezone.now() + timedelta(hours=2),
    )
    transport = AsyncMock()

    await RecoveryService().tick(transport)

    if inspect:
        transport.emit.assert_awaited_once_with(
            "operation:inspect_request",
            {
                "operation_id": str(command.task_id),
                "attempt": 3,
                "target": "original-target",
                "runner_id": str(runner.id),
            },
            room=sid,
        )
    else:
        transport.emit.assert_not_awaited()
