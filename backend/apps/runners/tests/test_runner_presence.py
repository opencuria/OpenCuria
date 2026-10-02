from __future__ import annotations

import asyncio
import threading

import pytest
from asgiref.sync import sync_to_async

from apps.runners.models import Runner
from apps.runners.repositories import RunnerRepository
from apps.runners.services import RunnerService


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_presence_lane_is_not_blocked_by_thread_sensitive_reconciliation(runner):
    reconcile_started = threading.Event()
    release_reconcile = threading.Event()

    def blocked_reconcile():
        reconcile_started.set()
        if not release_reconcile.wait(timeout=5):
            raise TimeoutError("test did not release simulated reconciliation")

    service = RunnerService(sio_server=None)
    reconcile_task = asyncio.create_task(sync_to_async(blocked_reconcile)())
    assert await asyncio.to_thread(reconcile_started.wait, 2)
    try:
        active = await asyncio.wait_for(
            service.is_active_runner_session(str(runner.id), runner.sid),
            timeout=1,
        )
        heartbeat_recorded = await asyncio.wait_for(
            service.record_runner_heartbeat(str(runner.id), runner.sid),
            timeout=1,
        )
        assert active is True
        assert heartbeat_recorded is True
        assert not reconcile_task.done()
    finally:
        release_reconcile.set()
        await reconcile_task


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_registration_and_disconnect_presence_calls_bypass_blocked_shared_lane(
    runner, monkeypatch
):
    """Connect/disconnect control paths finish while reconciliation holds asgiref's lane."""
    registration_started = threading.Event()
    release_registration = threading.Event()
    disconnect_started = threading.Event()
    release_disconnect = threading.Event()
    shared_lane_started = threading.Event()
    release_shared_lane = threading.Event()
    original_register = RunnerRepository.register_session
    original_offline = RunnerRepository.set_offline_for_sid

    def block_register(*args, **kwargs):
        registration_started.set()
        if not release_registration.wait(timeout=5):
            raise TimeoutError("registration barrier wasn't released")
        return original_register(*args, **kwargs)

    def block_offline(sid):
        disconnect_started.set()
        if not release_disconnect.wait(timeout=5):
            raise TimeoutError("disconnect barrier wasn't released")
        return original_offline(sid)

    monkeypatch.setattr(
        RunnerRepository, "register_session", staticmethod(block_register)
    )
    monkeypatch.setattr(
        RunnerRepository, "set_offline_for_sid", staticmethod(block_offline)
    )

    service = RunnerService(sio_server=None)
    service.fail_streams_for_runner = lambda _runner_id: None
    runner.status = "online"
    runner.sid = "before"
    runner.save(update_fields=["status", "sid"])

    def block_shared_lane():
        shared_lane_started.set()
        if not release_shared_lane.wait(timeout=5):
            raise TimeoutError("shared lane barrier wasn't released")

    disconnect_task = asyncio.create_task(service.unregister_runner_async("before"))
    assert await asyncio.wait_for(
        asyncio.to_thread(disconnect_started.wait, 2), timeout=3
    )
    # Let the disconnect proceed into its stream cleanup. It then blocks on the
    # shared thread-sensitive lane while its post-transition status is queued.
    release_disconnect.set()
    shared_task = asyncio.create_task(sync_to_async(block_shared_lane)())
    assert await asyncio.to_thread(shared_lane_started.wait, 2)
    register_task = asyncio.create_task(
        service.register_runner_async(
            str(runner.id), sid="after-register", available_runtimes=["docker"]
        )
    )
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(registration_started.wait, 2), timeout=3
        )
        # Registration must reach its repository despite the blocked lane.
        # Disconnect has already reached its SID-conditional repository call.
        # Both presence operations have reached repository calls on their
        # dedicated lane while the thread-sensitive reconciliation is blocked.
        assert not shared_task.done()
    finally:
        release_registration.set()
        release_disconnect.set()
        release_shared_lane.set()
        await asyncio.gather(
            register_task, disconnect_task, shared_task, return_exceptions=True
        )

    registered = await register_task
    assert registered is not None and registered.sid == "after-register"
    assert await disconnect_task == str(runner.id)
