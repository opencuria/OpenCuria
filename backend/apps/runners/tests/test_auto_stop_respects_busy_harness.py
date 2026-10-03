from datetime import timedelta
from types import SimpleNamespace

from django.utils import timezone

from apps.runners.enums import WorkspaceStatus
from apps.runners.services.domains.heartbeat_reconciler import HeartbeatReconcilerMixin


def test_workspace_auto_stop_never_stops_a_busy_harness_session():
    workspace = SimpleNamespace(
        status=WorkspaceStatus.RUNNING,
        active_operation=None,
        current_task_id=None,
        has_active_harness_session=True,
        last_activity_at=timezone.now() - timedelta(hours=12),
        runner=SimpleNamespace(
            is_online=True,
            organization=SimpleNamespace(workspace_auto_stop_timeout_minutes=1),
        ),
    )

    assert not HeartbeatReconcilerMixin()._should_auto_stop_workspace(workspace)


def test_workspace_auto_stop_still_stops_idle_workspace():
    workspace = SimpleNamespace(
        status=WorkspaceStatus.RUNNING,
        active_operation=None,
        current_task_id=None,
        has_active_harness_session=False,
        last_activity_at=timezone.now() - timedelta(hours=12),
        runner=SimpleNamespace(
            is_online=True,
            organization=SimpleNamespace(workspace_auto_stop_timeout_minutes=1),
        ),
    )

    assert HeartbeatReconcilerMixin()._should_auto_stop_workspace(workspace)


def test_intervention_fence_prevents_idle_stop():
    workspace = SimpleNamespace(
        status=WorkspaceStatus.RUNNING,
        active_operation=None,
        current_task_id="failed-intervention-task",
    )
    assert not HeartbeatReconcilerMixin()._should_auto_stop_workspace(workspace)
