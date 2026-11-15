"""The supervisor receives health only after successful recovery progress."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from apps.runners.management.commands import recover_lifecycle


@pytest.fixture
def broker(monkeypatch):
    factory = MagicMock()
    client = AsyncMock()
    factory.from_url.return_value.__aenter__.return_value = client
    monkeypatch.setattr(recover_lifecycle, "Redis", factory)
    return client


@pytest.mark.asyncio
async def test_worker_readiness_watchdog_and_recovery_after_failed_tick(
    monkeypatch, broker
):
    service = Mock(tick=AsyncMock(side_effect=[RuntimeError("db busy"), None, None]))
    monkeypatch.setattr(recover_lifecycle, "RecoveryService", lambda: service)
    monkeypatch.setattr(recover_lifecycle, "close_old_connections", Mock())
    sleep = AsyncMock(side_effect=[None, None, asyncio.CancelledError])
    monkeypatch.setattr(recover_lifecycle.asyncio, "sleep", sleep)
    notify = Mock()
    monkeypatch.setattr(recover_lifecycle, "notify_systemd", notify)
    with pytest.raises(asyncio.CancelledError):
        await recover_lifecycle.Command().run()
    notifications = [call.args[0] for call in notify.call_args_list]
    assert notifications[0].startswith("STATUS=")
    assert "WATCHDOG=1" not in notifications[0]
    assert "READY=1" in notifications[1] and "WATCHDOG=1" in notifications[1]
    assert "WATCHDOG=1" in notifications[2] and "READY=1" not in notifications[2]
    assert [call.args[0] for call in sleep.call_args_list] == [4, 2, 2]
    assert service.tick.await_count == 3
    assert broker.ping.await_count == 3


@pytest.mark.asyncio
async def test_failing_worker_never_reports_ready_or_renews_watchdog(
    monkeypatch, broker
):
    service = Mock(tick=AsyncMock(side_effect=RuntimeError("db busy")))
    monkeypatch.setattr(recover_lifecycle, "RecoveryService", lambda: service)
    monkeypatch.setattr(recover_lifecycle, "close_old_connections", Mock())
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError])
    monkeypatch.setattr(recover_lifecycle.asyncio, "sleep", sleep)
    notify = Mock()
    monkeypatch.setattr(recover_lifecycle, "notify_systemd", notify)
    with pytest.raises(asyncio.CancelledError):
        await recover_lifecycle.Command().run()
    assert all(
        "READY=1" not in call.args[0] and "WATCHDOG=1" not in call.args[0]
        for call in notify.call_args_list
    )
    assert [call.args[0] for call in sleep.call_args_list] == [4, 8]


@pytest.mark.asyncio
async def test_redis_outage_does_not_report_healthy_or_mutate_commands(
    monkeypatch, broker
):
    broker.ping.side_effect = ConnectionError("broker unavailable")
    service = Mock(tick=AsyncMock())
    monkeypatch.setattr(recover_lifecycle, "RecoveryService", lambda: service)
    monkeypatch.setattr(
        recover_lifecycle.asyncio,
        "sleep",
        AsyncMock(side_effect=asyncio.CancelledError),
    )
    notify = Mock()
    monkeypatch.setattr(recover_lifecycle, "notify_systemd", notify)
    with pytest.raises(asyncio.CancelledError):
        await recover_lifecycle.Command().run()
    service.tick.assert_not_awaited()
    assert "WATCHDOG=1" not in notify.call_args.args[0]
    assert "READY=1" not in notify.call_args.args[0]
