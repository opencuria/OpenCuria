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
async def test_workspace_room_publication_does_not_require_local_subscriptions(
    transport,
):
    """An execution process can reach authorized API rooms without local sids."""
    payload = {"workspace_id": "workspace", "status": "running"}
    await realtime.emit_to_frontend("workspace:status_changed", payload, "workspace")
    transport.emit.assert_awaited_once_with(
        "workspace:status_changed",
        payload,
        to="workspace:workspace",
        namespace="/frontend",
    )


def test_subscription_indexes_are_idempotent_and_workspace_scoped(transport):
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("first", "workspace")
    realtime.subscribe_frontend("first", "other")
    realtime.subscribe_frontend("second", "workspace")
    assert realtime._frontend_subscriptions["workspace"] == {"first", "second"}
    realtime.unsubscribe_frontend("first", "workspace")
    realtime.unsubscribe_frontend("first", "workspace")
    assert realtime._frontend_subscriptions["workspace"] == {"second"}
    assert realtime._frontend_sid_workspaces["first"] == {"other"}
    realtime.drop_frontend("first")
    realtime.drop_frontend("first")
    assert "other" not in realtime._frontend_subscriptions
    assert realtime._frontend_subscriptions["workspace"] == {"second"}


@pytest.mark.asyncio
async def test_room_delivery_failure_does_not_abort_later_publication(transport):
    transport.emit.side_effect = [RuntimeError("connection lost"), None]
    await realtime.emit_to_frontend("event", {}, "one")
    await realtime.emit_to_frontend("event", {}, "two")
    assert [call.kwargs["to"] for call in transport.emit.await_args_list] == [
        "workspace:one",
        "workspace:two",
    ]
