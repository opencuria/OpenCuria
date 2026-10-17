"""Stable backend identity and journal with isolated runner code generations.

The supervisor owns one Socket.IO connection and one operation Journal. Each
execution generation is an immutable source snapshot in a separate Python
process; all existing WebSocketInterface handlers are reused behind an
authenticated private Unix socket. A generation is retired only after every
opaque session it owns drains.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import socketio
import structlog

from .config import RunnerSettings
from .journal import COMMANDS, TERMINAL, Journal, OperationExecutor, _active
from .main import build_workspace_service
from .supervised_ipc import (
    IPCError,
    IPCPeer,
    _read_frame,
    _write_frame,
    peer_uid,
    prepare_socket_path,
)

logger = structlog.get_logger(__name__)
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SESSION_EVENTS = {
    "terminal:attach",
    "terminal:input",
    "terminal:resize",
    "terminal:close",
    "workspace:stream_input",
    "workspace:stream_close",
    "workspace:stream_cancel",
    "desktop:proxy_ws_send",
    "desktop:proxy_ws_close",
    "harness:cancel",
    "harness:process_list",
    "harness:process_start",
    "harness:process_get",
    "harness:process_stop",
}
REQUIRED_HANDLERS = (
    COMMANDS
    | SESSION_EVENTS
    | {
        "task:start_terminal",
        "task:start_desktop",
        "task:stop_desktop",
        "harness:desktop_action",
        "workspace:artifact_ensure",
        "desktop:proxy_http_request",
        "desktop:clipboard_read",
        "desktop:clipboard_write",
        "desktop:proxy_ws_open",
        "workspace:stream_input",
        "workspace:stream_start",
        "harness:exec_stream",
        "harness:exec_wait",
        "harness:read_file",
        "harness:write_file",
        "harness:write_file_start",
        "harness:write_file_chunk",
        "harness:write_file_finish",
        "harness:list",
        "harness:stat",
        "harness:process_verify",
        "git:operation",
        "files:list",
        "files:find",
        "files:read",
        "files:upload",
        "files:upload_chunk",
        "files:upload_finish",
        "files:download",
        "task:cleanup_unknown_workspace",
        "task:list_image_artifacts",
    }
)


@dataclass
class WorkerGeneration:
    """One immutable worker process and its current authenticated IPC peer."""

    generation_id: str
    source_dir: Path
    process: asyncio.subprocess.Process
    peer: IPCPeer | None = None
    events: set[str] = field(default_factory=set)
    ready: bool = False
    active: bool = False
    started_at: float = field(default_factory=time.time)
    in_flight: int = 0
    last_error: str | None = None


class _JournalSocketShim:
    """Expose only OperationExecutor's expected event registry and emit hook."""

    def __init__(self) -> None:
        self.handlers: dict[str, dict[str, Any]] = {"/": {}}
        self.connected = False
        self.emit = self._no_emit
        self._backend = None
        self.supervised_owner = True

    def bind(self, backend: Any) -> None:
        """Bind the one stable backend Socket.IO client."""
        self._backend = backend
        for event, handler in self.handlers["/"].items():
            backend.on(event, handler)

    def on(self, event: str, handler: Any) -> None:
        self.handlers["/"][event] = handler
        if self._backend is not None:
            self._backend.on(event, handler)

    async def _no_emit(self, event: str, data=None, *args, **kwargs) -> None:
        raise RuntimeError(f"Journal socket is not bound for {event}")


class RunnerSupervisor:
    """Own the backend connection, journal, and generation/session router."""

    def __init__(self, settings: RunnerSettings) -> None:
        self.settings = settings
        self.state_dir = Path(settings.state_dir).expanduser()
        self.generations_dir = self.state_dir / "generations"
        self.socket_path = self.state_dir / "supervisor.sock"
        self.status_path = self.state_dir / "supervisor-status.json"
        self.active_path = self.state_dir / "active-generation"
        self.epoch_path = self.state_dir / "supervisor-desktop-epoch"
        self._ipc_token = uuid.uuid4().hex + uuid.uuid4().hex
        self._desktop_epoch = self._load_or_create_epoch()
        self._workers: dict[str, WorkerGeneration] = {}
        self._active_generation: str | None = None
        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=2,
            reconnection_delay_max=30,
            logger=False,
            websocket_extra_options={"max_msg_size": 200 * 1024 * 1024},
        )
        self._gateway = _JournalSocketShim()
        self._journal: Journal | None = None
        self._operations: OperationExecutor | None = None
        self._admin_token = settings.api_token
        self._observer = None
        self._server: asyncio.AbstractServer | None = None
        self._activation_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._inventory_sequence = 0
        self._last_inventory = 0.0
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._terminal_owners: dict[str, str] = {}
        self._terminal_workspaces: dict[str, str] = {}
        self._terminal_ids: dict[str, str] = {}
        self._terminal_handles: dict[tuple[str, str], str] = {}
        self._stream_owners: dict[str, str] = {}
        self._stream_workspaces: dict[str, str] = {}
        self._tunnel_owners: dict[str, str] = {}
        self._tunnel_workspaces: dict[str, str] = {}
        self._process_owners: dict[tuple[str, str], str] = {}
        self._desktop_workspace_owners: dict[str, str] = {}
        self._request_owners: dict[str, str] = {}
        self._request_workspaces: dict[str, str] = {}
        self._operation_owners: dict[str, tuple[str, dict[str, Any]]] = {}
        self._task_owners: dict[str, str] = {}
        self._operation_workspace_owners: dict[str, str] = {}
        self._operation_terminal_events: dict[
            str, list[tuple[str, dict[str, Any]]]
        ] = {}
        self._run_owners: dict[str, str] = {}
        self._run_roots: dict[str, str] = {}
        self._run_workers: dict[str, str] = {}
        self._run_authorities: dict[str, dict[str, str]] = {}
        self._run_workspaces: dict[str, str] = {}
        self._pinned_workspaces: dict[str, str] = {}
        self._workspace_endpoints: dict[str, str] = {}
        self._background_owners: dict[tuple[str, str], str] = {}
        self._live_request_owners: dict[tuple[str, str], str] = {}
        self._pending_dispatch: dict[tuple[str, str], str] = {}
        self._rpc_routes: dict[tuple[str, str], str] = {}
        self._upload_owners: dict[tuple[str, str], str] = {}
        self._upload_events: dict[
            tuple[str, str], list[tuple[str, dict[str, Any]]]
        ] = {}
        self._git_owners: dict[str, str] = {}
        self._git_workspaces: dict[str, str] = {}
        self._registered_events: set[str] = set()
        self._stopping = False
        self._register_connection_events()

    @property
    def active_generation(self) -> str | None:
        """Current generation for unpinned, new work."""
        return self._active_generation

    def _register_connection_events(self) -> None:
        """Install only stable Socket.IO lifecycle callbacks at construction."""

        @self._sio.event
        async def connect() -> None:
            self._gateway.connected = True
            await self._register_backend()

        @self._sio.event
        async def disconnect() -> None:
            self._gateway.connected = False
            logger.warning("supervisor_backend_disconnected")

        @self._sio.event
        async def connect_error(data: object) -> None:
            logger.error("supervisor_backend_connect_error", data=data)

    async def run(self) -> None:
        """Start the private IPC listener, one journal, worker and backend client."""
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        self.generations_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._journal = Journal(str(self.state_dir), defer_recovery=True)
        prepare_socket_path(str(self.socket_path))
        self._server = await asyncio.start_unix_server(
            self._accept_worker,
            path=str(self.socket_path),
            limit=256 * 1024 * 1024,
        )
        os.chmod(self.socket_path, 0o600)
        for workspace_id, terminal_id in self._journal.active_terminals().items():
            self._terminal_ids[workspace_id] = terminal_id
            self._terminal_workspaces[terminal_id] = workspace_id
            self._pinned_workspaces[workspace_id] = ""
        self._observer = await build_workspace_service(self.settings, logger)
        self._observer._journal = self._journal
        for runtime in self._observer._runtimes.values():
            if getattr(runtime, "runtime_type", None) == "docker":
                runtime._publication_journal = self._journal
        self._operations = OperationExecutor(
            self._gateway,
            self._journal,
            reconciler=getattr(self._observer, "publication_evidence", None),
            observer=self._observer,
        )
        # The Journal executor's ACK/replay/inspect handlers live on the sole
        # backend socket. Workers never open Journal or connect to Socket.IO.
        self._operations.emit = self._sio.emit
        self._gateway.bind(self._sio)
        self._register_gateway_handlers()
        # Start inventory/lease maintenance independently of backend reconnects.
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._write_status()
        try:
            initial = self._read_active_generation()
            if initial is None:
                initial = f"builtin-{uuid.uuid4().hex[:12]}"
                self._stage_source(
                    Path(__file__).resolve().parents[1], initial, activate=True
                )
            await self._activate_generation(initial)
            self._activation_task = asyncio.create_task(self._activation_loop())
            await self._sio.connect(
                self.settings.backend_url,
                headers={"Authorization": f"Bearer {self._admin_token}"},
                auth={"token": self._admin_token} if self._admin_token else None,
                transports=["websocket"],
                socketio_path=self.settings.socketio_path,
            )
            await self._sio.wait()
        finally:
            await self._drain_for_supervisor_shutdown()
            self._stopping = True
            for task in (self._activation_task, self._heartbeat_task):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (self._activation_task, self._heartbeat_task)
                    if task
                ),
                return_exceptions=True,
            )
            if self._sio.connected:
                await self._sio.disconnect()
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                self.socket_path.unlink()
            if self._journal is not None:
                self._journal.close()
            self._write_status(stopped=True)

    def _register_gateway_handlers(self) -> None:
        """Install exactly one operation and inventory handler set."""
        assert self._operations is not None
        # OperationExecutor's wrapper calls these registered shims; real
        # backend handlers are installed once here, with no wrapper double-run.
        for event in COMMANDS:
            self._sio.on(event, self._journal_command(event))
        self._sio.on("operation:ack", self._acknowledge)
        self._sio.on("operation:heartbeat", self._operation_heartbeat)
        self._sio.on("operation:result", self._operation_result)
        self._sio.on("runner:request_route", self._runner_route_request)
        self._sio.on("operation:inspect", self._inspect)
        self._sio.on("operation:inspect_request", self._inspect_request)
        self._sio.on("operation:seal", self._seal)
        self._sio.on("inventory:refresh", self._refresh_inventory)
        for event in (
            "terminal:attach",
            "terminal:input",
            "terminal:resize",
            "terminal:close",
        ):

            async def route_terminal(data=None, *, _event=event):
                return await self._route_terminal_event(
                    _event, data if isinstance(data, dict) else {}
                )

            self._sio.on(event, route_terminal)

    async def _route_terminal_input_ack(
        self, generation_id: str, payload: dict[str, Any], input_id: str
    ) -> dict[str, Any]:
        """Route the write receipt to the one browser that submitted stdin."""
        if not input_id:
            return {"ok": False, "error": "input_id is required"}
        result = await self._sio.call(
            "terminal:input_ack",
            payload,
            timeout=15,
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            return {"ok": False, "error": "requesting frontend is disconnected"}
        return {
            "ok": bool(payload.get("ok")),
            "duplicate": bool(payload.get("duplicate")),
        }

    async def _route_terminal_event(self, event: str, data: dict[str, Any]) -> Any:
        """Route PTY operations to their original generation, surviving API reconnects."""
        terminal_id = str(data.get("terminal_id", ""))
        workspace_id = str(data.get("workspace_id", ""))
        if not terminal_id and workspace_id:
            terminal_id = self._terminal_ids.get(workspace_id, "")
            data = {**data, "terminal_id": terminal_id}
        if event == "terminal:attach" and not terminal_id:
            raise IPCError("Terminal attach requires the stable terminal_id")
        if event == "terminal:attach" and self._journal is not None:
            self._terminal_ids[workspace_id] = terminal_id
        if (
            event in {"terminal:input", "terminal:resize", "terminal:close"}
            and not terminal_id
        ):
            raise IPCError("Terminal event requires terminal_id")
        generation = self._owner_for_event(event, data)
        if generation is None and terminal_id:
            generation = self._owner_for_event(
                "terminal:attach", {**data, "terminal_id": terminal_id}
            )
        if generation is None:
            raise IPCError("No generation owns this terminal PTY")
        result = await self._dispatch_to_generation(generation, event, data)
        if event == "terminal:attach" and isinstance(result, dict):
            return result
        return {"ok": True} if result is None else result

    @staticmethod
    def _authority_payload(data: dict[str, Any]) -> dict[str, str] | None:
        """Extract the complete durable harness lease without granting authority."""
        raw = data.get("run_authority")
        if not isinstance(raw, dict):
            return None
        keys = (
            "run_id",
            "owner_token",
            "worker_id",
            "deployment_version",
            "root_run_id",
        )
        if any(not isinstance(raw.get(key), str) or not raw[key] for key in keys):
            return None
        try:
            uuid.UUID(raw["run_id"])
            uuid.UUID(raw["owner_token"])
            uuid.UUID(raw["root_run_id"])
        except ValueError:
            return None
        return {key: raw[key] for key in keys}

    async def _backend_workspace_owner(
        self, workspace_id: str
    ) -> tuple[str | None, bool]:
        """Read durable run ownership when the backend authority endpoint exists.

        The runner has no authority to decide which worker owns a Harness run.
        A positive backend answer pins its worker; a definite empty answer means
        workspace-level backend operations may use the selected runner generation.
        If the endpoint is unavailable, existing in-memory route state is used for
        ordinary operations, but run-scoped operations fail closed.
        """
        if not workspace_id:
            return None, False
        if not self._sio.connected:
            return None, False
        try:
            response = await self._sio.call(
                "runner:workspace_authority",
                {"workspace_id": workspace_id},
                timeout=5,
            )
        except (socketio.exceptions.TimeoutError, asyncio.TimeoutError, OSError):
            return None, False
        if not isinstance(response, dict) or response.get("ok") is not True:
            return None, False
        owner = response.get("owner_worker_id")
        if owner is not None and (not isinstance(owner, str) or not owner):
            return None, False
        return owner, True

    async def _select_lifecycle_worker(
        self, data: dict[str, Any]
    ) -> tuple[str, dict[str, str] | None]:
        """Keep a lifecycle operation on its workspace owner, never transfer effects."""
        workspace_id = str(data.get("workspace_id", ""))
        generation = self._workspace_owner(workspace_id) or self._active_generation
        if generation is None:
            raise IPCError("No ready worker for lifecycle operation")
        return generation, None

    def _pin_run_authority(
        self, authority: dict[str, str], generation_id: str, workspace_id: str
    ) -> None:
        """Remember backend worker and root-run pins until explicit completion."""
        run_id = authority["run_id"]
        root_id = authority["root_run_id"]
        self._run_owners[run_id] = generation_id
        self._run_roots[run_id] = root_id
        self._run_workers[run_id] = authority["worker_id"]
        self._run_authorities[run_id] = dict(authority)
        self._run_workspaces[run_id] = workspace_id
        self._run_owners[root_id] = generation_id
        self._run_roots[root_id] = root_id
        self._run_workers[root_id] = authority["worker_id"]
        self._run_authorities[root_id] = dict(authority)
        self._run_workspaces[root_id] = workspace_id
        # Run affinity is independent of long-lived terminal affinity.

    def _pin_workspace(self, workspace_id: str, generation_id: str) -> None:
        """Retain workspace affinity until explicit stop/remove drains the domain."""
        if workspace_id:
            existing = self._pinned_workspaces.get(workspace_id)
            if existing is not None and existing != generation_id:
                raise IPCError("Workspace remains pinned to an older generation")
            self._pinned_workspaces[workspace_id] = generation_id

    def _release_workspace_pin(self, workspace_id: str) -> None:
        """Clear routing pins only after definitive workspace stop/remove."""
        if not workspace_id:
            return
        if any(
            bound_workspace == workspace_id
            for bound_workspace, _process_id in self._process_owners
        ) or any(
            bound_workspace == workspace_id
            for bound_workspace, _process_id in self._background_owners
        ):
            raise IPCError("Detached workspace process ownership is still live")
        self._pinned_workspaces.pop(workspace_id, None)
        self._workspace_endpoints.pop(workspace_id, None)
        for operation_id, operation_workspace in list(
            self._operation_workspace_owners.items()
        ):
            if operation_workspace == workspace_id:
                self._operation_workspace_owners.pop(operation_id, None)
                self._operation_owners.pop(operation_id, None)
        for run_id, run_workspace in list(self._run_workspaces.items()):
            if run_workspace == workspace_id:
                self._run_workspaces.pop(run_id, None)
                root_id = self._run_roots.pop(run_id, None)
                self._run_owners.pop(run_id, None)
                self._run_workers.pop(run_id, None)
                self._run_authorities.pop(run_id, None)
                if root_id == run_id:
                    for child_id, child_root in list(self._run_roots.items()):
                        if child_root == run_id:
                            self._run_roots.pop(child_id, None)
                            self._run_owners.pop(child_id, None)
                            self._run_workers.pop(child_id, None)
                            self._run_authorities.pop(child_id, None)
        for request_id, request_workspace in list(self._request_workspaces.items()):
            if request_workspace == workspace_id:
                self._request_workspaces.pop(request_id, None)
                self._request_owners.pop(request_id, None)
        for (bound_workspace, request_id), _owner in list(
            self._live_request_owners.items()
        ):
            if bound_workspace == workspace_id:
                self._live_request_owners.pop((bound_workspace, request_id), None)
        for (bound_workspace, process_id), owner in list(
            self._background_owners.items()
        ):
            if bound_workspace == workspace_id:
                self._background_owners.pop((bound_workspace, process_id), None)
        for request_id, bound_workspace in list(self._git_workspaces.items()):
            if bound_workspace == workspace_id:
                self._git_workspaces.pop(request_id, None)
                self._git_owners.pop(request_id, None)
        for key in [
            key
            for key in self._terminal_workspaces
            if self._terminal_workspaces[key] == workspace_id
        ]:
            self._terminal_workspaces.pop(key, None)
            self._terminal_owners.pop(key, None)
            self._terminal_ids.pop(workspace_id, None)
            if self._journal is not None:
                self._journal.forget_terminal(workspace_id, key)
        for key in [key for key in self._terminal_handles if key[0] == workspace_id]:
            self._terminal_handles.pop(key, None)
        for key in [
            key
            for key in self._stream_workspaces
            if self._stream_workspaces[key] == workspace_id
        ]:
            self._stream_workspaces.pop(key, None)
            self._stream_owners.pop(key, None)
        for tunnel_id, tunnel_workspace in list(self._tunnel_workspaces.items()):
            if tunnel_workspace == workspace_id:
                self._tunnel_workspaces.pop(tunnel_id, None)
                self._tunnel_owners.pop(tunnel_id, None)
        for key in [key for key in self._process_owners if key[0] == workspace_id]:
            self._process_owners.pop(key, None)
        self._desktop_workspace_owners.pop(workspace_id, None)
        for key in [key for key in self._upload_owners if key[0] == workspace_id]:
            self._upload_owners.pop(key, None)
            self._upload_events.pop(key, None)

    def _journal_command(self, event: str):
        """Use the existing OperationExecutor's journaling and no-retry policy."""
        assert self._operations is not None
        return self._operations.wrap(event, self._remote_handler(event))

    async def _runner_route_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """Recheck backend authority; generation identity never impersonates a worker."""
        request = data.get("request") or {}
        event = str(data.get("runner_event", ""))
        if not isinstance(request, dict) or not event:
            return {"ok": False}
        try:
            response = await self._sio.call(
                "runner:authorize_request",
                {
                    "workspace_id": str(request.get("workspace_id", "")),
                    "event": event,
                    "action": request.get("action", ""),
                    "run_authority": request.get("run_authority"),
                },
                timeout=5,
            )
        except (socketio.exceptions.TimeoutError, asyncio.TimeoutError, OSError):
            return {"ok": False, "error": "Backend authority unavailable"}
        if not isinstance(response, dict) or response.get("ok") is not True:
            return {"ok": False, "error": "Backend authority rejected"}
        authority = self._authority_payload(request)
        if "run_authority" in request and authority is None:
            return {"ok": False, "error": "Malformed authority"}
        generation = self._owner_for_event(event, request)
        if authority is not None:
            resource_owner = None
            workspace_id = str(request.get("workspace_id", ""))
            if event == "harness:desktop_action":
                resource_owner = self._desktop_workspace_owners.get(workspace_id)
            elif event in {"harness:process_get", "harness:process_stop"}:
                resource_owner = self._process_owners.get(
                    (workspace_id, str(request.get("process_id", "")))
                )
            elif event == "harness:cancel":
                resource_owner = self._request_owners.get(
                    str(request.get("request_id", ""))
                )
            if resource_owner is not None:
                # Resource affinity wins without repinning the entire agent run.
                generation = resource_owner
            else:
                generation = (
                    self._run_owners.get(authority["root_run_id"])
                    or self._active_generation
                )
                if generation is not None:
                    self._pin_run_authority(authority, generation, workspace_id)
        if generation is None:
            return {"ok": False, "error": "No ready generation"}
        return {"ok": True, "generation": generation, "run_authority": authority}

    def _remote_handler(self, event: str):
        """Dispatch one journal command to its selected worker exactly once."""

        async def dispatch(data: dict[str, Any]) -> None:
            operation_id = str(data.get("operation_id", ""))
            task_id = str(data.get("task_id", ""))
            generation_id, authority = await self._select_lifecycle_worker(data)
            worker = self._workers.get(generation_id)
            if worker is None or not worker.ready or worker.peer is None:
                raise IPCError(
                    "No ready execution generation; journal identity retained for inspection"
                )
            request_data = dict(data)
            if authority is not None:
                request_data["run_authority"] = authority
            self._operation_terminal_events[operation_id] = []
            self._pending_dispatch[(generation_id, operation_id)] = event
            if operation_id:
                self._operation_workspace_owners[operation_id] = str(
                    data.get("workspace_id", "")
                )
            binding = {
                key: request_data[key]
                for key in (
                    "task_id",
                    "operation_id",
                    "attempt",
                    "target",
                    "runner_id",
                    "workspace_id",
                    "build_job_id",
                    "image_instance_id",
                )
                if key in request_data
            }
            if operation_id:
                self._operation_owners[operation_id] = (generation_id or "", binding)
            if task_id:
                self._operation_owners[task_id] = (generation_id or "", binding)
                self._task_owners[task_id] = generation_id
            if operation_id:
                self._operation_workspace_owners[operation_id] = str(
                    data.get("workspace_id", "")
                )
            if generation_id in self._workers:
                self._workers[generation_id].in_flight += 1
            try:
                await self._dispatch_to_generation(
                    generation_id, event, request_data, lifecycle=True
                )
                terminal = self._operation_terminal_events.get(operation_id, [])
                if terminal:
                    terminal_event, terminal_data = terminal[-1]
                    await self._gateway.emit(terminal_event, terminal_data)
            finally:
                self._operation_terminal_events.pop(operation_id, None)
                self._pending_dispatch.pop((generation_id, operation_id), None)
                if operation_id:
                    self._operation_owners.pop(operation_id, None)
                    self._operation_workspace_owners.pop(operation_id, None)
                if task_id:
                    self._operation_owners.pop(task_id, None)
                    self._task_owners.pop(task_id, None)
                if generation_id in self._workers:
                    self._workers[generation_id].in_flight = max(
                        self._workers[generation_id].in_flight - 1, 0
                    )
                self._write_status()

        return dispatch

    async def _route_backend_event(self, event: str, data: dict[str, Any]) -> Any:
        """Route one backend callback to its pinned worker or active generation."""
        generation_id = self._owner_for_event(event, data)
        if generation_id is None:
            if event in COMMANDS:
                raise IPCError("No ready worker generation; command was not delivered")
            return None
        return await self._dispatch_to_generation(generation_id, event, data)

    async def _dispatch_to_generation(
        self,
        generation_id: str | None,
        event: str,
        data: dict[str, Any],
        *,
        lifecycle: bool = False,
    ) -> Any:
        """Dispatch exactly once to one ready generation, serialized per target."""
        worker = self._workers.get(generation_id or "")
        if worker is None or not worker.ready or worker.peer is None:
            raise IPCError("No ready generation; request was not delivered")
        if event not in worker.events:
            raise IPCError(f"Generation does not implement {event}")
        key_value = data.get("workspace_id") or data.get("target")
        lock = (
            self._workspace_locks.setdefault(str(key_value), asyncio.Lock())
            if key_value and lifecycle
            else None
        )
        request_id = str(data.get("request_id", ""))
        workspace_id = str(data.get("workspace_id", ""))
        if request_id:
            self._request_owners.setdefault(request_id, worker.generation_id)
            self._request_workspaces.setdefault(request_id, workspace_id)
        if event == "harness:write_file_start" or (
            event == "files:upload" and data.get("chunked")
        ):
            self._upload_owners[(workspace_id, request_id)] = worker.generation_id
        worker.in_flight += 1
        try:
            if lock is None:
                response = await worker.peer.call(
                    "worker.dispatch", {"event": event, "data": data}, timeout=None
                )
            else:
                async with lock:
                    response = await worker.peer.call(
                        "worker.dispatch", {"event": event, "data": data}, timeout=None
                    )
            result = response.get("result") if isinstance(response, dict) else response
            self._record_dispatch_result(worker.generation_id, event, data, result)
            if event == "terminal:attach" and isinstance(result, dict):
                return result
            if event.startswith("terminal:") and isinstance(result, dict):
                return result
            return result
        except IPCError:
            # The worker may have performed an external side effect before IPC
            # loss. Do not retry on another generation or fabricate success.
            worker.last_error = "IPC result unknown; request was not replayed"
            raise
        finally:
            worker.in_flight = max(worker.in_flight - 1, 0)
            self._write_status()

    async def _register_worker_events(
        self, worker: WorkerGeneration, events: list[Any]
    ) -> None:
        """Validate generation capabilities before it can receive backend events."""
        if len(events) > 512 or any(
            not isinstance(event, str) or len(event) > 128 for event in events
        ):
            raise IPCError("Invalid or oversized handler manifest")
        declared = set(events)
        missing = REQUIRED_HANDLERS - declared
        if missing:
            raise IPCError(f"Worker missing required capabilities: {sorted(missing)}")
        worker.events = declared
        candidates = (
            declared
            - self._registered_events
            - {
                "connect",
                "disconnect",
                "connect_error",
                *COMMANDS,
            }
        )
        for event in sorted(candidates):
            if event in self._sio.handlers.get("/", {}):
                continue

            async def route(data=None, *, _event=event):
                payload = data if isinstance(data, dict) else {}
                if _event.startswith("terminal:"):
                    if _event == "terminal:attach":
                        generation = self._owner_for_event(_event, payload)
                        if generation is None:
                            generation = self._pinned_workspaces.get(
                                str(payload.get("workspace_id", ""))
                            )
                    else:
                        generation = self._owner_for_event(_event, payload)
                        if generation is None:
                            terminal_id = self._terminal_ids.get(
                                str(payload.get("workspace_id", ""))
                            )
                            if terminal_id:
                                generation = self._owner_for_event(
                                    "terminal:attach",
                                    {**payload, "terminal_id": terminal_id},
                                )
                elif _event in {
                    "harness:write_file_chunk",
                    "harness:write_file_finish",
                }:
                    key = (
                        str(payload.get("workspace_id", "")),
                        str(payload.get("request_id", "")),
                    )
                    generation = self._upload_owners.get(key)
                    if generation is None:
                        raise IPCError("Chunked write has no original generation")
                elif _event in {
                    "harness:exec_stream",
                    "harness:exec_wait",
                    "workspace:stream_start",
                    "workspace:artifact_ensure",
                    "harness:process_start",
                    "harness:process_list",
                    "harness:process_get",
                    "harness:process_stop",
                    "harness:process_verify",
                    "harness:read_file",
                    "harness:write_file",
                    "harness:write_file_start",
                    "harness:write_file_chunk",
                    "harness:write_file_finish",
                    "harness:list",
                    "harness:stat",
                    "harness:desktop_action",
                    "harness:cancel",
                    "git:operation",
                }:
                    route_payload = {
                        "workspace_id": str(payload.get("workspace_id", "")),
                        "request_id": str(payload.get("request_id", "")),
                        "runner_event": _event,
                        "request": payload,
                    }
                    try:
                        response = await self._runner_route_request(route_payload)
                    except (
                        socketio.exceptions.TimeoutError,
                        asyncio.TimeoutError,
                        OSError,
                    ) as exc:
                        raise IPCError(
                            "Backend did not authorize runner request"
                        ) from exc
                    if not isinstance(response, dict) or response.get("ok") is not True:
                        raise IPCError("Backend rejected runner request")
                    generation = str(response.get("generation", ""))
                    if response.get("run_authority") is not None:
                        authority = self._authority_payload(
                            {"run_authority": response["run_authority"]}
                        )
                        if authority is None:
                            raise IPCError("Backend returned malformed run authority")
                        payload = {**payload, "run_authority": authority}
                else:
                    generation = self._owner_for_event(_event, payload)
                    if generation is None:
                        generation = self._pinned_workspaces.get(
                            str(payload.get("workspace_id", ""))
                        )
                return await self._dispatch_to_generation(generation, _event, payload)

            self._sio.on(event, route)
            self._registered_events.add(event)
        worker.ready = True
        self._write_status()

    async def _worker_request(
        self, generation_id: str, method: str, data: dict[str, Any]
    ) -> Any:
        """Handle the narrow worker protocol: ready, emit, ACK call, dispatch."""
        worker = self._workers.get(generation_id)
        if worker is None:
            raise IPCError("Unknown generation")
        if method == "terminal.call":
            event = str(data.get("event", ""))
            payload = data.get("data")
            if event not in {
                "terminal:attach",
                "terminal:input",
                "terminal:resize",
                "terminal:close",
                "terminal:input_ack",
            }:
                raise IPCError("Terminal call event is not allowed")
            if not isinstance(payload, dict):
                raise IPCError("Terminal call payload is invalid")
            workspace_id = str(payload.get("workspace_id", ""))
            terminal_id = str(payload.get("terminal_id", ""))
            if not workspace_id or not terminal_id:
                raise IPCError("Terminal call requires workspace_id and terminal_id")
            generation_id = self._owner_for_event(
                "terminal:attach", {**payload, "terminal_id": terminal_id}
            )
            if generation_id is None:
                raise IPCError("No worker generation owns terminal")
            if event == "terminal:attach":
                response = await self._dispatch_to_generation(
                    generation_id, event, payload
                )
                return response if isinstance(response, dict) else {"ok": False}
            if event == "terminal:input_ack":
                return await self._route_terminal_input_ack(
                    generation_id, payload, str(payload.get("input_id", ""))
                )
            return await self._dispatch_to_generation(generation_id, event, payload)
        if method == "worker.ready":
            if worker.process.returncode is not None:
                raise IPCError("Worker exited before readiness")
            await self._register_worker_events(worker, data.get("events", []))
            return {"ok": True, "active": worker.active}
        if method == "worker.maintenance":
            ids = data.get("workspace_ids")
            if not isinstance(ids, list) or len(ids) > 100_000:
                raise IPCError("Invalid maintenance workspace set")
            if worker.peer is None:
                raise IPCError("Worker IPC is unavailable")
            await worker.peer.send_event("worker.maintenance", {"workspace_ids": ids})
            return {"accepted": True}
        if method == "backend.emit":
            event = str(data.get("event", ""))
            payload = data.get("data")
            if not event or not isinstance(payload, dict):
                raise IPCError("Invalid worker event")
            if event.startswith("harness:write_file_"):
                request_id = str(payload.get("request_id", ""))
                workspace_id = str(payload.get("workspace_id", ""))
                key = (workspace_id, request_id)
                if event.endswith("_start"):
                    if not request_id or not workspace_id:
                        raise IPCError(
                            "Chunked write start must have request/workspace IDs"
                        )
                    self._upload_owners[key] = generation_id
                    self._upload_events[key] = []
                    await self._sio.emit(event, payload)
                    self._write_status()
                    return {"accepted": True}
                if event.endswith("_chunk"):
                    if self._upload_owners.get(key) != generation_id:
                        raise IPCError("Chunked write owner mismatch")
                    self._upload_events.setdefault(key, []).append((event, payload))
                    return {"accepted": True}
                if event.endswith("_finish"):
                    if self._upload_owners.get(key) != generation_id:
                        raise IPCError("Chunked write owner mismatch")
                    chunks = self._upload_events.get(key, [])
                    for chunk_event, chunk_payload in chunks:
                        await self._sio.emit(chunk_event, chunk_payload)
                    await self._sio.emit(event, payload)
                    self._upload_events.pop(key, None)
                    self._upload_owners.pop(key, None)
                    self._write_status()
                    return {"accepted": True}
            self._record_session_owner(generation_id, event, payload)
            if event.startswith("terminal:"):
                response = await self._sio.call(event, payload, timeout=15)
                return {"accepted": True, "response": response}
            binding = (
                self._operation_binding(generation_id, payload)
                if event in TERMINAL
                else None
            )
            if binding is not None and self._operations is not None:
                terminal_data = {**payload, **binding, "execution_finished": True}
                op_id = str(binding.get("operation_id", ""))
                active_operation = _active.get()
                if active_operation and active_operation.get("operation_id") == op_id:
                    await self._operations.record_emit(event, terminal_data)
                else:
                    self._operation_terminal_events.setdefault(op_id, []).append(
                        (event, terminal_data)
                    )
            else:
                await self._sio.emit(event, payload)
            self._write_status()
            return {"accepted": True}
        if method == "runner.route":
            event = str(data.get("event", ""))
            payload = data.get("data") or {}
            if event != "runner:request_route" or not isinstance(payload, dict):
                raise IPCError("Unknown privileged runner route")
            response = await self._runner_route_request(payload)
            if not response.get("ok"):
                raise IPCError(str(response.get("error", "runner route rejected")))
            target = response.get("generation")
            if target != generation_id:
                raise IPCError("Runner request is pinned to another generation")
            authority = response.get("run_authority")
            request = payload.get("request")
            if authority is not None:
                if not isinstance(authority, dict) or not isinstance(request, dict):
                    raise IPCError("Malformed run authority forwarding request")
                request = dict(request)
                request["run_authority"] = authority
            runner_event = str(payload.get("runner_event", ""))
            if not runner_event or runner_event not in worker.events:
                raise IPCError("Runner event is not declared by this worker")
            return await self._dispatch_to_generation(
                generation_id,
                runner_event,
                request if isinstance(request, dict) else {},
            )
        if method == "backend.call":
            event = str(data.get("event", ""))
            payload = data.get("data")
            timeout = float(data.get("timeout", 30))
            if not event or not isinstance(payload, dict) or not 0 < timeout <= 300:
                raise IPCError("Invalid backend call")
            self._record_session_owner(generation_id, event, payload)
            return await self._sio.call(event, payload, timeout=timeout)
        if method == "worker.upload_chunk":
            workspace_id = str(data.get("workspace_id", ""))
            request_id = str(data.get("request_id", ""))
            key = (workspace_id, request_id)
            if (
                key not in self._upload_owners
                or self._upload_owners[key] != generation_id
            ):
                raise IPCError("Upload generation is not pinned")
            self._upload_events.setdefault(key, []).append(
                (str(data.get("event", "")), data.get("data") or {})
            )
            return {"accepted": True}
        if method == "worker.upload_finish":
            workspace_id = str(data.get("workspace_id", ""))
            request_id = str(data.get("request_id", ""))
            key = (workspace_id, request_id)
            if (
                key not in self._upload_owners
                or self._upload_owners[key] != generation_id
            ):
                raise IPCError("Upload generation is not pinned")
            result = await self._dispatch_to_generation(
                generation_id,
                str(data.get("event", "")),
                data.get("data") or {},
            )
            chunks = self._upload_events.pop(key, [])
            for event, payload in chunks:
                await self._dispatch_to_generation(generation_id, event, payload)
            return result
        if method == "worker.inbound":
            event = str(data.get("event", ""))
            payload = data.get("data") or {}
            if not isinstance(payload, dict):
                raise IPCError("Invalid backend payload")
            generation = self._owner_for_event(event, payload)
            if generation is None and event.startswith("terminal:"):
                terminal_id = str(payload.get("terminal_id", ""))
                workspace_id = str(payload.get("workspace_id", ""))
                if not terminal_id and workspace_id:
                    terminal_id = self._terminal_ids.get(workspace_id, "")
                    payload = {**payload, "terminal_id": terminal_id}
                if terminal_id:
                    generation = self._owner_for_event(
                        "terminal:attach", {**payload, "terminal_id": terminal_id}
                    )
            return await self._dispatch_to_generation(generation, event, payload)
        if method == "worker.control":
            if data.get("action") == "stop_metrics":
                await worker.peer.send_event("worker.control", data)
                return {"accepted": True}
            raise IPCError("Unknown worker control action")
        if method == "worker.dispatch":
            event = str(data.get("event", ""))
            payload = data.get("data") or {}
            if event not in worker.events or not isinstance(payload, dict):
                raise IPCError("Handler is not in this generation manifest")
            if self._stopping:
                raise IPCError("Supervisor is draining for shutdown")
            return await self._dispatch_to_generation(
                generation_id, event, payload, lifecycle=event in COMMANDS
            )
        raise IPCError("Worker IPC method is not allowed")

    def _operation_binding(
        self, generation_id: str, data: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Find the lifecycle context for terminal evidence from this worker."""
        for key in (data.get("operation_id"), data.get("task_id")):
            if key and str(key) in self._operation_owners:
                owner, binding = self._operation_owners[str(key)]
                if owner == generation_id:
                    return binding
        return None

    async def _acknowledge(self, data: dict[str, Any]) -> None:
        """Persist exact operation result ACK in the sole journal."""
        if self._journal is not None:
            self._journal.acknowledge(data)

    async def _operation_heartbeat(self, data: dict[str, Any]) -> None:
        """Forward one journal running-operation heartbeat from the stable owner."""
        await self._sio.emit("operation:heartbeat", data)

    async def _operation_result(self, data: dict[str, Any]) -> None:
        """Forward one durable terminal journal record to the backend."""
        await self._sio.emit("operation:result", data)

    async def _inspect(self, data: dict[str, Any]) -> dict[str, Any]:
        """Inspect exact durable lifecycle evidence without runtime side effects."""
        if self._operations is None:
            return {"status": "unknown", "execution_finished": False}
        return await self._operations.inspect(data)

    async def _inspect_request(self, data: dict[str, Any]) -> None:
        """Reply-event variant used by the backend recovery worker."""
        await self._sio.emit("operation:inspection", await self._inspect(data))

    async def _seal(self, data: dict[str, Any]) -> dict[str, Any]:
        """Delegate irreversible operator seal decisions to the sole Journal."""
        if self._operations is None:
            return {"status": "unknown", "execution_finished": False}
        return await self._operations.seal(data)

    async def _refresh_inventory(self, data=None) -> None:
        """Perform an explicit inventory scan through the stable observer."""
        if self._observer is None:
            return
        await self._send_inventory()

    async def _send_inventory(self) -> None:
        """Emit one fresh inventory scan with a monotonic sequence."""
        snapshot = await self._observer.inventory_for_runner()
        self._inventory_sequence += 1
        snapshot.update(
            inventory_epoch=self._desktop_epoch,
            inventory_sequence=self._inventory_sequence,
        )
        self._last_inventory = time.monotonic()
        await self._sio.emit("runner:inventory", snapshot)

    async def _register_backend(self) -> None:
        """Register one stable backend runner identity with its stable epoch."""
        await self._sio.emit(
            "runner:register",
            {
                "supported_runtimes": self.settings.enabled_runtime_list,
                "status": "ready" if self._active_generation else "maintenance",
                "inventory_epoch": self._desktop_epoch,
                "desktop_leases": {"protocol_version": 1, "epoch": self._desktop_epoch},
                "supervisor": self._status_payload(),
            },
        )

    async def _heartbeat_loop(self) -> None:
        """Reconcile runtime truth and replay durable Journal records once."""
        while True:
            try:
                if self._observer is not None:
                    await self._observer.sync_from_runtime()
                    workspaces = await self._observer.get_workspace_heartbeat_statuses()
                else:
                    workspaces = []
                if self._sio.connected:
                    await self._sio.emit(
                        "runner:heartbeat",
                        {
                            "workspaces": workspaces,
                            "workspace_states_observed": True,
                            "snapshot_complete": False,
                            "inventory_epoch": self._desktop_epoch,
                            "desktop_epoch": self._desktop_epoch,
                            "worker_generations": self._status_payload()["generations"],
                        },
                    )
                    if self._operations is not None:
                        await self._operations.replay(recover=False)
                    await self._release_finished_run_pins()
                    await self._stop_worker_metrics_loops()
                    if (
                        time.monotonic() - self._last_inventory
                        >= self.settings.inventory_interval
                    ):
                        await self._send_inventory()
                await self._update_worker_maintenance(workspaces)
                await asyncio.sleep(self.settings.heartbeat_interval)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("supervisor_heartbeat_failed")
                await asyncio.sleep(self.settings.heartbeat_interval)

    async def _release_finished_run_pins(self) -> None:
        """Release only runs the authoritative backend confirms terminal."""
        if not self._run_owners:
            return
        try:
            response = await self._sio.call(
                "runner:run_states",
                {"run_ids": list(self._run_owners)[:512]},
                timeout=5,
            )
        except (socketio.exceptions.TimeoutError, asyncio.TimeoutError, OSError):
            return
        if not isinstance(response, dict):
            return
        for run_id in response.get("terminal_run_ids", []):
            self._run_owners.pop(run_id, None)
            self._run_roots.pop(run_id, None)
            self._run_workers.pop(run_id, None)
            self._run_authorities.pop(run_id, None)
            self._run_workspaces.pop(run_id, None)

    async def _stop_worker_metrics_loops(self) -> None:
        """Keep host metrics single-owned by stable supervisor, not each worker."""
        for worker in self._workers.values():
            if not worker.ready or worker.peer is None:
                continue
            with contextlib.suppress(IPCError):
                await worker.peer.call(
                    "worker.control", {"action": "stop_metrics"}, timeout=5
                )

    async def _update_worker_maintenance(
        self, workspaces: list[dict[str, Any]]
    ) -> None:
        """Partition lease maintenance by pinned workspace owner, never overlap."""
        if self._observer is None:
            return
        workspace_ids = {str(row.get("workspace_id", "")) for row in workspaces}
        for generation_id, worker in self._workers.items():
            if not worker.ready or worker.peer is None:
                continue
            allowed: list[str] = []
            for workspace_id in workspace_ids:
                pinned = self._workspace_owner(workspace_id)
                if pinned == generation_id or (
                    pinned is None and generation_id == self._active_generation
                ):
                    allowed.append(workspace_id)
            with contextlib.suppress(IPCError):
                await worker.peer.call(
                    "worker.maintenance", {"workspace_ids": allowed}, timeout=10
                )

    async def _activation_loop(self) -> None:
        """Watch the local admin-only activation marker and reap drained code."""
        while True:
            try:
                await asyncio.sleep(1)
                selected = self._read_active_generation()
                if selected and selected != self._active_generation:
                    await self._activate_generation(selected)
                await self._reap_drained_workers()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("worker_generation_activation_failed")
                await asyncio.sleep(2)

    async def _activate_generation(self, generation_id: str) -> None:
        """Start and capability-check an isolated worker before switching new work."""
        if not _GENERATION_RE.fullmatch(generation_id):
            raise ValueError("Invalid generation identifier")
        source_dir = self.generations_dir / generation_id
        worker_main = source_dir / "src" / "supervised_worker.py"
        if not worker_main.is_file():
            raise ValueError(f"Generation {generation_id!r} is not staged")
        manifest = json.loads((source_dir / "manifest.json").read_text())
        if manifest.get("source_sha256") != self._source_digest(source_dir / "src"):
            raise ValueError("Generation source changed after staging")
        existing = self._workers.get(generation_id)
        if (
            existing is not None
            and existing.process.returncode is not None
            and generation_id in self._owned_generations()
        ):
            raise IPCError(
                "Crashed generation still owns uncertain effects; maintenance required"
            )
        if existing and existing.ready and existing.process.returncode is None:
            self._switch_active(existing)
            return
        env = os.environ.copy()
        for name in list(env):
            if name.startswith("RUNNER_"):
                env.pop(name, None)
        for name, value in self.settings.model_dump().items():
            if name == "api_token":
                continue
            env[f"RUNNER_{name.upper()}"] = str(value)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RUNNER_SUPERVISED_MODE"] = "false"
        env["OPENCURIA_SUPERVISOR_SOCKET"] = str(self.socket_path)
        env["OPENCURIA_SUPERVISOR_TOKEN"] = self._ipc_token
        env["OPENCURIA_GENERATION_ID"] = generation_id
        env["OPENCURIA_DESKTOP_EPOCH"] = self._desktop_epoch
        env["PYTHONPATH"] = str(source_dir) + os.pathsep + env.get("PYTHONPATH", "")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "src.supervised_worker",
            cwd=str(source_dir),
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        worker = WorkerGeneration(generation_id, source_dir, process)
        self._workers[generation_id] = worker
        self._write_status()
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise RuntimeError(f"Worker exited before readiness: {generation_id}")
            if worker.ready:
                break
            await asyncio.sleep(0.05)
        if not worker.ready:
            process.terminate()
            raise TimeoutError(f"Worker readiness timed out: {generation_id}")
        self._switch_active(worker)

    def _switch_active(self, worker: WorkerGeneration) -> None:
        """Atomically select one ready worker for new, unpinned requests."""
        for workspace_id, terminal_id in self._terminal_ids.items():
            if not self._terminal_owners.get(terminal_id):
                self._pinned_workspaces[workspace_id] = worker.generation_id
        old = self._workers.get(self._active_generation or "")
        if old is not None:
            old.active = False
        worker.active = True
        self._active_generation = worker.generation_id
        self._write_status()
        logger.info("worker_generation_activated", generation=worker.generation_id)

    async def _reap_drained_workers(self) -> None:
        """Stop only generations with no in-flight callbacks or pinned sessions."""
        for workspace_id in list(self._pinned_workspaces):
            if not self._workspace_has_owned_sessions(workspace_id) and not any(
                value == workspace_id for value in self._request_workspaces.values()
            ):
                self._pinned_workspaces.pop(workspace_id, None)
        owned = self._owned_generations()
        for generation_id, worker in list(self._workers.items()):
            if worker.active or worker.in_flight or generation_id in owned:
                continue
            if worker.process.returncode is None:
                worker.process.terminate()
                await worker.process.wait()
                if worker.peer is not None:
                    await worker.peer.close()
                logger.info("worker_generation_drained", generation=generation_id)
        self._write_status()

    async def _accept_worker(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Accept IPC only from the launched same-UID process with secret proof."""
        peer: IPCPeer | None = None
        generation_id: str | None = None
        try:
            pid, uid = peer_uid(writer)
            auth = await asyncio.wait_for(_read_frame(reader), 5)
            generation_id = str(auth.get("generation", ""))
            worker = self._workers.get(generation_id)
            if (
                uid != os.getuid()
                or auth.get("type") != "auth"
                or not hmac.compare_digest(str(auth.get("token", "")), self._ipc_token)
                or worker is None
                or worker.process.pid != pid
                or int(auth.get("pid", -1)) != pid
            ):
                raise IPCError("Worker authentication rejected")
            if worker.peer is not None:
                await worker.peer.close()
            peer = IPCPeer(
                reader,
                writer,
                request_handler=lambda method, data: self._worker_request(
                    generation_id, method, data
                ),
                event_handler=lambda event, data: self._worker_event(
                    generation_id, event, data
                ),
                close_handler=lambda: self._worker_disconnected(generation_id),
            )
            worker.peer = peer
            await _write_frame(
                writer, asyncio.Lock(), {"type": "auth_ok", "generation": generation_id}
            )
            await peer.start()
            await peer._reader_task
        except (IPCError, OSError, ValueError, TypeError) as exc:
            logger.warning(
                "worker_ipc_rejected", generation=generation_id, error=str(exc)
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        finally:
            worker = self._workers.get(generation_id or "")
            if worker is not None and worker.peer is peer:
                worker.peer = None
                worker.ready = False
                worker.last_error = (
                    "IPC disconnected; side-effect result may be unknown"
                )
                self._write_status()

    async def _worker_event(
        self, generation_id: str, event: str, data: dict[str, Any]
    ) -> None:
        """Record bounded session evidence needed for durable generation routing."""
        if event != "worker.observe" or not isinstance(data, dict):
            logger.warning("unexpected_worker_ipc_event", event=event)
            return
        self._record_session_owner(
            generation_id, str(data.get("event", "")), data.get("data") or {}
        )

    def _worker_disconnected(self, generation_id: str) -> None:
        worker = self._workers.get(generation_id)
        if worker is not None:
            worker.ready = False
            worker.last_error = (
                "IPC disconnected; side effects and sessions remain pinned"
            )
            # Do not retarget sessions on this generation to a new worker. If
            # the child died, the in-memory handle is irrecoverable; retaining
            # its pins is safer than killing/replaying an unknown guest effect.
            self._write_status()

    def _owner_for_event(self, event: str, data: dict[str, Any]) -> str | None:
        """Pin session traffic first, then workspace-local existing ownership."""
        if event == "terminal:attach":
            terminal_id = str(data.get("terminal_id", ""))
            workspace_id = str(data.get("workspace_id", ""))
            owner = self._terminal_handles.get((workspace_id, terminal_id))
            if owner:
                return owner
            owner = self._terminal_owners.get(terminal_id)
            if owner:
                return owner
        if event.startswith("terminal:"):
            terminal_id = str(data.get("terminal_id", ""))
            owner = self._terminal_owners.get(terminal_id)
            if owner:
                return owner
            workspace_id = str(data.get("workspace_id", ""))
            if (
                event == "terminal:attach"
                and self._terminal_ids.get(workspace_id) == terminal_id
            ):
                generation = self._active_generation
                worker = self._workers.get(generation or "")
                return generation if worker is not None and worker.ready else None
            if self._terminal_ids.get(workspace_id) == terminal_id:
                owner = self._pinned_workspaces.get(workspace_id)
                if owner:
                    return owner
        if event.startswith("workspace:stream_"):
            owner = self._stream_owners.get(str(data.get("connection_id", "")))
            if owner:
                return owner
        if event.startswith("desktop:proxy_ws_"):
            owner = self._tunnel_owners.get(str(data.get("tunnel_id", "")))
            if owner:
                return owner
            if event == "desktop:proxy_ws_open":
                owner = self._desktop_workspace_owners.get(
                    str(data.get("workspace_id", ""))
                )
                if owner:
                    return owner
        if event in {"task:stop_desktop", "harness:desktop_action"}:
            workspace = str(data.get("workspace_id", ""))
            if event == "harness:desktop_action":
                pinned = self._live_request_owners.get(
                    (workspace, str(data.get("request_id", "")))
                )
                if pinned:
                    return pinned
                action = data.get("action")
                existing_desktop = self._desktop_workspace_owners.get(workspace)
                if existing_desktop:
                    return existing_desktop
                if action in {
                    "hold",
                    "release",
                    "reserve",
                    "renew",
                    "binding",
                    "lease_status",
                }:
                    return self._active_generation
            owner = self._desktop_workspace_owners.get(workspace)
            if owner:
                return owner
        if event in {
            "desktop:proxy_http_request",
            "desktop:clipboard_read",
            "desktop:clipboard_write",
        }:
            workspace_id = str(data.get("workspace_id", ""))
            owner = self._desktop_workspace_owners.get(workspace_id)
            if owner:
                return owner
            endpoint = data.get("container_ip") or data.get("ip_address")
            if isinstance(endpoint, str) and endpoint:
                for (
                    pinned_workspace,
                    pinned_endpoint,
                ) in self._workspace_endpoints.items():
                    if pinned_endpoint == endpoint:
                        return self._pinned_workspaces.get(pinned_workspace)
        if event == "desktop:proxy_ws_open":
            owner = self._desktop_workspace_owners.get(
                str(data.get("workspace_id", ""))
            )
            if owner:
                return owner
        if event.startswith("harness:process_"):
            owner = self._process_owners.get(
                (str(data.get("workspace_id", "")), str(data.get("process_id", "")))
            )
            if owner:
                return owner
        request_id = str(data.get("request_id", ""))
        if event == "harness:cancel":
            workspace_id = str(data.get("workspace_id", ""))
            owner = self._live_request_owners.get((workspace_id, request_id))
            if owner:
                return owner
            for (
                bound_workspace,
                active_request,
            ), owner in self._live_request_owners.items():
                if active_request == request_id and (
                    not workspace_id or workspace_id == bound_workspace
                ):
                    return owner
        if event in {
            "task:start_terminal",
            "task:start_desktop",
            "task:stop_desktop",
            "desktop:proxy_http_request",
            "desktop:clipboard_read",
            "desktop:clipboard_write",
            "desktop:proxy_ws_open",
            "desktop:proxy_ws_send",
            "desktop:proxy_ws_close",
            "workspace:artifact_ensure",
            "workspace:stream_start",
            "workspace:stream_input",
            "workspace:stream_close",
            "workspace:stream_cancel",
            "harness:exec_stream",
            "harness:exec_wait",
            "harness:read_file",
            "harness:write_file",
            "harness:list",
            "harness:stat",
            "harness:process_start",
            "harness:process_list",
            "harness:process_get",
            "harness:process_stop",
            "harness:process_verify",
            "harness:write_file_start",
            "harness:write_file_chunk",
            "harness:write_file_finish",
            "git:operation",
            "files:list",
            "files:find",
            "files:read",
            "files:upload",
            "files:upload_chunk",
            "files:upload_finish",
            "files:download",
        }:
            workspace = str(data.get("workspace_id", ""))
            owner = self._workspace_owner(workspace)
            if owner:
                return owner
        if request_id and request_id in self._request_owners:
            return self._request_owners[request_id]
        if event.startswith("files:") and request_id:
            upload_owner = self._upload_owners.get(
                (str(data.get("workspace_id", "")), request_id)
            )
            if upload_owner:
                return upload_owner
        if event == "git:operation" and request_id in self._git_owners:
            return self._git_owners[request_id]
        if request_id and request_id in self._run_owners:
            return self._run_owners[request_id]
        if request_id and event.startswith("harness:write_file_"):
            owner = self._upload_owners.get(
                (str(data.get("workspace_id", "")), request_id)
            )
            if owner:
                return owner
        if data.get("task_id") and str(data["task_id"]) in self._task_owners:
            return self._task_owners[str(data["task_id"])]
        if request_id and request_id in self._request_workspaces:
            owner = self._workspace_owner(self._request_workspaces[request_id])
            if owner:
                return owner
        if request_id and request_id in self._git_owners:
            return self._git_owners[request_id]
        if request_id and request_id in self._git_workspaces:
            owner = self._workspace_owner(self._git_workspaces[request_id])
            if owner:
                return owner
        workspace_id = str(data.get("workspace_id", ""))
        owner = self._workspace_owner(workspace_id)
        if owner:
            return owner
        active = self._workers.get(self._active_generation or "")
        return active.generation_id if active and active.ready else None

    def _workspace_owner(self, workspace_id: str) -> str | None:
        """Return existing session owner so service registries never split."""
        if not workspace_id:
            return None
        pinned = self._pinned_workspaces.get(workspace_id)
        if pinned:
            return pinned
        for session_id, owner in self._terminal_owners.items():
            if self._terminal_workspaces.get(session_id) == workspace_id:
                return owner
        for connection_id, owner in self._stream_owners.items():
            if self._stream_workspaces.get(connection_id) == workspace_id:
                return owner
        owner = self._desktop_workspace_owners.get(workspace_id)
        if owner:
            return owner
        for (bound_workspace, _), owner in self._process_owners.items():
            if bound_workspace == workspace_id:
                return owner
        for owner, binding in self._operation_owners.values():
            if str(binding.get("workspace_id", "")) == workspace_id:
                return owner
        return None

    def _workspace_has_owned_sessions(self, workspace_id: str) -> bool:
        """Check all runner-owned session/stream state before releasing affinity."""
        return (
            workspace_id in self._desktop_workspace_owners
            or any(
                value == workspace_id for value in self._terminal_workspaces.values()
            )
            or any(value == workspace_id for value in self._stream_workspaces.values())
            or any(value == workspace_id for value in self._tunnel_workspaces.values())
            or any(
                bound_workspace == workspace_id
                for bound_workspace, _ in self._process_owners
            )
            or any(
                bound_workspace == workspace_id
                for bound_workspace, _ in self._background_owners
            )
            or any(
                bound_workspace == workspace_id
                for bound_workspace, _ in self._live_request_owners
            )
            or any(value == workspace_id for value in self._request_workspaces.values())
            or any(value == workspace_id for value in self._git_workspaces.values())
            or any(
                bound_workspace == workspace_id
                for bound_workspace, _ in self._upload_owners
            )
        )

    def _record_session_owner(
        self, generation_id: str, event: str, data: dict[str, Any]
    ) -> None:
        """Learn stable session ownership from protocol IDs and handler outputs."""
        request_id = str(data.get("request_id", ""))
        if event.endswith("_result") or event in {
            "harness:exec_done",
            "git:operation_result",
        }:
            self._request_owners.pop(request_id, None)
            self._request_workspaces.pop(request_id, None)
            self._live_request_owners.pop(
                (str(data.get("workspace_id", "")), request_id), None
            )
        if event == "terminal:started" and data.get("terminal_id"):
            terminal_id = str(data["terminal_id"])
            workspace_id = str(data.get("workspace_id", ""))
            self._terminal_owners[terminal_id] = generation_id
            self._terminal_workspaces[terminal_id] = workspace_id
            self._terminal_ids[workspace_id] = terminal_id
            if self._journal is not None:
                self._journal.remember_terminal(workspace_id, terminal_id)
            self._terminal_handles[(workspace_id, terminal_id)] = generation_id
            self._pin_workspace(workspace_id, generation_id)
        elif event == "terminal:closed" and data.get("terminal_id"):
            terminal_id = str(data["terminal_id"])
            workspace_id = self._terminal_workspaces.get(terminal_id, "")
            self._terminal_ids.pop(workspace_id, None)
            if self._journal is not None:
                self._journal.forget_terminal(workspace_id, terminal_id)
            self._terminal_owners.pop(terminal_id, None)
            self._terminal_workspaces.pop(terminal_id, None)
            for key in [key for key in self._terminal_handles if key[1] == terminal_id]:
                self._terminal_handles.pop(key, None)
            if workspace_id and not self._workspace_has_owned_sessions(workspace_id):
                self._pinned_workspaces.pop(workspace_id, None)
        elif event == "workspace:stream_output" and data.get("started"):
            connection_id = str(data.get("connection_id", ""))
            workspace_id = str(data.get("workspace_id", ""))
            self._stream_owners[connection_id] = generation_id
            self._stream_workspaces[connection_id] = workspace_id
            self._pinned_workspaces.setdefault(workspace_id, generation_id)
        elif event == "workspace:stream_closed" and data.get("connection_id"):
            connection_id = str(data["connection_id"])
            workspace_id = self._stream_workspaces.get(connection_id, "")
            self._stream_owners.pop(connection_id, None)
            self._stream_workspaces.pop(connection_id, None)
            if workspace_id and not self._workspace_has_owned_sessions(workspace_id):
                self._pinned_workspaces.pop(workspace_id, None)
        elif event == "desktop:proxy_ws_closed" and data.get("tunnel_id"):
            tunnel_id = str(data["tunnel_id"])
            self._tunnel_owners.pop(tunnel_id, None)
            self._tunnel_workspaces.pop(tunnel_id, None)
        elif event == "desktop:proxy_ws_open" and data.get("tunnel_id"):
            tunnel_id = str(data["tunnel_id"])
            self._tunnel_owners[tunnel_id] = generation_id
            self._tunnel_workspaces[tunnel_id] = str(data.get("workspace_id", ""))
        elif event in {
            "harness:desktop_action_result",
            "workspace:artifact_ensure_result",
        }:
            request_id = str(data.get("request_id", ""))
            self._request_owners.pop(request_id, None)
            self._request_workspaces.pop(request_id, None)
        elif event in {
            "harness:process_list_result",
            "harness:process_get_result",
            "harness:process_verify_result",
        }:
            self._live_request_owners.pop(
                (str(data.get("workspace_id", "")), str(data.get("request_id", ""))),
                None,
            )
        elif event in COMMANDS and data.get("task_id"):
            self._task_owners.pop(str(data["task_id"]), None)
        elif event == "harness:process_start_result" and data.get("process_id"):
            workspace_id = str(data.get("workspace_id", ""))
            process_id = str(data["process_id"])
            self._process_owners[(workspace_id, process_id)] = generation_id
            self._background_owners[(workspace_id, process_id)] = generation_id
        elif (
            event == "harness:process_stop_result"
            and data.get("process_id")
            and data.get("stopped")
        ):
            key = (str(data.get("workspace_id", "")), str(data["process_id"]))
            self._process_owners.pop(key, None)
            self._background_owners.pop(key, None)
        elif event in {"workspace:created", "workspace:resumed"} and data.get(
            "workspace_id"
        ):
            self._pin_workspace(str(data["workspace_id"]), generation_id)
            authority = self._authority_payload(data)
            if authority is not None:
                self._pin_run_authority(
                    authority, generation_id, str(data["workspace_id"])
                )
        elif event in {"desktop:started", "desktop:process"}:
            workspace_id = str(data.get("workspace_id", ""))
            if workspace_id:
                self._desktop_workspace_owners[workspace_id] = generation_id
            self._pin_workspace(workspace_id, generation_id)
            endpoint = data.get("container_ip") or data.get("ip_address")
            if isinstance(endpoint, str) and endpoint:
                self._workspace_endpoints[workspace_id] = endpoint
        elif event == "desktop:stopped":
            workspace_id = str(data.get("workspace_id", ""))
            self._desktop_workspace_owners.pop(workspace_id, None)
            if workspace_id and not self._workspace_has_owned_sessions(workspace_id):
                self._pinned_workspaces.pop(workspace_id, None)
        elif event in {"harness:exec_done", "harness:exec_wait_result"}:
            request_id = str(data.get("request_id", ""))
            self._request_owners.pop(request_id, None)
            self._request_workspaces.pop(request_id, None)
        elif event in {"files:upload_result", "git:operation_result"}:
            request_id = str(data.get("request_id", ""))
            workspace_id = str(data.get("workspace_id", ""))
            self._upload_owners.pop((workspace_id, request_id), None)
            self._upload_events.pop((workspace_id, request_id), None)
            self._live_request_owners.pop((workspace_id, request_id), None)
            self._git_owners.pop(request_id, None)
            self._git_workspaces.pop(request_id, None)
        elif event == "harness:write_file_result":
            request_id = str(data.get("request_id", ""))
            workspace_id = str(data.get("workspace_id", ""))
            key = (workspace_id, request_id)
            self._upload_owners.pop(key, None)
            self._upload_events.pop(key, None)
        elif event in {"workspace:removed", "workspace:stopped"}:
            workspace_id = str(data.get("workspace_id", ""))
            if event == "workspace:removed":
                self._release_workspace_pin(workspace_id)
            elif event == "workspace:stopped":
                self._pinned_workspaces.pop(workspace_id, None)
                for run_id, run_workspace in list(self._run_workspaces.items()):
                    if run_workspace == workspace_id:
                        self._run_workspaces.pop(run_id, None)
                        self._run_owners.pop(run_id, None)
                        self._run_roots.pop(run_id, None)
                        self._run_workers.pop(run_id, None)
                        self._run_authorities.pop(run_id, None)

    def _record_dispatch_result(
        self, generation_id: str, event: str, request: dict[str, Any], result: Any
    ) -> None:
        """Pin session/process ownership from ACKed request results."""
        if not isinstance(result, dict):
            return
        if event == "workspace:stream_start" and result.get("ok"):
            connection_id = str(request.get("connection_id", ""))
            self._stream_owners[connection_id] = generation_id
            self._stream_workspaces[connection_id] = str(
                request.get("workspace_id", "")
            )
        elif event == "workspace:stream_close" and result.get("closed"):
            connection_id = str(request.get("connection_id", ""))
            self._stream_owners.pop(connection_id, None)
            self._stream_workspaces.pop(connection_id, None)
        elif event == "desktop:proxy_ws_open" and result.get("ok"):
            tunnel_id = str(request.get("tunnel_id", ""))
            self._tunnel_owners[tunnel_id] = generation_id
            self._tunnel_workspaces[tunnel_id] = str(request.get("workspace_id", ""))
        elif (
            event == "desktop:proxy_http_request"
            and result.get("status") is not None
            or event in {"desktop:clipboard_read", "desktop:clipboard_write"}
            or event == "task:start_desktop"
        ):
            self._desktop_workspace_owners[str(request.get("workspace_id", ""))] = (
                generation_id
            )
        elif event == "task:stop_desktop" and result.get("stopped"):
            self._desktop_workspace_owners.pop(
                str(request.get("workspace_id", "")), None
            )

    def _owned_count(self, generation_id: str) -> int:
        """Count all live route pins for one generation."""
        mappings = (
            self._terminal_owners,
            self._stream_owners,
            self._tunnel_owners,
            self._request_owners,
            self._git_owners,
            self._desktop_workspace_owners,
        )
        route_count = sum(
            owner == generation_id for mapping in mappings for owner in mapping.values()
        ) + sum(owner == generation_id for owner in self._process_owners.values())
        route_count += sum(
            owner == generation_id for owner in self._upload_owners.values()
        )
        return route_count

    def _owned_generations(self) -> set[str]:
        """Return generations pinned to processes, requests, and session handles."""
        owned = (
            set(self._terminal_owners.values())
            | set(self._stream_owners.values())
            | set(self._tunnel_owners.values())
            | set(self._request_owners.values())
            | set(self._live_request_owners.values())
            | set(self._background_owners.values())
            | set(self._git_owners.values())
            | set(self._desktop_workspace_owners.values())
            | set(self._pinned_workspaces.values())
            | {owner for owner, _binding in self._operation_owners.values()}
        )
        owned.update(self._process_owners.values())
        owned.update(self._upload_owners.values())
        owned.update(self._run_owners.values())
        owned.update(self._pending_dispatch.values())
        owned.update(self._task_owners.values())
        owned.update(owner for owner, _binding in self._operation_owners.values())
        return owned

    async def _drain_for_supervisor_shutdown(self) -> None:
        """Do not kill PTYs or streams; wait indefinitely for safe maintenance."""
        while any(
            worker.in_flight or key in self._owned_generations()
            for key, worker in self._workers.items()
        ):
            logger.warning(
                "supervisor_shutdown_waiting_for_drain",
                generations=self._status_payload()["generations"],
            )
            await asyncio.sleep(30)
        if self._sio.connected:
            await self._sio.disconnect()
        for worker in self._workers.values():
            if worker.process.returncode is None:
                worker.process.terminate()
        await asyncio.gather(
            *(worker.process.wait() for worker in self._workers.values()),
            return_exceptions=True,
        )

    def _load_or_create_epoch(self) -> str:
        """Persist desktop epoch across worker generation switches/reconnects."""
        try:
            value = self.epoch_path.read_text(encoding="utf-8").strip()
            return str(uuid.UUID(value))
        except (FileNotFoundError, ValueError):
            value = str(uuid.uuid4())
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self.epoch_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temp.write_text(value + "\n", encoding="utf-8")
            temp.chmod(0o600)
            os.replace(temp, self.epoch_path)
            return value

    def _read_active_generation(self) -> str | None:
        """Read an admin-owned local selection marker, rejecting traversal."""
        try:
            version = self.active_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        if not _GENERATION_RE.fullmatch(version):
            raise ValueError("Invalid active generation marker")
        return version

    def _stage_source(self, source: Path, generation_id: str, *, activate: bool) -> str:
        """Create an immutable version snapshot from local runner source."""
        if not _GENERATION_RE.fullmatch(generation_id):
            raise ValueError("Generation identifier must be a simple safe name")
        source = source.expanduser().resolve()
        package = source / "src"
        if not package.is_dir() or not (package / "supervised_worker.py").is_file():
            raise ValueError(
                "Source must be a runner root containing src/supervised_worker.py"
            )
        if any(path.is_symlink() for path in package.rglob("*")):
            raise ValueError("Generation source cannot contain symlinks")
        self.generations_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.generations_dir / generation_id
        if destination.exists():
            raise FileExistsError(f"Generation {generation_id!r} already exists")
        staging = self.generations_dir / f".{generation_id}.{uuid.uuid4().hex}.staging"
        staging.mkdir(mode=0o700)
        try:
            shutil.copytree(
                package,
                staging / "src",
                copy_function=shutil.copy2,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            digest = self._source_digest(staging / "src")
            manifest = {
                "generation": generation_id,
                "source": str(source),
                "source_sha256": digest,
                "created_at": time.time(),
            }
            (staging / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True) + "\n"
            )
            for path in sorted(staging.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            staging.chmod(0o555)
            staging.rename(destination)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        if activate:
            self._write_activation_marker(generation_id)
        logger.info(
            "worker_generation_staged",
            generation=generation_id,
            source=str(source),
            digest=digest,
        )
        return digest

    @staticmethod
    def _source_digest(source: Path) -> str:
        """Hash every source relative path and file byte."""
        digest = hashlib.sha256()
        for path in sorted(
            item
            for item in source.rglob("*")
            if item.is_file()
            and "__pycache__" not in item.parts
            and item.suffix != ".pyc"
        ):
            digest.update(path.relative_to(source).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def _write_activation_marker(self, generation_id: str) -> None:
        """Atomically request local activation; no network update RPC exists."""
        if not _GENERATION_RE.fullmatch(generation_id):
            raise ValueError("Invalid generation identifier")
        temp = self.active_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp.write_text(generation_id + "\n", encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, self.active_path)

    def _status_payload(self) -> dict[str, Any]:
        """Expose bounded local readiness and drain observability."""
        return {
            "mode": "supervised",
            "active_generation": self._active_generation,
            "generations": [
                {
                    "generation": worker.generation_id,
                    "state": "active"
                    if worker.active
                    else "draining"
                    if worker.process.returncode is None
                    else "stopped",
                    "pid": getattr(worker.process, "pid", None),
                    "ready": worker.ready,
                    "in_flight": worker.in_flight,
                    "owned_sessions": self._owned_count(worker.generation_id),
                    "last_error": worker.last_error,
                    "started_at": worker.started_at,
                }
                for worker in self._workers.values()
            ],
        }

    def _write_status(self, *, stopped: bool = False) -> None:
        """Atomically write local status without secrets or runtime payloads."""
        try:
            payload = self._status_payload()
            if stopped:
                payload["mode"] = "stopped"
            temp = self.status_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            temp.write_text(
                json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8"
            )
            temp.chmod(0o600)
            os.replace(temp, self.status_path)
        except OSError:
            logger.exception("supervisor_status_write_failed")


def stage_generation(
    source: Path, state_dir: str, version: str, *, activate: bool = True
) -> str:
    """Stage immutable runner source with a local, operator-invoked command."""
    supervisor = RunnerSupervisor(RunnerSettings(state_dir=state_dir))
    return supervisor._stage_source(source, version, activate=activate)


def read_supervisor_status(state_dir: str) -> dict[str, Any]:
    """Read supervisor status locally; does not talk to the runner socket."""
    path = Path(state_dir).expanduser() / "supervisor-status.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"mode": "not_running", "generations": []}
