from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import socketio

from apps.runners.services.infra.runner_supervisor import RunnerSupervisor
from apps.runners.sio_server import PROTOCOL_VERSION, _register_event_handlers
from common.exceptions import AuthenticationError


class FakeSio:
    def __init__(self, session=None, *, connected=True):
        self.handlers = {}
        self.session = session or {}
        self.manager = SimpleNamespace(is_connected=lambda sid, namespace: connected)
        self.raise_session_key_error = False

    def event(self, fn):
        self.handlers[fn.__name__] = fn
        return fn

    def on(self, event):
        def decorator(fn):
            self.handlers[event] = fn
            return fn

        return decorator

    async def get_session(self, sid):
        if self.raise_session_key_error:
            raise KeyError(sid)
        return self.session

    async def save_session(self, sid, session):
        self.session = session


def valid_registration(**overrides):
    """Build a valid protocol 1 registration request."""
    return {
        "protocol_version": 1,
        "supported_runtimes": ["docker"],
        "status": "ready",
        **overrides,
    }


@pytest.mark.asyncio
async def test_connect_rejects_missing_or_mismatched_protocol(monkeypatch):
    sio = FakeSio()
    _register_event_handlers(sio)
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", Mock())
    connect = sio.handlers["connect"]

    for auth in (None, {"token": "secret"}, {"token": "secret", "protocol_version": 2}):
        with pytest.raises(socketio.exceptions.ConnectionRefusedError) as error:
            await connect("sid", {}, auth)
        assert error.value.error_args["data"] == {
            "code": "protocol_mismatch",
            "retryable": False,
        }


@pytest.mark.asyncio
async def test_connect_classifies_missing_invalid_and_transient_auth(monkeypatch):
    service = SimpleNamespace()
    service.authenticate_runner_async = AsyncMock()
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio()
    _register_event_handlers(sio)
    connect = sio.handlers["connect"]

    with pytest.raises(socketio.exceptions.ConnectionRefusedError) as missing:
        await connect("sid", {}, {"protocol_version": PROTOCOL_VERSION})
    assert missing.value.error_args["data"] == {
        "code": "missing_token",
        "retryable": False,
    }

    service.authenticate_runner_async.side_effect = AuthenticationError("no")
    with pytest.raises(socketio.exceptions.ConnectionRefusedError) as invalid:
        await connect("sid", {}, {"token": "bad", "protocol_version": 1})
    assert invalid.value.error_args["data"] == {
        "code": "invalid_token",
        "retryable": False,
    }

    service.authenticate_runner_async.side_effect = RuntimeError("database down")
    with pytest.raises(socketio.exceptions.ConnectionRefusedError) as transient:
        await connect("sid", {}, {"token": "valid", "protocol_version": 1})
    assert transient.value.error_args["data"] == {
        "code": "backend_unavailable",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_connect_authenticates_and_stores_protocol_session(monkeypatch):
    runner = SimpleNamespace(id="runner-1")
    service = SimpleNamespace(authenticate_runner_async=AsyncMock(return_value=runner))
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio()
    _register_event_handlers(sio)

    await sio.handlers["connect"](
        "sid", {}, {"token": "secret", "protocol_version": PROTOCOL_VERSION}
    )
    assert sio.session == {"runner_id": "runner-1", "protocol_version": 1}


@pytest.mark.asyncio
async def test_registration_validates_and_preserves_runtime_snapshot(monkeypatch):
    runner = SimpleNamespace(id="runner-1")
    service = _registration_service(runner)
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": "runner-1", "protocol_version": 1})
    _register_event_handlers(sio)

    for runtimes in ([], ["unknown"], ["docker", "lxc"], [1]):
        reply = await sio.handlers["runner:register"](
            "sid", valid_registration(supported_runtimes=runtimes)
        )
        assert reply == {
            "ok": False,
            "code": "invalid_registration",
            "retryable": False,
        }
    service.register_runner_async.assert_not_awaited()

    reply = await sio.handlers["runner:register"](
        "sid", valid_registration(supported_runtimes=["qemu"])
    )
    assert reply["ok"] is True
    assert (
        service.register_runner_async.await_args.kwargs["available_runtimes"]
        == ["qemu"]
    )


@pytest.mark.asyncio
async def test_registration_returns_without_waiting_for_slow_pending_dispatch(
    monkeypatch,
):
    runner = SimpleNamespace(id="runner-1")
    service = _registration_service(runner)
    slow_dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def blocked_dispatch(_runner):
        slow_dispatch_started.set()
        await release_dispatch.wait()

    service.dispatch_pending_image_builds = blocked_dispatch
    service.dispatch_pending_image_deletions = AsyncMock()
    service.dispatch_pending_workspace_deletions = AsyncMock()
    service.dispatch_pending_build_job_deletions = AsyncMock()
    supervisor = RunnerSupervisor(service)
    service.schedule_runner_drain = supervisor.schedule_drain
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": "runner-1", "protocol_version": 1})
    _register_event_handlers(sio)

    reply = await asyncio.wait_for(
        sio.handlers["runner:register"]("sid", valid_registration()), timeout=0.1
    )
    assert reply["ok"] is True
    await slow_dispatch_started.wait()
    release_dispatch.set()
    await asyncio.gather(*supervisor._workers.values())


@pytest.mark.asyncio
async def test_registration_is_idempotent_and_notifies_online_once(monkeypatch):
    runner = SimpleNamespace(id="runner-1")
    service = _registration_service(runner)
    supervisor = RunnerSupervisor(service)
    service.schedule_runner_drain = supervisor.schedule_drain
    service.reserve_runner_online_notification = supervisor.reserve_online_notification
    service.mark_runner_online_notified = supervisor.mark_online_notified
    service.release_runner_online_notification = supervisor.release_online_notification
    service.dispatch_pending_image_builds = AsyncMock()
    service.dispatch_pending_image_deletions = AsyncMock()
    service.dispatch_pending_workspace_deletions = AsyncMock()
    service.dispatch_pending_build_job_deletions = AsyncMock()
    service.reconcile_runner_snapshot = AsyncMock()
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": "runner-1", "protocol_version": 1})
    _register_event_handlers(sio)
    register = sio.handlers["runner:register"]

    first = await register("sid", valid_registration())
    second = await register("sid", valid_registration())
    assert first["ok"] and second["ok"]
    assert service.notify_runner_online_async.await_count == 1
    assert len(supervisor._workers) == 1
    for worker in supervisor._workers.values():
        worker.cancel()
    await asyncio.gather(*supervisor._workers.values(), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_register_and_disconnect_race_cannot_resurrect_online_runner(
    monkeypatch, runner
):
    from apps.runners.repositories import RunnerRepository
    from apps.runners.services import RunnerService

    runner.sid = "old-sid"
    runner.save(update_fields=["sid", "status"])
    service = RunnerService(sio_server=None)
    service.notify_runner_status_async = AsyncMock()
    service.schedule_runner_drain = Mock()
    register_entered = threading.Event()
    release_register = threading.Event()
    original_register = RunnerRepository.register_session

    def delayed_register(*args, **kwargs):
        register_entered.set()
        if not release_register.wait(timeout=5):
            raise TimeoutError("registration test barrier timed out")
        return original_register(*args, **kwargs)

    monkeypatch.setattr(
        RunnerRepository, "register_session", staticmethod(delayed_register)
    )
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": str(runner.id), "protocol_version": 1}, connected=True)
    _register_event_handlers(sio)
    register = sio.handlers["runner:register"]
    disconnect = sio.handlers["disconnect"]

    register_task = asyncio.create_task(
        register("new-sid", valid_registration(supported_runtimes=["docker", "qemu"]))
    )
    assert await asyncio.to_thread(register_entered.wait, 2)
    disconnect_task = asyncio.create_task(disconnect("old-sid"))
    await asyncio.sleep(0)
    assert not disconnect_task.done()

    release_register.set()
    reply = await register_task
    await disconnect_task
    runner.refresh_from_db()
    assert reply["ok"] is True
    assert runner.status == "online"
    assert runner.sid == "new-sid"
    assert service.notify_runner_status_async.await_count == 1
    assert service.notify_runner_status_async.await_args.args[1] == "online"


@pytest.mark.asyncio
async def test_unknown_sid_get_session_returns_protocol_rejections(monkeypatch):
    service = _registration_service(SimpleNamespace(id="runner-1"))
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio()
    sio.raise_session_key_error = True
    _register_event_handlers(sio)

    reply = await sio.handlers["runner:register"]("gone", valid_registration())
    assert reply == {"ok": False, "code": "unauthenticated", "retryable": False}
    reply = await sio.handlers["runner:heartbeat"]("gone", {})
    assert reply == {"ok": False, "code": "stale_session", "retryable": True}


@pytest.mark.asyncio
async def test_heartbeat_only_persists_for_active_sid(monkeypatch):
    service = SimpleNamespace(
        is_active_runner_session=AsyncMock(return_value=True),
        record_runner_heartbeat=AsyncMock(return_value=True),
    )
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": "runner-1", "protocol_version": 1})
    _register_event_handlers(sio)

    ack = await sio.handlers["runner:heartbeat"]("sid", {})
    assert ack == {"ok": True, "sid": "sid", "protocol_version": 1}
    service.record_runner_heartbeat.assert_awaited_once_with("runner-1", "sid")

    service.is_active_runner_session.return_value = False
    stale = await sio.handlers["runner:heartbeat"]("sid", {})
    assert stale == {"ok": False, "code": "stale_session", "retryable": True}
    assert service.record_runner_heartbeat.await_count == 1


@pytest.mark.asyncio
async def test_stale_runner_reply_is_dropped(monkeypatch):
    service = SimpleNamespace(is_active_runner_session=AsyncMock(return_value=False))
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": "runner-1", "protocol_version": 1})
    _register_event_handlers(sio)
    assert await sio.handlers["workspace:created"]("sid", {"task_id": "x"}) is None


@pytest.mark.asyncio
async def test_register_rejects_deleted_runner_and_invalid_status(monkeypatch):
    service = _registration_service(SimpleNamespace(id="deleted"))
    service.register_runner_async = AsyncMock(return_value=None)
    monkeypatch.setattr("apps.runners.sio_server.get_runner_service", lambda: service)
    sio = FakeSio({"runner_id": "deleted", "protocol_version": 1})
    _register_event_handlers(sio)

    deleted = await sio.handlers["runner:register"]("sid", valid_registration())
    assert deleted == {"ok": False, "code": "runner_deleted", "retryable": False}
    invalid = await sio.handlers["runner:register"](
        "sid", valid_registration(status="starting")
    )
    assert invalid == {"ok": False, "code": "invalid_registration", "retryable": False}


async def _async_register(runner_id, register, runner, **kwargs):
    """Mirror repository lookup plus async registration for the test adapter."""
    if str(runner.id) != str(runner_id):
        return None
    return register(runner, **kwargs)


def _registration_service(runner):
    """Create a protocol test service with real idempotency gates."""
    service = SimpleNamespace()
    service.runner_session_lock = lambda runner_id: asyncio.Lock()
    service.get_runner_async = AsyncMock(return_value=runner)
    service.unregister_runner_async = AsyncMock(return_value=None)
    service.notify_runner_online_async = AsyncMock()
    registered = set()

    def register(runner_arg, *, sid, available_runtimes):
        changed = sid not in registered
        registered.add(sid)
        runner_arg._registration_changed = changed
        runner_arg._superseded_sid = None
        runner_arg.available_runtimes = available_runtimes
        return runner_arg

    service.register_runner = Mock(side_effect=register)
    async def register_async(runner_id, **kwargs):
        if str(runner.id) != str(runner_id):
            return None
        return register(runner, **kwargs)

    service.register_runner_async = AsyncMock(side_effect=register_async)
    service.cancel_runner_session_work = Mock()
    service.is_active_runner_session = AsyncMock(return_value=True)
    service.notify_runner_online = Mock()
    notified = set()

    def reserve(runner_id, sid):
        key = (runner_id, sid)
        if key in notified:
            return False
        notified.add(key)
        return True

    service.reserve_runner_online_notification = reserve
    service.mark_runner_online_notified = Mock()
    service.release_runner_online_notification = Mock()
    service.schedule_runner_drain = Mock()
    service.cancel_runner_session_work = Mock()
    return service
