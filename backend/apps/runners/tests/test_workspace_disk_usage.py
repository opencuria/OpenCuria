"""Workspace filesystem usage reported with runner VM metrics."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from apps.runners.disk_usage import parse_vm_disk_usage
from apps.runners.models import Runner, RunnerSystemMetrics, Workspace
from apps.runners.services import RunnerService
from common.utils import hash_token

GIB = 1024**3
OBSERVED_AT = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


def _guest(used: int, free: int, total: int) -> dict[str, int]:
    return {
        "disk_used_bytes": 1,
        "disk_total_bytes": 2,
        "guest_disk_used_bytes": used,
        "guest_disk_free_bytes": free,
        "guest_disk_total_bytes": total,
    }


def _metrics(vm_metrics: dict) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cpu_usage_percent": 1.0,
        "ram_used_bytes": 1,
        "ram_total_bytes": 2,
        "disk_used_bytes": 1,
        "disk_total_bytes": 2,
        "vm_metrics": vm_metrics,
    }


def test_parse_keeps_only_complete_plausible_guest_counters() -> None:
    valid = str(uuid.uuid4())

    usages = parse_vm_disk_usage(
        {
            valid: _guest(9 * GIB, GIB, 10 * GIB),
            str(uuid.uuid4()): {"disk_used_bytes": 1},  # guest did not answer
            str(uuid.uuid4()): _guest(1, 1, 0),  # no filesystem size
            str(uuid.uuid4()): _guest(-1, 1, 2),  # negative counter
            "not-a-uuid": _guest(1, 1, 2),
            str(uuid.uuid4()): "garbage",
        },
        observed_at=OBSERVED_AT,
    )

    assert list(usages) == [uuid.UUID(valid)]
    assert usages[uuid.UUID(valid)].free_bytes == GIB
    assert parse_vm_disk_usage(None, observed_at=OBSERVED_AT) == {}


@pytest.mark.django_db
def test_metrics_persist_disk_usage_and_notify_open_workspace(
    runner, workspace, frontend, django_capture_on_commit_callbacks
):
    service = RunnerService()

    with django_capture_on_commit_callbacks(execute=True):
        service.record_runner_system_metrics(
            runner, _metrics({str(workspace.id): _guest(9 * GIB, GIB, 10 * GIB)})
        )

    workspace.refresh_from_db()
    assert workspace.disk_used_bytes == 9 * GIB
    assert workspace.disk_free_bytes == GIB
    assert workspace.disk_total_bytes == 10 * GIB
    assert workspace.disk_usage_observed_at is not None
    frontend.assert_awaited_once()
    event, payload, room = frontend.await_args.args
    assert event == "workspace:disk_usage_changed"
    assert room == str(workspace.id)
    assert payload["workspace_id"] == str(workspace.id)
    assert payload["disk_usage"]["used_bytes"] == 9 * GIB
    assert payload["disk_usage"]["observed_at"] == (
        workspace.disk_usage_observed_at.isoformat()
    )
    assert RunnerSystemMetrics.objects.filter(runner=runner).count() == 1


@pytest.mark.django_db
def test_metrics_ignore_workspaces_of_other_runners(
    runner, organization, user, frontend, django_capture_on_commit_callbacks
):
    other_runner = Runner.objects.create(
        name="other-runner",
        api_token_hash=hash_token("other-runner-token"),
        organization=organization,
    )
    foreign = Workspace.objects.create(runner=other_runner, created_by=user)

    with django_capture_on_commit_callbacks(execute=True):
        RunnerService().record_runner_system_metrics(
            runner, _metrics({str(foreign.id): _guest(1, 2, 3)})
        )

    foreign.refresh_from_db()
    assert foreign.disk_usage_observed_at is None
    frontend.assert_not_called()


@pytest.mark.django_db
def test_metrics_keep_last_observation_when_guest_does_not_answer(
    runner, workspace, frontend, django_capture_on_commit_callbacks
):
    service = RunnerService()
    service.record_runner_system_metrics(
        runner, _metrics({str(workspace.id): _guest(1, 2, 3)})
    )

    with django_capture_on_commit_callbacks(execute=True):
        RunnerService().record_runner_system_metrics(
            runner, _metrics({str(workspace.id): {"disk_used_bytes": 5}})
        )

    workspace.refresh_from_db()
    assert (workspace.disk_used_bytes, workspace.disk_free_bytes) == (1, 2)
    frontend.assert_not_called()
