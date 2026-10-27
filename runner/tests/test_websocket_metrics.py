"""Contract and lifecycle checks for the Socket.IO metrics reporting loop."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface


@pytest.fixture
def interface(tmp_path: Path) -> Iterator[WebSocketInterface]:
    """Provide an isolated metrics owner with a fake runtime and transport."""
    service = SimpleNamespace(
        registry=SimpleNamespace(get_vm_metrics=AsyncMock(return_value={}))
    )
    result = WebSocketInterface(service, RunnerSettings(state_dir=str(tmp_path)))
    result._sio.emit = AsyncMock()
    yield result
    result._operations.journal.close()


async def test_metrics_loop_preserves_wire_payload_and_cpu_warmup(
    interface: WebSocketInterface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve the exact event payload, cadence and host CPU warmup."""
    now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "src.services.system_metrics.datetime",
        SimpleNamespace(now=lambda **kwargs: now),
    )
    cpu = Mock(return_value=42.5)
    monkeypatch.setattr("src.services.system_metrics.psutil.cpu_percent", cpu)
    monkeypatch.setattr(
        "src.services.system_metrics.psutil.virtual_memory",
        Mock(return_value=SimpleNamespace(used=1024, total=4096)),
    )
    disk = Mock(return_value=SimpleNamespace(used=8192, total=16384))
    monkeypatch.setattr("src.services.system_metrics.psutil.disk_usage", disk)
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr("src.interfaces.websocket.asyncio.sleep", sleep)
    interface._sio.connected = True
    interface._service.registry.get_vm_metrics.return_value = {
        "ws": {
            "cpu_time_ns": 2_000_000_000,
            "vcpu_count": 2,
            "ram_used_bytes": "100",
            "ram_total_bytes": 200,
            "disk_used_bytes": 300,
            "disk_total_bytes": 400,
            "guest_disk_used_bytes": 500,
            "guest_disk_free_bytes": 600,
            "guest_disk_total_bytes": 1100,
            "runtime_private_field": "excluded",
        }
    }

    await interface._metrics_loop()

    assert cpu.call_args_list == [call(interval=None), call(1)]
    assert [call.args for call in sleep.await_args_list] == [(60,), (60,)]
    disk.assert_called_once_with(interface._system_metrics.disk_usage_path)
    interface._service.registry.get_vm_metrics.assert_awaited_once_with()
    interface._sio.emit.assert_awaited_once_with(
        "runner:system_metrics",
        {
            "timestamp": "2026-10-26T12:00:00+00:00",
            "cpu_usage_percent": 42.5,
            "ram_used_bytes": 1024,
            "ram_total_bytes": 4096,
            "disk_used_bytes": 8192,
            "disk_total_bytes": 16384,
            "vm_metrics": {
                "ws": {
                    "cpu_usage_percent": 0.0,
                    "ram_used_bytes": 100,
                    "ram_total_bytes": 200,
                    "disk_used_bytes": 300,
                    "disk_total_bytes": 400,
                    "guest_disk_used_bytes": 500,
                    "guest_disk_free_bytes": 600,
                    "guest_disk_total_bytes": 1100,
                }
            },
        },
    )


async def test_metrics_loop_skips_disconnected_samples(
    interface: WebSocketInterface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An offline connection neither samples VMs nor publishes observations."""
    monkeypatch.setattr("src.services.system_metrics.psutil.cpu_percent", Mock())
    monkeypatch.setattr(
        "src.interfaces.websocket.asyncio.sleep",
        AsyncMock(side_effect=[None, asyncio.CancelledError()]),
    )
    await interface._metrics_loop()
    interface._service.registry.get_vm_metrics.assert_not_awaited()
    interface._sio.emit.assert_not_awaited()


async def test_metrics_loop_recovers_after_collection_failure(
    interface: WebSocketInterface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed sample must not end the reporting loop or send a partial event."""
    monkeypatch.setattr(
        "src.services.system_metrics.psutil.cpu_percent", Mock(return_value=1)
    )
    memory = SimpleNamespace(used=1, total=2)
    monkeypatch.setattr(
        "src.services.system_metrics.psutil.virtual_memory", lambda: memory
    )
    monkeypatch.setattr(
        "src.services.system_metrics.psutil.disk_usage", lambda _: memory
    )
    monkeypatch.setattr(
        "src.interfaces.websocket.asyncio.sleep",
        AsyncMock(side_effect=[None, None, asyncio.CancelledError()]),
    )
    interface._sio.connected = True
    interface._service.registry.get_vm_metrics.side_effect = [
        OSError("runtime gone"), {}
    ]
    await interface._metrics_loop()
    assert interface._service.registry.get_vm_metrics.await_count == 2
    interface._sio.emit.assert_awaited_once()
    assert interface._sio.emit.await_args.args[0] == "runner:system_metrics"
