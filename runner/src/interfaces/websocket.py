"""WebSocket interface using python-socketio (async client).

Connects to the Django backend, authenticates with a Bearer token,
and listens for task events.  Harness exec output is streamed back
to the backend in real time via ``harness:*`` events.

Includes a periodic heartbeat that reports workspace container states
so the backend can reconcile its records with actual runtime state.

A separate metrics loop collects host CPU, RAM, and disk usage every
60 seconds and sends them to the backend as ``runner:system_metrics``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import psutil
import socketio
import structlog

from ..config import RunnerSettings
from ..service import WorkspaceService
from .base import Interface

logger = structlog.get_logger(__name__)


async def _stream_with_timeout(agen, timeout_s: float):
    """Yield items from *agen* enforcing an overall timeout."""
    iterator = agen.__aiter__()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError()
        try:
            item = await asyncio.wait_for(iterator.__anext__(), remaining)
        except StopAsyncIteration:
            return
        yield item


@dataclass
class DesktopProxyTunnel:
    """State for a desktop WebSocket tunnel proxied through the runner."""

    session: aiohttp.ClientSession
    websocket: aiohttp.ClientWebSocketResponse
    reader_task: asyncio.Task


class WebSocketInterface(Interface):
    """python-socketio async client that bridges backend ↔ service."""

    def __init__(self, service: WorkspaceService, settings: RunnerSettings) -> None:
        super().__init__(service)
        self._settings = settings
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,  # unlimited
            reconnection_delay=2,
            reconnection_delay_max=30,
            logger=False,
        )
        self._running_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._heartbeat_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._metrics_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._health_check_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._vm_cpu_samples: dict[str, tuple[int, float, int]] = {}
        self._disk_usage_path = self._resolve_disk_usage_path()
        self._desktop_proxy_tunnels: dict[str, DesktopProxyTunnel] = {}
        self._setup_handlers()

    async def _fetch_desktop_http(
        self,
        workspace_id: uuid.UUID,
        rest_path: str,
        query_string: str = "",
    ) -> dict[str, object]:
        """Fetch a desktop HTTP resource from the local workspace runtime."""
        container_ip = self._service.get_desktop_container_ip(workspace_id)
        upstream_url = f"http://{container_ip}:6901{rest_path}"
        if query_string:
            upstream_url = f"{upstream_url}?{query_string}"

        async with aiohttp.ClientSession() as session:
            async with session.get(upstream_url) as resp:
                headers: list[list[str]] = []
                for key, value in resp.headers.items():
                    if key.lower() in ("transfer-encoding", "connection", "keep-alive"):
                        continue
                    headers.append([key, value])

                body = await resp.read()
                return {
                    "status": resp.status,
                    "headers": headers,
                    "body": base64.b64encode(body).decode("ascii"),
                    "body_encoding": "base64",
                }

    async def _desktop_proxy_reader(
        self,
        tunnel_id: str,
        websocket: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """Forward upstream desktop WebSocket frames back to the backend."""
        close_code = 1000
        try:
            async for msg in websocket:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    await self._sio.emit(
                        "desktop:proxy_ws_frame",
                        {
                            "tunnel_id": tunnel_id,
                            "data": base64.b64encode(msg.data).decode("ascii"),
                            "encoding": "base64",
                        },
                    )
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    await self._sio.emit(
                        "desktop:proxy_ws_frame",
                        {
                            "tunnel_id": tunnel_id,
                            "text": msg.data,
                        },
                    )
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    close_code = websocket.close_code or 1000
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    close_code = 1011
                    break
        except asyncio.CancelledError:
            close_code = websocket.close_code or 1000
            raise
        except Exception:
            logger.exception("desktop_proxy_reader_failed", tunnel_id=tunnel_id)
            close_code = 1011
        finally:
            await self._finalize_desktop_proxy_tunnel(tunnel_id, close_code=close_code)

    async def _open_desktop_proxy_tunnel(
        self,
        workspace_id: uuid.UUID,
        tunnel_id: str,
        subprotocols: list[str] | None = None,
    ) -> dict[str, object]:
        """Open a runner-local WebSocket tunnel to the desktop session."""
        container_ip = self._service.get_desktop_container_ip(workspace_id)
        upstream_url = f"ws://{container_ip}:6901/websockify"
        upstream_origin = f"http://{container_ip}:6901"
        chosen_protocol = "binary" if "binary" in (subprotocols or []) else None

        session = aiohttp.ClientSession()
        try:
            websocket = await session.ws_connect(
                upstream_url,
                protocols=["binary"] if chosen_protocol else None,
                max_msg_size=16 * 1024 * 1024,
                headers={"Origin": upstream_origin},
            )
        except Exception:
            await session.close()
            raise

        reader_task = asyncio.create_task(
            self._desktop_proxy_reader(tunnel_id, websocket)
        )
        self._desktop_proxy_tunnels[tunnel_id] = DesktopProxyTunnel(
            session=session,
            websocket=websocket,
            reader_task=reader_task,
        )
        return {"ok": True, "subprotocol": chosen_protocol}

    async def _send_desktop_proxy_tunnel_message(
        self,
        tunnel_id: str,
        *,
        text: str | None = None,
        data: str | None = None,
        encoding: str | None = None,
    ) -> None:
        """Forward a browser frame from the backend to the upstream desktop WS."""
        tunnel = self._desktop_proxy_tunnels.get(tunnel_id)
        if tunnel is None:
            raise RuntimeError(f"Unknown desktop proxy tunnel: {tunnel_id}")

        if text is not None:
            await tunnel.websocket.send_str(text)
            return

        if data is None:
            return

        if encoding == "base64":
            await tunnel.websocket.send_bytes(base64.b64decode(data))
            return

        await tunnel.websocket.send_bytes(data.encode("utf-8"))

    async def _close_desktop_proxy_tunnel(self, tunnel_id: str) -> None:
        """Close an active desktop WebSocket tunnel."""
        tunnel = self._desktop_proxy_tunnels.get(tunnel_id)
        if tunnel is None:
            return
        await tunnel.websocket.close()

    async def _finalize_desktop_proxy_tunnel(
        self,
        tunnel_id: str,
        *,
        close_code: int = 1000,
    ) -> None:
        """Release runner-side resources for a desktop proxy tunnel."""
        tunnel = self._desktop_proxy_tunnels.pop(tunnel_id, None)
        if tunnel is None:
            return

        current = asyncio.current_task()
        if tunnel.reader_task is not current:
            tunnel.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await tunnel.reader_task

        if not tunnel.websocket.closed:
            with contextlib.suppress(Exception):
                await tunnel.websocket.close()
        with contextlib.suppress(Exception):
            await tunnel.session.close()

        if self._sio.connected:
            await self._sio.emit(
                "desktop:proxy_ws_closed",
                {
                    "tunnel_id": tunnel_id,
                    "code": close_code,
                },
            )

    # -- heartbeat -------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Periodically send workspace container states to the backend."""
        interval = self._settings.heartbeat_interval
        while True:
            try:
                if not self._sio.connected:
                    await asyncio.sleep(interval)
                    continue

                # Sync cache from runtime to catch externally killed containers
                await self._service.sync_from_runtime()
                workspaces = await self._service.get_workspace_heartbeat_statuses()

                await self._sio.emit(
                    "runner:heartbeat",
                    {"workspaces": workspaces},
                )
                logger.debug(
                    "heartbeat_sent",
                    workspace_count=len(workspaces),
                )
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("heartbeat_failed")
                await asyncio.sleep(interval)

    # -- system metrics loop ---------------------------------------------------

    def _compute_vm_cpu_percent(
        self, workspace_id: str, cpu_time_ns: int, vcpu_count: int
    ) -> float:
        """Compute VM CPU utilisation percentage from cumulative CPU time."""
        now = time.monotonic()
        prev = self._vm_cpu_samples.get(workspace_id)
        self._vm_cpu_samples[workspace_id] = (cpu_time_ns, now, max(vcpu_count, 1))
        if prev is None:
            return 0.0

        prev_cpu_time_ns, prev_sample_ts, prev_vcpu_count = prev
        elapsed_s = now - prev_sample_ts
        if elapsed_s <= 0:
            return 0.0

        cpu_delta_ns = max(cpu_time_ns - prev_cpu_time_ns, 0)
        capacity_ns = elapsed_s * max(prev_vcpu_count, 1) * 1_000_000_000
        if capacity_ns <= 0:
            return 0.0

        cpu_percent = (cpu_delta_ns / capacity_ns) * 100
        return round(max(0.0, min(cpu_percent, 100.0)), 1)

    def _build_vm_metrics_payload(
        self, raw_vm_metrics: dict[str, dict]
    ) -> dict[str, dict[str, float | int]]:
        """Normalise raw VM metrics for transport to backend/webapp."""
        vm_metrics: dict[str, dict[str, float | int]] = {}
        observed_workspace_ids = set(raw_vm_metrics.keys())

        for workspace_id, metric in raw_vm_metrics.items():
            cpu_time_ns = int(metric.get("cpu_time_ns", 0))
            vcpu_count = int(metric.get("vcpu_count", 1))
            vm_metrics[workspace_id] = {
                "cpu_usage_percent": self._compute_vm_cpu_percent(
                    workspace_id, cpu_time_ns, vcpu_count
                ),
                "ram_used_bytes": int(metric.get("ram_used_bytes", 0)),
                "ram_total_bytes": int(metric.get("ram_total_bytes", 0)),
                "disk_used_bytes": int(metric.get("disk_used_bytes", 0)),
                "disk_total_bytes": int(metric.get("disk_total_bytes", 0)),
            }

        # Remove stale CPU samples for VMs that no longer exist.
        for workspace_id in list(self._vm_cpu_samples.keys()):
            if workspace_id not in observed_workspace_ids:
                self._vm_cpu_samples.pop(workspace_id, None)

        return vm_metrics

    def _storage_root_path(self) -> Path:
        """Return the shared storage root for runner-managed QEMU artifacts."""
        storage_dirs = [
            self._settings.qemu_image_cache_dir,
            self._settings.qemu_disk_dir,
            self._settings.qemu_snapshot_dir,
        ]
        expanded = [str(Path(path).expanduser()) for path in storage_dirs if path]
        if not expanded:
            return Path("/")
        return Path(os.path.commonpath(expanded))

    def _resolve_disk_usage_path(self) -> str:
        """Resolve an existing path on the filesystem used for runner storage."""
        probe = self._storage_root_path()
        while not probe.exists():
            if probe.parent == probe:
                return "/"
            probe = probe.parent
        return str(probe)

    async def _metrics_loop(self) -> None:
        """Collect host system metrics every 60 s and emit to backend."""
        # Trigger the first psutil CPU sample so the 60-s average is meaningful
        psutil.cpu_percent(interval=None)

        while True:
            try:
                await asyncio.sleep(60)
                if not self._sio.connected:
                    continue

                cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
                ram = psutil.virtual_memory()
                disk = psutil.disk_usage(self._disk_usage_path)
                raw_vm_metrics = await self._service.get_vm_metrics()
                vm_metrics = self._build_vm_metrics_payload(raw_vm_metrics)

                payload = {
                    "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                    "cpu_usage_percent": cpu,
                    "ram_used_bytes": ram.used,
                    "ram_total_bytes": ram.total,
                    "disk_used_bytes": disk.used,
                    "disk_total_bytes": disk.total,
                    "vm_metrics": vm_metrics,
                }
                await self._sio.emit("runner:system_metrics", payload)
                logger.debug(
                    "system_metrics_sent",
                    cpu=cpu,
                    ram_used=ram.used,
                    disk_used=disk.used,
                    disk_path=self._disk_usage_path,
                    vm_count=len(vm_metrics),
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("metrics_loop_failed")

    # -- handler registration --------------------------------------------------

    def _setup_handlers(self) -> None:
        sio = self._sio

        @sio.event
        async def connect() -> None:
            logger.info("websocket_connected", url=self._settings.backend_url)
            # Sync cache from runtime before registering
            await self._service.sync_from_runtime()
            await self._service.recover_desktop_sessions_from_runtime()
            # Announce this runner to the backend
            await sio.emit(
                "runner:register",
                {
                    "supported_runtimes": self._service.supported_runtimes,
                    "status": "ready",
                },
            )
            # Re-announce live desktop processes so the backend can
            # reconstruct VNC proxy routing. Do not treat this as a
            # viewer acquire — desktop:started is reserved for that.
            for workspace in await self._service.get_workspace_heartbeat_statuses():
                desktop = workspace.get("desktop")
                if not desktop:
                    continue
                await sio.emit(
                    "desktop:process",
                    {
                        "workspace_id": workspace["workspace_id"],
                        "port": desktop["port"],
                        "container_ip": desktop["container_ip"],
                        "network_name": desktop["network_name"],
                        "viewer": bool(desktop.get("viewer")),
                        "computer_use": bool(desktop.get("computer_use")),
                    },
                )
            # Start heartbeat
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            # Start system metrics loop
            if self._metrics_task is None or self._metrics_task.done():
                self._metrics_task = asyncio.create_task(self._metrics_loop())
            # Start SSH health check loop (self-healing)
            if self._health_check_task is None or self._health_check_task.done():
                self._health_check_task = asyncio.create_task(
                    self._service.run_health_check_loop()
                )

        @sio.event
        async def disconnect() -> None:
            logger.warning("websocket_disconnected")
            # Stop heartbeat on disconnect (will be restarted on reconnect)
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
            self._heartbeat_task = None
            # Stop metrics loop on disconnect
            if self._metrics_task and not self._metrics_task.done():
                self._metrics_task.cancel()
            self._metrics_task = None
            if self._desktop_proxy_tunnels:
                for tunnel_id in list(self._desktop_proxy_tunnels.keys()):
                    with contextlib.suppress(Exception):
                        await self._close_desktop_proxy_tunnel(tunnel_id)
            # Keep the health check loop running across reconnects — workspaces
            # can still be unreachable even when the backend connection is down.
            # The loop will restart automatically on the next connect() if needed.

        @sio.event
        async def connect_error(data: object) -> None:
            logger.error("websocket_connect_error", data=data)

        # -- task events -------------------------------------------------------

        @sio.on("task:create_workspace")
        async def on_create_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            # Use workspace_id from backend if provided
            raw_ws_id = data.get("workspace_id")
            workspace_id = uuid.UUID(raw_ws_id) if raw_ws_id else None

            log = logger.bind(task_id=task_id)
            log.info("task_received", task="create_workspace")

            try:
                ws_id, credentials_present = await self._service.create_workspace(
                    repos=data.get("repos", []),
                    qemu_vcpus=data.get("qemu_vcpus"),
                    qemu_memory_mb=data.get("qemu_memory_mb"),
                    qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                    env_vars=data.get("env_vars", {}),
                    files=data.get("files", []),
                    ssh_keys=data.get("ssh_keys", []),
                    workspace_id=workspace_id,
                    runtime_type=data.get("runtime_type", "docker"),
                    image_tag=data.get("image_tag") or None,
                    base_image_path=data.get("base_image_path") or None,
                )
                await sio.emit(
                    "workspace:created",
                    {
                        "task_id": task_id,
                        "workspace_id": str(ws_id),
                        "status": "created",
                        "credentials_present": credentials_present,
                    },
                )
                log.info("task_completed", workspace_id=str(ws_id))
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {
                        "task_id": task_id,
                        "error": str(exc),
                    },
                )
                log.exception("task_failed")

        @sio.on("task:build_image")
        async def on_build_image(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            build_job_id = data.get("build_job_id", "")
            runtime_type = data.get("runtime_type", "docker")
            log = logger.bind(task_id=task_id, build_job_id=build_job_id)
            log.info("task_received", task="build_image", runtime_type=runtime_type)
            try:

                async def _progress(line: str) -> None:
                    await sio.emit(
                        "image:build_progress",
                        {
                            "task_id": task_id,
                            "build_job_id": build_job_id,
                            "line": line,
                        },
                    )

                result = await self._service.build_image(
                    runtime_type=runtime_type,
                    build_job_id=build_job_id,
                    dockerfile_content=data.get("dockerfile_content", ""),
                    image_tag=data.get("image_tag", ""),
                    base_distro=data.get("base_distro", ""),
                    init_script=data.get("init_script", ""),
                    image_path=data.get("image_path", ""),
                    progress_callback=_progress,
                )
                await sio.emit(
                    "image:built",
                    {
                        "task_id": task_id,
                        "build_job_id": build_job_id,
                        "image_tag": result.get("image_tag", ""),
                        "image_path": result.get("image_path", ""),
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "image:build_failed",
                    {
                        "task_id": task_id,
                        "build_job_id": build_job_id,
                        "error": str(exc),
                    },
                )
                log.exception("image_build_failed")

        @sio.on("task:stop_workspace")
        async def on_stop_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            try:
                credentials_present = await self._service.stop_workspace(workspace_id)
                await sio.emit(
                    "workspace:stopped",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "credentials_present": credentials_present,
                    },
                )
                log.info("workspace_stopped")
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("stop_failed")

        @sio.on("task:resume_workspace")
        async def on_resume_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            try:
                credentials_present = await self._service.resume_workspace(
                    workspace_id,
                    qemu_vcpus=data.get("qemu_vcpus"),
                    qemu_memory_mb=data.get("qemu_memory_mb"),
                    qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                    env_vars=data.get("env_vars", {}),
                    files=data.get("files", []),
                    ssh_keys=data.get("ssh_keys", []),
                )
                await sio.emit(
                    "workspace:resumed",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "credentials_present": credentials_present,
                    },
                )
                log.info("workspace_resumed")
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("resume_failed")

        @sio.on("task:update_workspace")
        async def on_update_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(
                task_id=task_id,
                workspace_id=str(workspace_id),
            )
            try:
                await self._service.update_workspace_resources(
                    workspace_id,
                    qemu_vcpus=int(data["qemu_vcpus"]),
                    qemu_memory_mb=int(data["qemu_memory_mb"]),
                    qemu_disk_size_gb=int(data["qemu_disk_size_gb"]),
                )
                await sio.emit(
                    "workspace:updated",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                    },
                )
                log.info("workspace_updated")
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("update_workspace_failed")

        @sio.on("task:inject_credentials")
        async def on_inject_credentials(data: dict) -> dict:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            try:
                credentials_present = await self._service.inject_credentials(
                    workspace_id,
                    env_vars=data.get("env_vars", {}),
                    files=data.get("files", []),
                    ssh_keys=data.get("ssh_keys", []),
                )
                await sio.emit(
                    "workspace:credentials_injected",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "credentials_present": credentials_present,
                    },
                )
                log.info(
                    "workspace_credentials_injected",
                    credentials_present=credentials_present,
                )
                return {
                    "ok": True,
                    "credentials_present": credentials_present,
                }
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("inject_credentials_failed")
                return {"ok": False, "error": str(exc)}

        @sio.on("task:remove_workspace")
        async def on_remove_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            try:
                # Check if workspace exists in cache before removal
                already_absent = workspace_id not in self._service._cache
                await self._service.remove_workspace(workspace_id)
                await sio.emit(
                    "workspace:removed",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "result": "already_absent" if already_absent else "deleted",
                        "already_absent": already_absent,
                    },
                )
                log.info("workspace_removed", already_absent=already_absent)
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("remove_failed")

        @sio.on("task:cleanup_unknown_workspace")
        async def on_cleanup_unknown_workspace(data: dict) -> None:
            raw_workspace_id = data.get("workspace_id", "")
            log = logger.bind(workspace_id=raw_workspace_id)
            try:
                workspace_id = uuid.UUID(raw_workspace_id)
                cleaned = await self._service.cleanup_unknown_workspace(workspace_id)
                await sio.emit(
                    "workspace:cleanup_unknown_done",
                    {
                        "workspace_id": str(workspace_id),
                        "cleaned": cleaned,
                    },
                )
                log.info("unknown_workspace_cleanup_done", cleaned=cleaned)
            except Exception as exc:
                await sio.emit(
                    "workspace:cleanup_unknown_failed",
                    {
                        "workspace_id": raw_workspace_id,
                        "error": str(exc),
                    },
                )
                log.exception("unknown_workspace_cleanup_failed")

        # -- terminal events ---------------------------------------------------

        @sio.on("task:start_terminal")
        async def on_start_terminal(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            cols = data.get("cols", 80)
            rows = data.get("rows", 24)
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            log.info("task_received", task="start_terminal")

            try:
                terminal_id = await self._service.start_terminal(
                    workspace_id,
                    cols=cols,
                    rows=rows,
                )
                await sio.emit(
                    "terminal:started",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "terminal_id": terminal_id,
                    },
                )

                # Start background reader task
                async def _read_pty() -> None:
                    try:
                        async for chunk in self._service.read_terminal(terminal_id):
                            await sio.emit(
                                "terminal:output",
                                {
                                    "workspace_id": str(workspace_id),
                                    "terminal_id": terminal_id,
                                    "data": base64.b64encode(chunk).decode("ascii"),
                                },
                            )
                    except Exception:
                        log.exception("pty_read_failed")
                    finally:
                        try:
                            await self._service.close_terminal(terminal_id)
                        except Exception:
                            log.exception("terminal_cleanup_failed")
                        await sio.emit(
                            "terminal:closed",
                            {
                                "workspace_id": str(workspace_id),
                                "terminal_id": terminal_id,
                            },
                        )
                        self._running_tasks.pop(f"terminal:{terminal_id}", None)

                reader = asyncio.create_task(_read_pty())
                self._running_tasks[f"terminal:{terminal_id}"] = reader

                log.info("terminal_started", terminal_id=terminal_id)
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("start_terminal_failed")

        @sio.on("terminal:input")
        async def on_terminal_input(data: dict) -> None:
            terminal_id = data["terminal_id"]
            raw = base64.b64decode(data["data"])
            try:
                await self._service.write_terminal(terminal_id, raw)
            except Exception:
                logger.exception("terminal_write_failed", terminal_id=terminal_id)

        @sio.on("terminal:resize")
        async def on_terminal_resize(data: dict) -> None:
            terminal_id = data["terminal_id"]
            cols = data.get("cols", 80)
            rows = data.get("rows", 24)
            try:
                await self._service.resize_terminal(terminal_id, cols, rows)
            except Exception:
                logger.exception("terminal_resize_failed", terminal_id=terminal_id)

        @sio.on("terminal:close")
        async def on_terminal_close(data: dict) -> None:
            terminal_id = data["terminal_id"]
            workspace_id = data.get("workspace_id", "")
            try:
                # Cancel the reader task
                reader = self._running_tasks.pop(f"terminal:{terminal_id}", None)
                if reader:
                    reader.cancel()
                await self._service.close_terminal(terminal_id)
                await sio.emit(
                    "terminal:closed",
                    {
                        "workspace_id": workspace_id,
                        "terminal_id": terminal_id,
                    },
                )
            except Exception:
                logger.exception("terminal_close_failed", terminal_id=terminal_id)

        # -- desktop session events --------------------------------------------

        @sio.on("task:start_desktop")
        async def on_start_desktop(data: dict) -> None:
            task_id = data["task_id"]
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            log.info("task_received", task="start_desktop")

            try:
                session = await self._service.start_desktop(
                    workspace_id,
                    width=data.get("desktop_width"),
                    height=data.get("desktop_height"),
                )

                # Get container IP for backend proxy
                container_ip = self._service.get_desktop_container_ip(workspace_id)
                network_name = self._service.get_desktop_network_name(workspace_id)

                await sio.emit(
                    "desktop:started",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "port": session.port,
                        "container_ip": container_ip,
                        "network_name": network_name,
                        "viewer": session.viewer_held,
                        "computer_use": bool(session.computeruse_run_ids),
                    },
                )
                log.info(
                    "desktop_started", port=session.port, container_ip=container_ip
                )
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("start_desktop_failed")

        @sio.on("task:stop_desktop")
        async def on_stop_desktop(data: dict) -> None:
            task_id = data["task_id"]
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            log.info("task_received", task="stop_desktop")

            try:
                result = await self._service.stop_desktop(workspace_id)
                if result.stopped or not result.process_alive:
                    await sio.emit(
                        "desktop:stopped",
                        {
                            "task_id": task_id,
                            "workspace_id": str(workspace_id),
                        },
                    )
                    log.info("desktop_stopped")
                else:
                    await sio.emit(
                        "desktop:viewer_released",
                        {
                            "task_id": task_id,
                            "workspace_id": str(workspace_id),
                            "computer_use_active": result.computer_use_active,
                        },
                    )
                    log.info(
                        "desktop_viewer_released",
                        computer_use_active=result.computer_use_active,
                    )
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("stop_desktop_failed")

        @sio.on("desktop:clipboard_write")
        async def on_desktop_clipboard_write(data: dict) -> dict:
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(workspace_id=str(workspace_id))
            try:
                await self._service.write_desktop_clipboard(
                    workspace_id,
                    data.get("text", ""),
                )
                return {"ok": True}
            except Exception as exc:
                log.exception("desktop_clipboard_write_failed")
                return {"ok": False, "error": str(exc)}

        @sio.on("desktop:clipboard_read")
        async def on_desktop_clipboard_read(data: dict) -> dict:
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(workspace_id=str(workspace_id))
            try:
                text = await self._service.read_desktop_clipboard(workspace_id)
                return {"ok": True, "text": text}
            except Exception as exc:
                log.exception("desktop_clipboard_read_failed")
                return {"ok": False, "error": str(exc)}

        @sio.on("desktop:proxy_http_request")
        async def on_desktop_proxy_http_request(data: dict) -> dict:
            workspace_id = uuid.UUID(data["workspace_id"])
            rest_path = data.get("path", "/")
            query_string = data.get("query_string", "")
            return await self._fetch_desktop_http(
                workspace_id,
                rest_path,
                query_string,
            )

        @sio.on("desktop:proxy_ws_open")
        async def on_desktop_proxy_ws_open(data: dict) -> dict:
            workspace_id = uuid.UUID(data["workspace_id"])
            tunnel_id = data["tunnel_id"]
            return await self._open_desktop_proxy_tunnel(
                workspace_id,
                tunnel_id,
                list(data.get("subprotocols") or []),
            )

        @sio.on("desktop:proxy_ws_send")
        async def on_desktop_proxy_ws_send(data: dict) -> None:
            await self._send_desktop_proxy_tunnel_message(
                data["tunnel_id"],
                text=data.get("text"),
                data=data.get("data"),
                encoding=data.get("encoding"),
            )

        @sio.on("desktop:proxy_ws_close")
        async def on_desktop_proxy_ws_close(data: dict) -> None:
            await self._close_desktop_proxy_tunnel(data["tunnel_id"])

        # -- harness workspace-access RPC ----------------------------------------

        async def _harness_result(event: str, data: dict) -> None:
            await sio.emit(event, data)

        @sio.on("harness:exec_stream")
        async def on_harness_exec_stream(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            timeout = data.get("timeout")
            log = logger.bind(workspace_id=str(workspace_id), request_id=request_id)
            log.info("harness_received", task="exec_stream")
            task_key = f"harness:{request_id}"

            async def _run() -> None:
                try:
                    timeout_s = float(timeout) if timeout is not None else None
                    coro = self._service.exec_harness_command_stream(
                        workspace_id,
                        data.get("command", []),
                        workdir=data.get("workdir", "/workspace"),
                        env=data.get("env", {}),
                    )
                    if timeout_s is not None:
                        async for stream, text in _stream_with_timeout(coro, timeout_s):
                            if stream == "exit":
                                await _harness_result(
                                    "harness:exec_done",
                                    {
                                        "workspace_id": str(workspace_id),
                                        "request_id": request_id,
                                        "exit_code": int(text),
                                    },
                                )
                            else:
                                await _harness_result(
                                    "harness:exec_chunk",
                                    {
                                        "workspace_id": str(workspace_id),
                                        "request_id": request_id,
                                        "stream": stream,
                                        "data": text,
                                    },
                                )
                    else:
                        async for stream, text in coro:
                            if stream == "exit":
                                await _harness_result(
                                    "harness:exec_done",
                                    {
                                        "workspace_id": str(workspace_id),
                                        "request_id": request_id,
                                        "exit_code": int(text),
                                    },
                                )
                            else:
                                await _harness_result(
                                    "harness:exec_chunk",
                                    {
                                        "workspace_id": str(workspace_id),
                                        "request_id": request_id,
                                        "stream": stream,
                                        "data": text,
                                    },
                                )
                except asyncio.TimeoutError:
                    await _harness_result(
                        "harness:exec_done",
                        {
                            "workspace_id": str(workspace_id),
                            "request_id": request_id,
                            "error": "Execution timed out",
                        },
                    )
                    log.warning("harness_exec_timeout")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await _harness_result(
                        "harness:exec_done",
                        {
                            "workspace_id": str(workspace_id),
                            "request_id": request_id,
                            "error": str(exc),
                        },
                    )
                    log.exception("harness_exec_stream_failed")
                finally:
                    self._running_tasks.pop(task_key, None)

            task = asyncio.create_task(_run())
            self._running_tasks[task_key] = task

        @sio.on("harness:exec_wait")
        async def on_harness_exec_wait(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            timeout = data.get("timeout")
            log = logger.bind(workspace_id=str(workspace_id), request_id=request_id)
            log.info("harness_received", task="exec_wait")
            try:
                timeout_s = float(timeout) if timeout is not None else None
                coro = self._service.exec_harness_command(
                    workspace_id,
                    data.get("command", []),
                    workdir=data.get("workdir", "/workspace"),
                    env=data.get("env", {}),
                )
                if timeout_s is not None:
                    exit_code, stdout, stderr = await asyncio.wait_for(coro, timeout_s)
                else:
                    exit_code, stdout, stderr = await coro
                await _harness_result(
                    "harness:exec_wait_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "exit_code": exit_code,
                        "stdout": stdout,
                        "stderr": stderr,
                    },
                )
            except asyncio.TimeoutError:
                await _harness_result(
                    "harness:exec_wait_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "error": "Execution timed out",
                    },
                )
                log.warning("harness_exec_timeout")
            except Exception as exc:
                await _harness_result(
                    "harness:exec_wait_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "error": str(exc),
                    },
                )
                log.exception("harness_exec_wait_failed")

        @sio.on("harness:read_file")
        async def on_harness_read_file(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data.get("path", "/workspace")
            try:
                result = await self._service.read_file(
                    workspace_id, path, max_size=data.get("max_size")
                )
                await _harness_result(
                    "harness:read_file_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        **result,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:read_file_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "error": str(exc),
                    },
                )
                logger.exception("harness_read_file_failed")

        @sio.on("harness:write_file")
        async def on_harness_write_file(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data.get("path", "/workspace")
            try:
                await self._service.write_file_content(
                    workspace_id,
                    path,
                    data.get("content", ""),
                    mode=int(data.get("mode", 0o644)),
                )
                await _harness_result(
                    "harness:write_file_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "ok": True,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:write_file_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "error": str(exc),
                    },
                )
                logger.exception("harness_write_file_failed")

        @sio.on("harness:list")
        async def on_harness_list(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data.get("path", "/workspace")
            try:
                raw_entries = await self._service.list_files(workspace_id, path)
                entries = [
                    {
                        "name": entry.get("name", ""),
                        "path": entry.get("path", ""),
                        "is_dir": entry.get("type") == "directory",
                        "size": entry.get("size", 0),
                    }
                    for entry in raw_entries
                ]
                await _harness_result(
                    "harness:list_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "entries": entries,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:list_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "entries": [],
                        "error": str(exc),
                    },
                )
                logger.exception("harness_list_failed")

        @sio.on("harness:stat")
        async def on_harness_stat(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data.get("path", "/workspace")
            try:
                result = await self._service.stat_path(workspace_id, path)
                await _harness_result(
                    "harness:stat_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        **result,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:stat_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "error": str(exc),
                    },
                )
                logger.exception("harness_stat_failed")

        @sio.on("harness:desktop_action")
        async def on_harness_desktop_action(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            action = data.get("action", "")
            args = data.get("args") or {}
            try:
                result = await self._service.desktop_action(workspace_id, action, args)
                await _harness_result(
                    "harness:desktop_action_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        **result,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:desktop_action_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "error": str(exc),
                    },
                )
                logger.exception("harness_desktop_action_failed")

        @sio.on("harness:process_start")
        async def on_harness_process_start(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            process_id = str(data.get("process_id", ""))
            log = logger.bind(
                workspace_id=str(workspace_id), request_id=request_id
            )
            log.info("harness_received", task="process_start")
            try:
                result = await self._service.start_background_process(
                    workspace_id,
                    process_id,
                    str(data.get("command", "")),
                    workdir=data.get("workdir", "/workspace"),
                    env=data.get("env"),
                    name=str(data.get("name", "") or ""),
                )
                await _harness_result(
                    "harness:process_start_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "process_id": result["process_id"],
                        "pid": result["pid"],
                        "log_path": result["log_path"],
                        "exit_path": result["exit_path"],
                        "status": "running",
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:process_start_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "process_id": process_id,
                        "error": str(exc),
                    },
                )
                log.exception("harness_process_start_failed")

        @sio.on("harness:process_list")
        async def on_harness_process_list(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            try:
                processes = await self._service.list_background_processes(
                    workspace_id
                )
                await _harness_result(
                    "harness:process_list_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "processes": processes,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:process_list_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "processes": [],
                        "error": str(exc),
                    },
                )
                logger.exception("harness_process_list_failed")

        @sio.on("harness:process_get")
        async def on_harness_process_get(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            process_id = str(data.get("process_id", ""))
            try:
                process = await self._service.get_background_status(
                    workspace_id, process_id
                )
                await _harness_result(
                    "harness:process_get_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "process": process,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:process_get_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "process_id": process_id,
                        "error": str(exc),
                    },
                )
                logger.exception("harness_process_get_failed")

        @sio.on("harness:process_stop")
        async def on_harness_process_stop(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            process_id = str(data.get("process_id", ""))
            force = bool(data.get("force", False))
            try:
                result = await self._service.stop_background_process(
                    workspace_id, process_id, force=force
                )
                await _harness_result(
                    "harness:process_stop_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        **result,
                    },
                )
            except Exception as exc:
                await _harness_result(
                    "harness:process_stop_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "process_id": process_id,
                        "stopped": False,
                        "error": str(exc),
                    },
                )
                logger.exception("harness_process_stop_failed")

        @sio.on("harness:cancel")
        async def on_harness_cancel(data: dict) -> None:
            request_id = data.get("request_id", "")
            task = self._running_tasks.pop(f"harness:{request_id}", None)
            if task is not None and not task.done():
                task.cancel()
                logger.info("harness_cancelled", request_id=request_id)

        # -- file explorer events ----------------------------------------------

        @sio.on("files:list")
        async def on_files_list(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data.get("path", "/workspace")
            try:
                entries = await self._service.list_files(workspace_id, path)
                await sio.emit(
                    "files:list_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "entries": entries,
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "files:list_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "entries": [],
                        "error": str(exc),
                    },
                )
                logger.exception("files_list_failed")

        @sio.on("files:find")
        async def on_files_find(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            query = data.get("query", "")
            limit = data.get("limit", 50)
            try:
                result = await self._service.find_files(
                    workspace_id, query=query, limit=limit
                )
                await sio.emit(
                    "files:find_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "query": query,
                        "paths": result.get("paths", []),
                        "truncated": result.get("truncated", False),
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "files:find_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "query": query,
                        "paths": [],
                        "truncated": False,
                        "error": str(exc),
                    },
                )
                logger.exception("files_find_failed")

        @sio.on("files:read")
        async def on_files_read(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data["path"]
            max_size = data.get("max_size")
            try:
                result = await self._service.read_file(
                    workspace_id,
                    path,
                    max_size=max_size,
                )
                await sio.emit(
                    "files:content_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        **result,
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "files:content_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "content": "",
                        "size": 0,
                        "truncated": False,
                        "error": str(exc),
                    },
                )
                logger.exception("files_read_failed")

        @sio.on("files:upload")
        async def on_files_upload(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data["path"]
            try:
                await self._service.upload_file(
                    workspace_id,
                    path=path,
                    filename=data["filename"],
                    content_b64=data["content"],
                    is_directory=data.get("is_directory", False),
                )
                await sio.emit(
                    "files:upload_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "status": "success",
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "files:upload_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "status": "error",
                        "error": str(exc),
                    },
                )
                logger.exception("files_upload_failed")

        @sio.on("files:download")
        async def on_files_download(data: dict) -> None:
            workspace_id = uuid.UUID(data["workspace_id"])
            request_id = data.get("request_id", "")
            path = data["path"]
            try:
                result = await self._service.download_file(workspace_id, path)
                await sio.emit(
                    "files:download_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        **result,
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "files:download_result",
                    {
                        "workspace_id": str(workspace_id),
                        "request_id": request_id,
                        "path": path,
                        "content": "",
                        "filename": "",
                        "is_archive": False,
                        "error": str(exc),
                    },
                )
                logger.exception("files_download_failed")

        # -- image artifact events ---------------------------------------------

        @sio.on("task:create_image_artifact")
        async def on_create_image_artifact(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            name = data["name"]
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            log.info("task_received", task="create_image_artifact")
            try:
                artifact = await self._service.create_image_artifact(workspace_id, name)
                await sio.emit(
                    "image_artifact:created",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "image_artifact_id": artifact.artifact_id,
                        "name": artifact.name,
                        "created_at": artifact.created_at.isoformat()
                        if isinstance(artifact.created_at, datetime)
                        else str(artifact.created_at),
                        "size_bytes": artifact.size_bytes,
                    },
                )
                log.info(
                    "image_artifact_created", image_artifact_id=artifact.artifact_id
                )
            except Exception as exc:
                await sio.emit(
                    "image_artifact:failed",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "error": str(exc),
                    },
                )
                log.exception("create_image_artifact_failed")

        @sio.on("task:list_image_artifacts")
        async def on_list_image_artifacts(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(task_id=task_id, workspace_id=str(workspace_id))
            try:
                artifacts = await self._service.list_image_artifacts(workspace_id)
                await sio.emit(
                    "image_artifact:list",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "image_artifacts": [
                            {
                                "image_artifact_id": artifact.artifact_id,
                                "name": artifact.name,
                                "created_at": artifact.created_at,
                                "size_bytes": artifact.size_bytes,
                            }
                            for artifact in artifacts
                        ],
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("list_image_artifacts_failed")

        @sio.on("task:delete_image_artifact")
        async def on_delete_image_artifact(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            image_artifact_id = data["image_artifact_id"]
            image_instance_id = data.get("image_instance_id", "")
            runtime_type = data.get("runtime_type", "")
            raw_workspace_id = data.get("workspace_id")
            workspace_id = uuid.UUID(raw_workspace_id) if raw_workspace_id else None
            log = logger.bind(task_id=task_id, image_artifact_id=image_artifact_id)
            try:
                result = "deleted"
                if runtime_type:
                    result = await self._service.delete_image_reference(
                        runtime_type=runtime_type,
                        image_ref=image_artifact_id,
                    )
                elif workspace_id is not None:
                    await self._service.delete_image_artifact(
                        workspace_id, image_artifact_id
                    )
                else:
                    raise RuntimeError(
                        "Either runtime_type or workspace_id is required for image deletion"
                    )
                await sio.emit(
                    "image_artifact:deleted",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id) if workspace_id else "",
                        "image_instance_id": image_instance_id,
                        "image_artifact_id": image_artifact_id,
                        "result": result,
                        "already_absent": result == "already_absent",
                    },
                )
                log.info(
                    "image_artifact_deleted",
                    image_artifact_id=image_artifact_id,
                    result=result,
                )
            except Exception as exc:
                await sio.emit(
                    "image_artifact:delete_failed",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("delete_image_artifact_failed")

        @sio.on("task:create_workspace_from_image_artifact")
        async def on_create_workspace_from_image_artifact(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            image_artifact_id = data["image_artifact_id"]
            raw_ws_id = data.get("workspace_id")
            new_workspace_id = uuid.UUID(raw_ws_id) if raw_ws_id else uuid.uuid4()
            runtime_type = data.get("runtime_type", "qemu")
            log = logger.bind(task_id=task_id, image_artifact_id=image_artifact_id)
            log.info("task_received", task="create_workspace_from_image_artifact")
            try:
                (
                    ws_id,
                    credentials_present,
                ) = await self._service.create_workspace_from_image_artifact(
                    image_artifact_id=image_artifact_id,
                    new_workspace_id=new_workspace_id,
                    runtime_type=runtime_type,
                    qemu_vcpus=data.get("qemu_vcpus"),
                    qemu_memory_mb=data.get("qemu_memory_mb"),
                    qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                    env_vars=data.get("env_vars", {}),
                    files=data.get("files", []),
                    ssh_keys=data.get("ssh_keys", []),
                )
                await sio.emit(
                    "workspace:created",
                    {
                        "task_id": task_id,
                        "workspace_id": str(ws_id),
                        "status": "created",
                        "credentials_present": credentials_present,
                    },
                )
                log.info(
                    "workspace_created_from_image_artifact",
                    workspace_id=str(ws_id),
                )
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {
                        "task_id": task_id,
                        "workspace_id": str(new_workspace_id),
                        "error": str(exc),
                    },
                )
                log.exception("clone_failed")

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Connect to the backend and block until disconnected."""
        headers = {"Authorization": f"Bearer {self._settings.api_token}"}

        logger.info(
            "websocket_connecting",
            url=self._settings.backend_url,
        )

        await self._sio.connect(
            self._settings.backend_url,
            headers=headers,
            transports=["websocket"],
            socketio_path=self._settings.socketio_path,
        )

        # Block until the connection is closed (reconnects are automatic)
        await self._sio.wait()

    async def stop(self) -> None:
        """Cancel running tasks, stop heartbeat and metrics loop, and disconnect."""
        # Cancel heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        # Cancel metrics loop
        if self._metrics_task and not self._metrics_task.done():
            self._metrics_task.cancel()
        # Cancel health check loop
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()

        for task_id, task in self._running_tasks.items():
            task.cancel()
            logger.info("task_cancelled", task_id=task_id)
        self._running_tasks.clear()

        if self._sio.connected:
            await self._sio.disconnect()
