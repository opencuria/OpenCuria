"""Isolated execution worker for :mod:`src.supervisor`.

The worker reuses the real WebSocketInterface handlers, but its Socket.IO-like
adapter forwards registration, emit and ACK calls over authenticated local IPC.
It never opens the backend network connection or a Journal database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Callable
from typing import Any

import structlog

from .config import RunnerSettings
from .main import build_workspace_service
from .supervised_ipc import IPCError, IPCPeer, connect_private_socket

logger = structlog.get_logger(__name__)


class WorkerSocket:
    """Minimal Socket.IO client interface used by existing runner handlers."""

    def __init__(self) -> None:
        self.handlers: dict[str, dict[str, Callable]] = {"/": {}}
        self.peer: IPCPeer | None = None
        self.connected = False
        self.journal = None

    def on(self, event: str, handler: Callable | None = None):
        """Register the existing handler for later supervisor dispatch."""

        def register(callback: Callable):
            self.handlers["/"][event] = callback
            return callback

        return register(handler) if handler is not None else register

    def event(self, handler: Callable):
        """Register a named Socket.IO lifecycle callback."""
        return self.on(handler.__name__, handler)

    async def emit(self, event: str, data=None, *args, **kwargs):
        """Forward event to the one backend transport owner and await acceptance."""
        if self.peer is None:
            raise IPCError("Supervisor IPC is unavailable; backend event not delivered")
        result = await self.peer.call(
            "backend.emit", {"event": event, "data": data or {}}, timeout=None
        )
        if event.startswith("terminal:") and isinstance(result, dict):
            return result.get("response")
        return result

    async def call(self, event: str, data=None, *args, **kwargs):
        """Proxy ACKed backend calls, preserving Socket.IO ACK/backpressure."""
        if self.peer is None:
            raise IPCError("Supervisor IPC is unavailable; backend call not delivered")
        timeout = kwargs.get("timeout", 60)
        if event.startswith("terminal:"):
            return await self.peer.call(
                "terminal.call",
                {"event": event, "data": data or {}},
                timeout=float(timeout) + 5 if timeout else None,
            )
        return await self.peer.call(
            "backend.call",
            {"event": event, "data": data or {}, "timeout": timeout},
            timeout=(float(timeout) + 5 if timeout else None),
        )

    async def request(self, event: str, data=None, *args, **kwargs):
        """Socket.IO request compatibility; dispatch same callback remotely."""
        if self.peer is None:
            raise IPCError("Supervisor IPC is unavailable")
        if event == "runner:request_route":
            return await self.peer.call(
                "runner.route",
                {"event": event, "data": data or {}},
                timeout=kwargs.get("timeout"),
            )
        return await self.peer.call(
            "worker.inbound",
            {"event": event, "data": data or {}},
            timeout=kwargs.get("timeout"),
        )


async def run_worker() -> None:
    """Build one isolated service and serve registered handler RPCs."""
    settings = RunnerSettings(_env_file=None)
    service = await build_workspace_service(settings, logger)
    gateway = WorkerSocket()
    interface = None
    try:
        from .interfaces.websocket import WebSocketInterface

        interface = WebSocketInterface(
            service,
            settings,
            sio_client=gateway,
            own_journal=False,
        )
        # A generation shares the supervisor's desktop lease epoch and SQLite
        # store. Managers are generation-local; routing pins all live session
        # ownership to exactly one generation/workspace.
        service.configure_desktop_leases(
            settings.state_dir,
            os.environ.get("OPENCURIA_DESKTOP_EPOCH", interface._desktop_epoch),
        )
        interface._desktop_epoch = os.environ.get(
            "OPENCURIA_DESKTOP_EPOCH", interface._desktop_epoch
        )
        service._journal = None
        for runtime in getattr(service, "_runtimes", {}).values():
            # Workers must never open or publish through a second Journal owner.
            if hasattr(runtime, "_publication_journal"):
                runtime._publication_journal = None
        socket_path = os.environ.get("OPENCURIA_SUPERVISOR_SOCKET", "")
        token = os.environ.get("OPENCURIA_SUPERVISOR_TOKEN", "")
        generation_id = os.environ.get("OPENCURIA_GENERATION_ID", "")
        if not socket_path or not token or not generation_id:
            raise RuntimeError("Supervised worker authentication configuration missing")

        async def handle_supervisor_event(event: str, data: dict[str, Any]) -> None:
            await _ignore_supervisor_event(event, data, service)

        gateway.peer = await connect_private_socket(
            socket_path,
            token,
            generation_id,
            request_handler=lambda method, data: _worker_request(
                gateway, interface, service, method, data
            ),
            event_handler=handle_supervisor_event,
        )
        gateway.connected = True
        gateway.journal = None
        _start_worker_maintenance(interface, service)
        await gateway.peer.call(
            "worker.ready",
            {"events": sorted(gateway.handlers["/"])},
            timeout=15,
        )
        logger.info("supervised_worker_ready", generation=generation_id)
        await gateway.peer._reader_task
    finally:
        gateway.connected = False
        if gateway.peer is not None:
            await gateway.peer.close()
        if interface is not None:
            # Do not invoke legacy shutdown semantics (stream cleanup, journal
            # flush); explicit session ownership is drained by supervisor.
            maintenance_tasks = (
                interface._heartbeat_task,
                interface._observation_task,
                interface._health_check_task,
                interface._metrics_task,
            )
            for task in maintenance_tasks:
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in maintenance_tasks if task is not None),
                return_exceptions=True,
            )
            for task in list(interface._running_tasks.values()):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                *interface._running_tasks.values(), return_exceptions=True
            )
        for runtime in getattr(service, "_runtimes", {}).values():
            close = getattr(runtime, "close", None)
            if close is not None:
                result = close()
                if asyncio.iscoroutine(result):
                    await result


def _start_worker_maintenance(interface: Any, service: Any) -> None:
    """Start only side-effect-free health probes in an execution generation.

    Desktop lease recovery is *not* a worker maintenance loop: all generations
    share the durable lease database, and an old generation must never race the
    stable supervisor's lease arbiter. The supervisor owns observation,
    inventory and lease maintenance.
    """
    task = interface._health_check_task
    if task is None or task.done():
        interface._health_check_task = asyncio.create_task(
            service.run_health_check_loop()
        )


async def _ignore_supervisor_event(
    event: str, data: dict[str, Any], service: Any
) -> None:
    """Apply supervisor-issued lease maintenance to this generation's service."""
    if event != "worker.maintenance":
        logger.warning("unexpected_supervisor_event", event=event)
        return
    workspace_ids = data.get("workspace_ids")
    if not isinstance(workspace_ids, list):
        return
    # Workspace ownership is arbitrated by the supervisor. A worker's ordinary
    # desktop maintenance loop is replaced by these explicit scoped requests.
    allowed = [
        str(value) for value in workspace_ids if isinstance(value, str) and value
    ]
    desktop = getattr(service, "desktop", None)
    if desktop is None:
        return
    for value in allowed:
        try:
            await desktop.maintain_workspace(uuid.UUID(value))
        except Exception:
            logger.exception("worker_desktop_maintenance_failed", workspace_id=value)


async def _worker_request(
    gateway: WorkerSocket,
    interface: Any,
    service: Any,
    method: str,
    data: dict[str, Any],
) -> Any:
    """Dispatch actual handlers and preserve their Socket.IO return values."""
    if method == "worker.maintenance":
        await _ignore_supervisor_event("worker.maintenance", data, service)
        return {"accepted": True}
    if method == "worker.control":
        if data.get("action") == "stop_metrics":
            task = interface._metrics_task
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            interface._metrics_task = None
            return {"accepted": True}
        raise IPCError("Unknown worker control action")
    if method != "worker.dispatch":
        raise IPCError("Worker method is not allowed")
    event = str(data.get("event", ""))
    payload = data.get("data") or {}
    if not isinstance(payload, dict):
        raise IPCError("Worker handler payload must be an object")
    handler = gateway.handlers["/"].get(event)
    if handler is None:
        raise IPCError("Worker handler is not registered")
    # The stable supervisor serializes lifecycle commands globally by workspace;
    # refresh generation-local caches after another generation may have changed
    # runtime state. Do not refresh for live session I/O.
    if event.startswith("task:") and event in {
        "task:create_workspace",
        "task:create_workspace_from_image_artifact",
        "task:stop_workspace",
        "task:resume_workspace",
        "task:update_workspace",
        "task:remove_workspace",
        "task:build_image",
        "task:create_image_artifact",
        "task:delete_image_artifact",
        "task:inject_credentials",
    }:
        await service.sync_from_runtime()
    result = await handler(payload)
    # Worker callbacks often schedule stream/terminal readers and return before
    # completion. This is intentional: future calls route by their stable IDs.
    return {"result": result}


def main() -> None:
    """Worker module entry point."""
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception("supervised_worker_failed")
        raise


if __name__ == "__main__":
    main()
