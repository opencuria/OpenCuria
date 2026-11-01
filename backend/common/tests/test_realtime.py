"""Characterize the shared Socket.IO server and workspace event fan-out."""

from __future__ import annotations

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from django.test import override_settings

from common import realtime


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isolate the process singleton and both subscription indexes."""
    server = SimpleNamespace(emit=AsyncMock())
    monkeypatch.setattr(realtime, "_sio", server)
    monkeypatch.setattr(realtime, "_frontend_subscriptions", defaultdict(set))
    monkeypatch.setattr(realtime, "_frontend_sid_workspaces", defaultdict(set))
    return server


@pytest.mark.parametrize("redis_url", [None, "redis://cache:6380/2"])
def test_server_is_lazy_shared_and_preserves_transport_settings(
    monkeypatch: pytest.MonkeyPatch, redis_url: str | None
) -> None:
    """Retain one handler-free server, Redis channel and connection limits."""
    monkeypatch.setattr(realtime, "_sio", None)
    monkeypatch.delenv("REDIS_URL", raising=False)
    if redis_url:
        monkeypatch.setenv("REDIS_URL", redis_url)
    manager = Mock()
    server = Mock()
    monkeypatch.setattr(realtime.socketio, "AsyncRedisManager", manager)
    monkeypatch.setattr(realtime.socketio, "AsyncServer", server)
    manager.assert_not_called()
    server.assert_not_called()

    with override_settings(SIO_CORS_ALLOWED_ORIGINS=["https://chat.example.com"]):
        assert realtime.get_sio_server() is server.return_value
        assert realtime.get_sio_server() is server.return_value

    manager.assert_called_once_with(
        redis_url or "redis://localhost:6379/0", channel="opencuria-runner"
    )
    server.assert_called_once_with(
        async_mode="asgi",
        client_manager=manager.return_value,
        cors_allowed_origins=["https://chat.example.com"],
        logger=False,
        engineio_logger=False,
        max_http_buffer_size=200 * 1024 * 1024,
        monitor_clients=True,
        ping_interval=25,
        ping_timeout=20,
    )


@pytest.mark.asyncio
async def test_fanout_is_scoped_and_duplicate_subscriptions_are_idempotent(
    transport: SimpleNamespace,
) -> None:
    """Deliver the unchanged event once per subscribed sid and workspace."""
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("second", "workspace")
    realtime.subscribe_frontend("outsider", "other")
    payload = {"workspace_id": "workspace", "status": "running"}

    await realtime.emit_to_frontend("workspace:status_changed", payload, "workspace")

    assert transport.emit.await_count == 2
    assert {call.kwargs["to"] for call in transport.emit.await_args_list} == {
        "first", "second"
    }
    for call in transport.emit.await_args_list:
        assert call.args == ("workspace:status_changed", payload)
        assert call.kwargs["namespace"] == "/frontend"


@pytest.mark.asyncio
async def test_unsubscribe_preserves_other_clients_and_workspaces(
    transport: SimpleNamespace,
) -> None:
    """Removing one subscription cannot drop unrelated subscriptions."""
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("first", "other")
    realtime.subscribe_frontend("second", "workspace")
    realtime.unsubscribe_frontend("first", "workspace")
    realtime.unsubscribe_frontend("first", "workspace")

    await realtime.emit_to_frontend("event", {}, "workspace")
    transport.emit.assert_awaited_once_with(
        "event", {}, to="second", namespace="/frontend"
    )
    assert realtime._frontend_sid_workspaces["first"] == {"other"}


@pytest.mark.asyncio
async def test_disconnect_removes_all_client_subscriptions(
    transport: SimpleNamespace,
) -> None:
    """Drop the disconnected sid from both indexes, retaining other clients."""
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("first", "other")
    realtime.subscribe_frontend("second", "workspace")
    realtime.drop_frontend("first")
    realtime.drop_frontend("first")
    assert "first" not in realtime._frontend_sid_workspaces
    assert "other" not in realtime._frontend_subscriptions

    await realtime.emit_to_frontend("event", {}, "other")
    transport.emit.assert_not_awaited()
    await realtime.emit_to_frontend("event", {}, "workspace")
    transport.emit.assert_awaited_once_with(
        "event", {}, to="second", namespace="/frontend"
    )


@pytest.mark.asyncio
async def test_failed_client_does_not_interrupt_fanout(
    transport: SimpleNamespace,
) -> None:
    """A delivery failure is contained so remaining subscribers get the event."""
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("second", "workspace")
    transport.emit.side_effect = [RuntimeError("connection lost"), None]

    await realtime.emit_to_frontend("event", {}, "workspace")

    assert transport.emit.await_count == 2
    assert {call.kwargs["to"] for call in transport.emit.await_args_list} == {
        "first", "second"
    }


@pytest.mark.asyncio
async def test_subscription_changes_during_delivery_do_not_break_iteration(
    transport: SimpleNamespace,
) -> None:
    """Use a snapshot when an awaited emit lets a client disconnect."""
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("second", "workspace")

    async def disconnect(*args: object, **kwargs: object) -> None:
        realtime.drop_frontend(str(kwargs["to"]))

    transport.emit.side_effect = disconnect
    await realtime.emit_to_frontend("event", {}, "workspace")
    assert transport.emit.await_count == 2
    assert not realtime._frontend_subscriptions
