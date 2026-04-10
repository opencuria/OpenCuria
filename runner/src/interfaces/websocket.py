"""WebSocket interface using python-socketio (async client).

Connects to the Django backend, authenticates with a Bearer token,
and listens for task events.  Agent output is streamed back to the
backend in real time via ``output:chunk`` events.

Includes a periodic heartbeat that reports workspace container states
so the backend can reconcile its records with actual runtime state.

A separate metrics loop collects host CPU, RAM, and disk usage every
60 seconds and sends them to the backend as ``runner:system_metrics``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import shlex
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psutil
import socketio
import structlog

from ..config import RunnerSettings
from ..runtime.base import CommandExecutionError
from ..service import WorkspaceService
from .base import Interface

logger = structlog.get_logger(__name__)


class WebSocketInterface(Interface):
    """python-socketio async client that bridges backend ↔ service."""

    def __init__(
        self, service: WorkspaceService, settings: RunnerSettings
    ) -> None:
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
        self._cancelling_task_ids: set[str] = set()
        self._heartbeat_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._metrics_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._health_check_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._vm_cpu_samples: dict[str, tuple[int, float, int]] = {}
        self._disk_usage_path = self._resolve_disk_usage_path()
        self._setup_handlers()

    # -- heartbeat -------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """Periodically send workspace container states to the backend."""
        interval = self._settings.heartbeat_interval
        while True:
            try:
                await asyncio.sleep(interval)
                if not self._sio.connected:
                    continue

                # Sync cache from runtime to catch externally killed containers
                await self._service.sync_from_runtime()
                workspaces = self._service.get_workspace_statuses()

                await self._sio.emit(
                    "runner:heartbeat",
                    {"workspaces": workspaces},
                )
                logger.debug(
                    "heartbeat_sent",
                    workspace_count=len(workspaces),
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("heartbeat_failed")

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

        def _prompt_pidfile(task_id: str) -> str:
            return f"/tmp/opencuria-prompt-{task_id}.pid"

        def _build_prompt_wrapped_args(
            command_args: list[str] | str,
            task_id: str,
        ) -> list[str]:
            pid_file = _prompt_pidfile(task_id)
            normalised_args = self._service._normalise_command_args(command_args)
            wrapped_entrypoint = (
                f"printf '%s' \"$$\" > {shlex.quote(pid_file)}; exec \"$@\""
            )
            return [
                "sh",
                "-lc",
                wrapped_entrypoint,
                "opencuria-prompt",
                *normalised_args,
            ]

        def _wrap_prompt_command(command: dict, task_id: str) -> dict:
            return {
                **command,
                "args": _build_prompt_wrapped_args(command["args"], task_id),
            }

        @sio.event
        async def connect() -> None:
            logger.info("websocket_connected", url=self._settings.backend_url)
            # Sync cache from runtime before registering
            await self._service.sync_from_runtime()
            # Announce this runner to the backend
            await sio.emit(
                "runner:register",
                {
                    "supported_runtimes": self._service.supported_runtimes,
                    "status": "ready",
                },
            )
            # Start heartbeat
            if self._heartbeat_task is None or self._heartbeat_task.done():
                self._heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop()
                )
            # Start system metrics loop
            if self._metrics_task is None or self._metrics_task.done():
                self._metrics_task = asyncio.create_task(
                    self._metrics_loop()
                )
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
            # Stop metrics loop on disconnect
            if self._metrics_task and not self._metrics_task.done():
                self._metrics_task.cancel()
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
                ws_id = await self._service.create_workspace(
                    repos=data.get("repos", []),
                    qemu_vcpus=data.get("qemu_vcpus"),
                    qemu_memory_mb=data.get("qemu_memory_mb"),
                    qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                    configure_commands=data.get("configure_commands", []),
                    env_vars=data.get("env_vars", {}),
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
            runner_image_build_id = data.get("runner_image_build_id", "")
            runtime_type = data.get("runtime_type", "docker")
            log = logger.bind(task_id=task_id, runner_image_build_id=runner_image_build_id)
            log.info("task_received", task="build_image", runtime_type=runtime_type)
            try:
                async def _progress(line: str) -> None:
                    await sio.emit(
                        "image:build_progress",
                        {
                            "task_id": task_id,
                            "runner_image_build_id": runner_image_build_id,
                            "line": line,
                        },
                    )

                result = await self._service.build_image(
                    runtime_type=runtime_type,
                    runner_image_build_id=runner_image_build_id,
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
                        "runner_image_build_id": runner_image_build_id,
                        "image_tag": result.get("image_tag", ""),
                        "image_path": result.get("image_path", ""),
                    },
                )
            except Exception as exc:
                await sio.emit(
                    "image:build_failed",
                    {
                        "task_id": task_id,
                        "runner_image_build_id": runner_image_build_id,
                        "error": str(exc),
                    },
                )
                log.exception("image_build_failed")

        @sio.on("task:run_prompt")
        async def on_run_prompt(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            command = _wrap_prompt_command(data["command"], task_id)
            configure_commands = data.get("configure_commands", [])
            fallback_configure_commands = data.get("fallback_configure_commands", [])

            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
            log.info("task_received", task="run_prompt")

            # Run as a background asyncio task so we can handle
            # multiple prompts concurrently.
            async def _stream() -> None:
                prepared = None
                try:
                    prepared = await self._service.prepare_operation(
                        workspace_id,
                        env_vars=data.get("env_vars", {}),
                        ssh_keys=data.get("ssh_keys", []),
                    )

                    async def _emit_status(status: str, detail: str) -> None:
                        await sio.emit(
                            "output:status",
                            {
                                "task_id": task_id,
                                "workspace_id": str(workspace_id),
                                "status": status,
                                "detail": detail,
                            },
                        )

                    async def _emit_output(text: str) -> None:
                        if not text:
                            return
                        await sio.emit(
                            "output:chunk",
                            {
                                "task_id": task_id,
                                "workspace_id": str(workspace_id),
                                "line": text,
                            },
                        )

                    async def _run_and_stream(command_payload: dict) -> str:
                        chunks: list[str] = []
                        async for line in self._service.run_command(
                            workspace_id,
                            command_payload,
                            prepared=prepared,
                        ):
                            chunks.append(line)
                            await _emit_output(line)
                        return "\n".join(chunks)

                    # Run configure commands first if this is the first time this
                    # agent is used in the workspace (sent by backend when needed).
                    if configure_commands:
                        log.info(
                            "running_configure_commands",
                            count=len(configure_commands),
                        )
                        await _emit_status(
                            "executing_configuration_commands",
                            "Executing Configuration Commands…",
                        )
                        await self._service.run_configure_commands(
                            workspace_id,
                            configure_commands,
                            prepared=prepared,
                        )

                    await _emit_status(
                        "executing_agent_command",
                        "Executing Agent Command…",
                    )
                    try:
                        await _run_and_stream(command)
                    except CommandExecutionError as exc:
                        if not fallback_configure_commands or exc.exit_code != 127:
                            if (
                                task_id in self._cancelling_task_ids
                                and exc.exit_code in {137, 143}
                            ):
                                raise asyncio.CancelledError from exc
                            raise
                        log.info(
                            "retry_after_missing_command_exit_code",
                            configure_count=len(fallback_configure_commands),
                            exit_code=exc.exit_code,
                        )
                        await _emit_status(
                            "retrying_configuration_commands",
                            "Command missing in workspace. Installing agent…",
                        )
                        await self._service.run_configure_commands(
                            workspace_id,
                            fallback_configure_commands,
                            prepared=prepared,
                        )
                        await _emit_status(
                            "retrying_agent_command",
                            "Retrying Agent Command…",
                        )
                        await _run_and_stream(command)

                    await sio.emit(
                        "output:complete",
                        {
                            "task_id": task_id,
                            "workspace_id": str(workspace_id),
                        },
                    )
                except asyncio.CancelledError:
                    await sio.emit(
                        "output:error",
                        {
                            "task_id": task_id,
                            "workspace_id": str(workspace_id),
                            "error": "Prompt execution cancelled by user",
                        },
                    )
                except Exception as exc:
                    await sio.emit(
                        "output:error",
                        {
                            "task_id": task_id,
                            "workspace_id": str(workspace_id),
                            "error": str(exc),
                        },
                    )
                    log.exception("prompt_stream_failed")
                finally:
                    try:
                        await self._service.cleanup_prompt_process_tracking(
                            workspace_id,
                            _prompt_pidfile(task_id),
                        )
                    except Exception:
                        log.exception("prompt_pidfile_cleanup_failed")
                    if prepared is not None:
                        try:
                            await self._service.cleanup_operation(prepared)
                        except Exception:
                            log.exception("operation_context_cleanup_failed")
                    self._cancelling_task_ids.discard(task_id)
                    self._running_tasks.pop(task_id, None)

            task = asyncio.create_task(_stream())
            self._running_tasks[task_id] = task

        @sio.on("task:cancel_prompt")
        async def on_cancel_prompt(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            target_task_id = data.get("target_task_id", "")
            log = logger.bind(
                task_id=task_id,
                workspace_id=str(workspace_id),
                target_task_id=target_task_id,
            )
            try:
                if not target_task_id:
                    raise ValueError("Missing target_task_id")

                self._cancelling_task_ids.add(target_task_id)
                await self._service.terminate_prompt_process(
                    workspace_id,
                    _prompt_pidfile(target_task_id),
                )

                target_task = self._running_tasks.get(target_task_id)
                if target_task and not target_task.done():
                    target_task.cancel()

                await sio.emit(
                    "prompt:cancelled",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "target_task_id": target_task_id,
                    },
                )
                log.info("prompt_cancelled")
            except Exception as exc:
                self._cancelling_task_ids.discard(target_task_id)
                await sio.emit(
                    "workspace:error",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                        "error": str(exc),
                    },
                )
                log.exception("cancel_prompt_failed")

        @sio.on("task:stop_workspace")
        async def on_stop_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
            try:
                await self._service.stop_workspace(workspace_id)
                await sio.emit(
                    "workspace:stopped",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
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
            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
            try:
                await self._service.resume_workspace(
                    workspace_id,
                    qemu_vcpus=data.get("qemu_vcpus"),
                    qemu_memory_mb=data.get("qemu_memory_mb"),
                    qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                )
                await sio.emit(
                    "workspace:resumed",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
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

        @sio.on("task:remove_workspace")
        async def on_remove_workspace(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            workspace_id = uuid.UUID(data["workspace_id"])
            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
            try:
                await self._service.remove_workspace(workspace_id)
                await sio.emit(
                    "workspace:removed",
                    {
                        "task_id": task_id,
                        "workspace_id": str(workspace_id),
                    },
                )
                log.info("workspace_removed")
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
                cleaned = await self._service.cleanup_unknown_workspace(
                    workspace_id
                )
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
            configure_commands = data.get("configure_commands", [])
            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
            log.info("task_received", task="start_terminal")

            prepared = None
            try:
                prepared = await self._service.prepare_operation(
                    workspace_id,
                    env_vars=data.get("env_vars", {}),
                    ssh_keys=data.get("ssh_keys", []),
                )
                if configure_commands:
                    await self._service.run_configure_commands(
                        workspace_id,
                        configure_commands,
                        prepared=prepared,
                    )
                terminal_id = await self._service.start_terminal(
                    workspace_id,
                    cols=cols,
                    rows=rows,
                    prepared=prepared,
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
                        async for chunk in self._service.read_terminal(
                            terminal_id
                        ):
                            await sio.emit(
                                "terminal:output",
                                {
                                    "workspace_id": str(workspace_id),
                                    "terminal_id": terminal_id,
                                    "data": base64.b64encode(chunk).decode(
                                        "ascii"
                                    ),
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
                        self._running_tasks.pop(
                            f"terminal:{terminal_id}", None
                        )

                reader = asyncio.create_task(_read_pty())
                self._running_tasks[f"terminal:{terminal_id}"] = reader

                log.info("terminal_started", terminal_id=terminal_id)
            except Exception as exc:
                if prepared is not None:
                    try:
                        await self._service.cleanup_operation(prepared)
                    except Exception:
                        log.exception("terminal_operation_context_cleanup_failed")
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
                logger.exception(
                    "terminal_write_failed", terminal_id=terminal_id
                )

        @sio.on("terminal:resize")
        async def on_terminal_resize(data: dict) -> None:
            terminal_id = data["terminal_id"]
            cols = data.get("cols", 80)
            rows = data.get("rows", 24)
            try:
                await self._service.resize_terminal(terminal_id, cols, rows)
            except Exception:
                logger.exception(
                    "terminal_resize_failed", terminal_id=terminal_id
                )

        @sio.on("terminal:close")
        async def on_terminal_close(data: dict) -> None:
            terminal_id = data["terminal_id"]
            workspace_id = data.get("workspace_id", "")
            try:
                # Cancel the reader task
                reader = self._running_tasks.pop(
                    f"terminal:{terminal_id}", None
                )
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
                logger.exception(
                    "terminal_close_failed", terminal_id=terminal_id
                )

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
            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
            log.info("task_received", task="create_image_artifact")
            try:
                artifact = await self._service.create_image_artifact(
                    workspace_id, name
                )
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
            log = logger.bind(
                task_id=task_id, workspace_id=str(workspace_id)
            )
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
                if runtime_type:
                    await self._service.delete_image_reference(
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
                    },
                )
                log.info("image_artifact_deleted", image_artifact_id=image_artifact_id)
            except Exception as exc:
                await sio.emit(
                    "workspace:error",
                    {"task_id": task_id, "error": str(exc)},
                )
                log.exception("delete_image_artifact_failed")

        @sio.on("task:create_workspace_from_image_artifact")
        async def on_create_workspace_from_image_artifact(data: dict) -> None:
            task_id = data.get("task_id", str(uuid.uuid4()))
            image_artifact_id = data["image_artifact_id"]
            raw_ws_id = data.get("workspace_id")
            new_workspace_id = (
                uuid.UUID(raw_ws_id) if raw_ws_id else uuid.uuid4()
            )
            runtime_type = data.get("runtime_type", "qemu")
            log = logger.bind(task_id=task_id, image_artifact_id=image_artifact_id)
            log.info("task_received", task="create_workspace_from_image_artifact")
            try:
                ws_id = await self._service.create_workspace_from_image_artifact(
                    image_artifact_id=image_artifact_id,
                    new_workspace_id=new_workspace_id,
                    runtime_type=runtime_type,
                    qemu_vcpus=data.get("qemu_vcpus"),
                    qemu_memory_mb=data.get("qemu_memory_mb"),
                    qemu_disk_size_gb=data.get("qemu_disk_size_gb"),
                    env_vars=data.get("env_vars", {}),
                    ssh_keys=data.get("ssh_keys", []),
                )
                await sio.emit(
                    "workspace:created",
                    {
                        "task_id": task_id,
                        "workspace_id": str(ws_id),
                        "status": "created",
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
