from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.runners.exceptions import PendingDispatchError
from apps.runners.services.infra.runner_supervisor import RunnerSupervisor


def make_service(runner, *, dispatch_builds, reconcile):
    """Return coordinator dependencies for focused worker behavior tests."""
    return SimpleNamespace(
        is_active_runner_session=AsyncMock(return_value=True),
        get_runner_async=AsyncMock(return_value=runner),
        dispatch_pending_image_builds=dispatch_builds,
        dispatch_pending_image_deletions=AsyncMock(),
        dispatch_pending_workspace_deletions=AsyncMock(),
        dispatch_pending_build_job_deletions=AsyncMock(),
        reconcile_runner_snapshot=reconcile,
    )


@pytest.mark.asyncio
async def test_pending_dispatch_is_single_flight_and_status_coalesces():
    started = asyncio.Event()
    release = asyncio.Event()
    dispatch_calls = 0
    snapshots = []
    runner = SimpleNamespace(id="runner-1")

    async def dispatch(_runner):
        nonlocal dispatch_calls
        dispatch_calls += 1
        started.set()
        await release.wait()

    async def reconcile(_runner, workspaces):
        snapshots.append(workspaces)

    supervisor = RunnerSupervisor(
        make_service(runner, dispatch_builds=dispatch, reconcile=reconcile)
    )
    supervisor.schedule_drain(runner, "sid-1")
    supervisor.schedule_drain(runner, "sid-1")
    await started.wait()

    supervisor.enqueue_snapshot(runner, "sid-1", [{"revision": 1}])
    supervisor.enqueue_snapshot(runner, "sid-1", [{"revision": 2}])
    supervisor.enqueue_snapshot(runner, "sid-1", [{"revision": 3}])
    assert len(supervisor._workers) == 1
    assert supervisor._snapshots["runner-1"] == ("sid-1", [{"revision": 3}])
    release.set()
    await supervisor._workers["runner-1"]

    assert dispatch_calls == 1
    assert snapshots == [[{"revision": 3}]]
    assert supervisor._workers == {}
    assert supervisor._snapshots == {}


@pytest.mark.asyncio
async def test_superseded_worker_finishes_before_successor_work():
    started = asyncio.Event()
    release = asyncio.Event()
    dispatch_sids = []
    snapshots = []
    runner = SimpleNamespace(id="runner-1")
    supervisor = None

    async def dispatch(_runner):
        dispatch_sids.append(supervisor._desired_sid["runner-1"])
        started.set()
        await release.wait()

    async def reconcile(_runner, workspaces):
        snapshots.append(workspaces)

    supervisor = RunnerSupervisor(
        make_service(runner, dispatch_builds=dispatch, reconcile=reconcile)
    )
    supervisor.schedule_drain(runner, "sid-1")
    await started.wait()
    predecessor = supervisor._workers["runner-1"]

    supervisor.schedule_drain(runner, "sid-2")
    supervisor.enqueue_snapshot(runner, "sid-2", [{"new": True}])
    supervisor.cancel_session("runner-1", "sid-1")
    assert supervisor._workers["runner-1"] is predecessor
    assert supervisor._snapshots["runner-1"] == ("sid-2", [{"new": True}])

    release.set()
    await predecessor
    assert supervisor._workers == {}
    assert snapshots == [[{"new": True}]]
    assert dispatch_sids == ["sid-1", "sid-2"]


@pytest.mark.asyncio
async def test_dispatcher_error_defers_retry_but_reconciles_snapshot():
    runner = SimpleNamespace(id="runner-1")
    dispatch_calls = 0
    snapshots = []

    async def dispatch(_runner):
        nonlocal dispatch_calls
        dispatch_calls += 1
        if dispatch_calls == 1:
            raise RuntimeError("database query failed")

    async def reconcile(_runner, workspaces):
        snapshots.append(workspaces)

    supervisor = RunnerSupervisor(
        make_service(runner, dispatch_builds=dispatch, reconcile=reconcile)
    )
    supervisor.enqueue_snapshot(runner, "sid-1", [{"status": "running"}])
    await supervisor._workers["runner-1"]

    assert dispatch_calls == 1
    assert snapshots == [[{"status": "running"}]]
    assert supervisor._drained == set()
    assert supervisor._drain_requested == set()
    assert supervisor._workers == {}

    # A later status requests one drain retry and coalesces to the newest snapshot.
    supervisor.enqueue_snapshot(runner, "sid-1", [{"status": "stopped"}])
    await supervisor._workers["runner-1"]
    assert dispatch_calls == 2
    assert snapshots[-1] == [{"status": "stopped"}]
    assert supervisor._drained == {("runner-1", "sid-1")}
    assert supervisor._drain_requested == set()


@pytest.mark.asyncio
async def test_partial_dispatch_failure_does_not_hot_loop_and_status_retries():
    runner = SimpleNamespace(id="runner-1")
    dispatch_calls = 0
    snapshots = []

    async def dispatch(_runner):
        nonlocal dispatch_calls
        dispatch_calls += 1
        if dispatch_calls == 1:
            raise PendingDispatchError("pending image build dispatch", ["failed item"])

    async def reconcile(_runner, workspaces):
        snapshots.append(workspaces)

    supervisor = RunnerSupervisor(
        make_service(runner, dispatch_builds=dispatch, reconcile=reconcile)
    )
    supervisor.schedule_drain(runner, "sid-1")
    await supervisor._workers["runner-1"]

    assert dispatch_calls == 1
    assert supervisor._drained == set()
    assert supervisor._drain_requested == set()
    assert supervisor._workers == {}

    supervisor.enqueue_snapshot(runner, "sid-1", [{"revision": 2}])
    await supervisor._workers["runner-1"]
    assert dispatch_calls == 2
    assert snapshots == [[{"revision": 2}]]
    assert supervisor._drained == {("runner-1", "sid-1")}
