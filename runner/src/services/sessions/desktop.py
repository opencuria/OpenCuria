"""Desktop (KasmVNC/Xvnc) session lifecycle (Step 6a sessions group).

Canonical home for the desktop session management previously living on
``WorkspaceService`` in :mod:`src.service`:

- ``DESKTOP_*`` / ``DEFAULT_DESKTOP_*`` / ``MIN_DESKTOP_*`` /
  ``MAX_DESKTOP_*`` / ``COMPUTER_USE_RECORD_DIR`` constants,
  ``_RUN_ID_RE`` / ``DESKTOP_HOLDER_*`` / ``_SCROLL_BUTTONS`` /
  ``_CLICK_BUTTONS`` sentinels,
- the ``_desktop_sessions`` / ``_desktop_recordings`` state plus the
  ``_desktop_lock_map`` keyed lock map (never-evict semantics), and
- the start / acquire / release / action / clipboard operations
  (``desktop_action`` is a thin dispatcher plus one private
  ``_desktop_action_<action>`` helper per branch — Step 6b split,
  same order/checks/messages as the verbatim Step 6a method).

``WorkspaceService`` keeps thin delegates (same names/signatures/
messages) plus ``_desktop_sessions`` / ``_desktop_recordings`` /
``_desktop_locks`` / ``_desktop_locks_guard`` property aliases onto the
manager-owned stores, and a ``desktop`` property exposing the manager,
so existing callers and tests keep working.

Workspace resolution is injected so this module never imports
``src.service`` (no dependency cycle):

- ``runtimes``: runtime backends by type (kept for introspection;
  resolution itself goes through the callables below).
- ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
  ``ValueError("... not found")`` for unknown ids.
- ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
  ``RuntimeError`` for unknown runtimes.
- ``exec_harness_command``: async
  ``(workspace_id, command, workdir, env) -> (exit_code, stdout,
  stderr)`` used by ``desktop_action("execute")``; ``WorkspaceService``
  passes its bound ``exec_harness_command`` facade (which delegates to
  the harness manager).

``DesktopSession`` / ``DesktopReleaseResult`` are imported from
``src.models`` (identical objects, never redefined). Path validation
uses the canonical ``src.services.exec_kernel.sanitize_path``; xdotool
key helpers come from :mod:`src.services.sessions.xdotool`.

Extraction owner: Step 6a (sessions group, PART 1: verbatim move);
Step 6b splits ``desktop_action`` into dispatcher + per-action helpers.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shlex
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import structlog

from ...models import DesktopReleaseResult, DesktopSession, WorkspaceInfo
from ...runtime.managed_process import validate_guest_token
from ...runtime.base import RuntimeBackend
from ..capture_fence import CaptureFence, live_interaction
from ..exec_kernel import KeyedLockMap
from ..exec_kernel import sanitize_path as _sanitize_path
from .desktop_leases import DesktopLeaseStore
from .xdotool import (
    _normalize_xdotool_key_combo,
    _xdotool_key_failed,
    _xdotool_type_command,
)

logger = structlog.get_logger(__name__)

DESKTOP_DISPLAY = ":1"
DESKTOP_HOME = "/root"
#: Marker file written by the runner once a workspace X11 client
#: environment is known to accept connections (currently an empty
#: ``.Xauthority`` is sufficient: Xvnc uses ``-SecurityTypes None`` but
#: python-Xlib unconditionally opens ``$XAUTHORITY``/``~/.Xauthority``,
#: so every X11 client — including PyAutoGUI — requires the file to
#: exist; without it every ``execute`` snippet fails before connecting).
DESKTOP_XAUTHORITY_PATH = "/root/.Xauthority"
DEFAULT_DESKTOP_WIDTH = 1920
DEFAULT_DESKTOP_HEIGHT = 1080
MIN_DESKTOP_WIDTH = 800
MAX_DESKTOP_WIDTH = 3840
MIN_DESKTOP_HEIGHT = 600
MAX_DESKTOP_HEIGHT = 2160
COMPUTER_USE_RECORD_DIR = "/workspace/.opencuria/computeruse"
#: Max accepted ``desktop_action("execute")`` code payload (chars).
#: Mirrors the backend ``ACTION_EXECUTE_MAX_CHARS`` guard.
DESKTOP_EXECUTE_MAX_CHARS = 200_000
#: Timeout (seconds) for one remote ``desktop_action("execute")`` snippet.
DESKTOP_EXECUTE_TIMEOUT_S = 120.0
_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
DESKTOP_HOLDER_VIEWER = "viewer"
DESKTOP_HOLDER_COMPUTERUSE = "computeruse"
DESKTOP_STOP_SCRIPT = "/usr/local/bin/opencuria-desktop-stop"
#: Exit 0 once Xvnc is gone (bounded ~5 s wait after SIGTERM). Older guest
#: stop scripts kill every ``pgrep -f 'Xvnc.*:1'`` match, so neither this
#: argv nor the stop invocation may contain a literal ``Xvnc``; the
#: bracketed first letters keep the regex while hiding the literal name.
DESKTOP_STOPPED_PROBE = (
    "i=0; while [ \"$i\" -lt 20 ]; do "
    "pgrep -f '^(/usr/bin/)?X[v]nc :1|^(/usr/bin/)?X[t]igervnc :1' "
    ">/dev/null || exit 0; i=$((i+1)); sleep 0.25; done; exit 1"
)
_SCROLL_BUTTONS = {
    "up": 4,
    "down": 5,
    "left": 6,
    "right": 7,
}
_CLICK_BUTTONS = {
    "left": 1,
    "middle": 2,
    "right": 3,
    1: 1,
    2: 2,
    3: 3,
}


@dataclass(frozen=True)
class WorkspaceEndedEvidence:
    """Exact physical identity confirmed by a successful lifecycle operation."""

    workspace_id: uuid.UUID
    instance_id: str
    runtime_type: str
    operation: str


class DesktopManager:
    """Owns the shared Xvnc desktop process, leases and recordings.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids.
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes.
    - ``exec_harness_command``: async ``(workspace_id, command, workdir,
      env) -> (exit_code, stdout, stderr)`` used by
      ``desktop_action("execute")``.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        exec_harness_command: (
            Callable[
                [uuid.UUID, list[str] | str, str, dict[str, str] | None],
                Awaitable[tuple[int, str, str]],
            ]
            | None
        ) = None,
        lease_store: DesktopLeaseStore | None = None,
        epoch: str | None = None,
        close_owner_streams: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._legacy_mode = lease_store is None
        self._lease_tempdir = (
            tempfile.TemporaryDirectory() if lease_store is None else None
        )
        self.lease_store = lease_store or DesktopLeaseStore(self._lease_tempdir.name)
        self.epoch = epoch or str(uuid.uuid4())

        async def no_streams(lease_id: str) -> bool:
            return self._legacy_mode

        self.close_owner_streams = close_owner_streams or no_streams
        self.capture_fence: CaptureFence | None = None
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._exec_harness_command = exec_harness_command
        self._desktop_sessions: dict[uuid.UUID, DesktopSession] = {}
        self._maintenance_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._maintenance_offsets: dict[uuid.UUID, int] = {}
        self._recording_waiters: dict[str, asyncio.Task] = {}
        self._recording_handles: dict[str, tuple[Any, Any]] = {}
        self._desktop_recordings: dict[tuple[uuid.UUID, str], tuple[int, str]] = {}
        # Serialises concurrent viewer/computer-use lifecycle ops per
        # workspace (same never-evict rationale as the background and
        # git lock maps: dropping a lock object while a holder waits
        # would hand the next caller a different lock). Step 5: the
        # get-or-create never-evict mechanics live in the canonical
        # ``KeyedLockMap`` (``src.services.exec_kernel``);
        # ``_desktop_locks`` / ``_desktop_locks_guard`` stay
        # readable/writable as live aliases onto the map's dict/guard
        # so ``WorkspaceService`` property aliases and tests poking
        # ``service._desktop_locks.get(...)`` keep working.
        self._desktop_lock_map: KeyedLockMap = KeyedLockMap()

    async def workspace_ended(
        self,
        workspace_id: uuid.UUID,
        instance_id: str,
        runtime_type: str,
        evidence: WorkspaceEndedEvidence,
    ) -> None:
        """Finalize only proven-dead physical owners, without guest execution.

        Lifecycle must confirm streams first, before losing registry metadata.
        Retain tombstones: the same logical workspace/viewer UUID can return.
        This internal coordinated operation deliberately has no capture decorator.
        """
        if (
            not isinstance(evidence, WorkspaceEndedEvidence)
            or evidence.workspace_id != workspace_id
            or evidence.instance_id != instance_id
            or evidence.runtime_type != runtime_type
            or evidence.operation not in {"stop", "remove"}
            or not instance_id
        ):
            raise ValueError("Invalid physical workspace-ended evidence")
        info = self._get_cached(workspace_id)
        if info.instance_id != instance_id or info.runtime_type != runtime_type:
            raise ValueError("Workspace-ended evidence does not match registry")
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            rows = await self.lease_store.list_unfinished(str(workspace_id))
            for recording in await self.lease_store.unfinished_recordings(
                str(workspace_id)
            ):
                if recording["instance_id"] != instance_id:
                    continue
                waiter = self._recording_waiters.pop(recording["recording_id"], None)
                if waiter:
                    waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                # Detach local transport only; never signal or exec a guest
                # after the exact physical disposal has been confirmed.
                cached = self._recording_handles.pop(recording["recording_id"], None)
                if cached:
                    try:
                        await cached[0].process_detach(cached[1])
                    except Exception:
                        logger.exception(
                            "recording_local_detach_failed",
                            recording_id=recording["recording_id"],
                        )
                await self.lease_store.recording_state(
                    recording["recording_id"], "closed"
                )
            for row in rows:
                if row["instance_id"] == instance_id:
                    await self.lease_store.mark_closing(row["lease_id"])
                    await self.lease_store.finish(row["lease_id"], "expired")
            session = self._desktop_sessions.get(workspace_id)
            if session and session.instance_id == instance_id:
                self._desktop_sessions.pop(workspace_id, None)
            self._desktop_recordings = {
                k: v
                for k, v in self._desktop_recordings.items()
                if k[0] != workspace_id
            }

    def configure_leases(
        self,
        lease_store: DesktopLeaseStore,
        epoch: str,
        close_owner_streams: Callable[[str], Awaitable[bool]],
    ) -> None:
        """Configure production durability before accepting workspace requests."""
        if self._desktop_sessions or self._recording_handles:
            raise RuntimeError("Desktop lease configuration requires idle manager")
        self.lease_store = lease_store
        self.epoch = epoch
        self.close_owner_streams = close_owner_streams
        self._legacy_mode = False

    async def _recording_owner(
        self, workspace_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict:
        run_id = self._sanitize_run_id(str(payload.get("run_id", "")))
        rows = await self.lease_store.list_workspace(str(workspace_id))
        matches = [
            r
            for r in rows
            if r["kind"] == "computeruse"
            and r["owner_id"] == run_id
            and (not payload.get("lease_id") or r["lease_id"] == payload["lease_id"])
        ]
        if len(matches) != 1:
            raise ValueError("Recording requires an unambiguous computeruse lease")
        return matches[0]

    async def _managed_record_start(
        self, workspace_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            await self._check_unfinished_incarnations(workspace_id)
            owner = await self._recording_owner(workspace_id, payload)
            if (
                owner["epoch"] != self.epoch
                or owner["state"] not in {"reserved", "held"}
                or owner["expires_at"] <= time.time()
            ):
                raise ValueError("Recording owner has ended")
            existing = await self.lease_store.recordings(owner["lease_id"])
            if existing:
                row = existing[0]
                if row["state"] != "starting":
                    raise ValueError("Recording attempt ended")
                # ACK loss is idempotent, never a second guest launch.
                return {"ok": True, "path": row["path"], "run_id": owner["owner_id"]}
            record_path = _sanitize_path(
                str(
                    payload.get("path")
                    or f"{COMPUTER_USE_RECORD_DIR}/{owner['owner_id']}/session.mp4"
                )
            )
            width, height = await self._get_desktop_geometry(workspace_id)
            runtime = self._get_runtime(workspace_id)
            guest_token = validate_guest_token(
                await runtime.probe_managed_token(owner["instance_id"])
            )
            # The probe awaits external I/O: durable ownership may have ended
            # even though this manager still holds the desktop serialization lock.
            fresh = await self.lease_store.get(owner["lease_id"])
            if (
                not fresh
                or fresh["epoch"] != self.epoch
                or fresh["state"] not in {"reserved", "held"}
                or fresh["expires_at"] <= time.time()
                or fresh["instance_id"] != owner["instance_id"]
                or self._get_cached(workspace_id).instance_id != owner["instance_id"]
            ):
                raise ValueError("Recording owner ended during guest token probe")
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            recording_id = str(uuid.uuid4())
            control_path = f"/workspace/.opencuria/managed-recordings/{recording_id}"
            row = dict(
                recording_id=recording_id,
                lease_id=owner["lease_id"],
                workspace_id=str(workspace_id),
                instance_id=owner["instance_id"],
                owner_id=owner["owner_id"],
                epoch=self.epoch,
                control_path=control_path,
                path=record_path,
                state="starting",
                guest_token=guest_token,
            )
            await self.lease_store.reserve_recording(row)
            runtime = self._get_runtime(workspace_id)
            # Redirect both streams rather than creating growing runner log buffers.
            # exec preserves the supervisor-anchored process group.
            command = (
                f"mkdir -p {shlex.quote(os.path.dirname(record_path))} && "
                f"exec ffmpeg -nostdin -hide_banner -loglevel error -y "
                f"-f x11grab -video_size {width}x{height} -framerate 10 "
                f"-draw_mouse 1 -i {DESKTOP_DISPLAY} -c:v libx264 "
                f"-preset ultrafast -pix_fmt yuv420p {shlex.quote(record_path)} "
                "</dev/null >/dev/null 2>&1"
            )
            try:
                handle = await runtime.spawn_process(
                    owner["instance_id"],
                    ["sh", "-lc", command],
                    workdir="/workspace",
                    env=self._desktop_env(),
                    control_path=control_path,
                    expected_token=guest_token,
                )
                self._recording_handles[recording_id] = (runtime, handle)
                self._recording_waiters[recording_id] = asyncio.create_task(
                    self._wait_recording_exit(runtime, handle)
                )
            except BaseException:
                await asyncio.shield(
                    self.lease_store.recording_state(recording_id, "closing")
                )
                raise
            return {"ok": True, "path": record_path, "run_id": owner["owner_id"]}

    async def _wait_recording_exit(self, runtime: Any, handle: Any) -> int:
        """Monitor without mistaking unknown/transport status for guest exit."""
        while True:
            code = await runtime.process_wait(handle)
            if code is not None:
                return code
            await asyncio.sleep(15)

    async def _cleanup_lease_recordings(self, lease_id: str) -> bool:
        """Fence late recorder launches and verify anchored groups, even after crash."""
        closed = True
        for row in await self.lease_store.recordings(lease_id):
            if row["state"] == "closed":
                continue
            await self.lease_store.recording_state(row["recording_id"], "closing")
            try:
                runtime = self._get_runtime(uuid.UUID(row["workspace_id"]))
                proven_stopped = await self._instance_proven_stopped(
                    uuid.UUID(row["workspace_id"]), row["instance_id"]
                )
                if (
                    not proven_stopped
                    and await runtime.close_managed_process(
                        row["instance_id"], row["control_path"]
                    )
                    is not True
                ):
                    closed = False
                    continue
                cached = self._recording_handles.get(row["recording_id"])
                if cached and not proven_stopped:
                    # The durable guest close above is authoritative. Also release
                    # local pipes; transport failure cannot erase the guest fence.
                    try:
                        await cached[0].process_close(cached[1])
                    except Exception:
                        logger.warning(
                            "recording_transport_cleanup_failed",
                            recording_id=row["recording_id"],
                        )
                    self._recording_handles.pop(row["recording_id"], None)
                self._recording_handles.pop(row["recording_id"], None)
                waiter = self._recording_waiters.pop(row["recording_id"], None)
                if waiter:
                    if not waiter.done():
                        waiter.cancel()
                    await asyncio.gather(waiter, return_exceptions=True)
                await self.lease_store.recording_state(row["recording_id"], "closed")
            except Exception:
                closed = False
                logger.exception(
                    "recording_group_cleanup_unverified",
                    recording_id=row["recording_id"],
                )
        return closed

    async def _managed_record_stop(
        self, workspace_id: uuid.UUID, payload: dict[str, Any]
    ) -> dict[str, Any]:
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            owner = await self._recording_owner(workspace_id, payload)
            rows = await self.lease_store.recordings(owner["lease_id"])
            if not rows:
                # stop-before-start is also an intent fence, not merely a read.
                # A later delayed start for this attempt must not launch ffmpeg.
                recording_id = str(uuid.uuid4())
                row = dict(
                    recording_id=recording_id,
                    lease_id=owner["lease_id"],
                    workspace_id=str(workspace_id),
                    instance_id=owner["instance_id"],
                    owner_id=owner["owner_id"],
                    epoch=owner["epoch"],
                    control_path=f"/workspace/.opencuria/managed-recordings/{recording_id}",
                    path=f"{COMPUTER_USE_RECORD_DIR}/{owner['owner_id']}/session.mp4",
                    state="closing",
                )
                await self.lease_store.reserve_recording(row)
                rows = [row]
            for row in rows:
                await self.lease_store.recording_state(row["recording_id"], "closing")
        if not await self._cleanup_lease_recordings(owner["lease_id"]):
            raise RuntimeError("Recording termination unverified")
        return {"ok": True, "path": rows[0]["path"]}

    def binding(self) -> dict[str, Any]:
        """Return the runner-owned X11 binding without activating a display."""
        return {
            "ok": True,
            "display": DESKTOP_DISPLAY,
            "xauthority": DESKTOP_XAUTHORITY_PATH,
            "epoch": self.epoch,
            "protocol_version": 1,
            "lease_ttl_seconds": 180,
            "renew_interval_seconds": 45,
        }

    async def _lease_projection(self, workspace_id: uuid.UUID) -> dict[str, Any]:
        rows = await self.lease_store.list_unfinished(str(workspace_id))
        held = [row for row in rows if row["activated"]]
        session = self._desktop_sessions.get(workspace_id)
        viewer = any(row["kind"] == "viewer" for row in held)
        runs = {row["owner_id"] for row in held if row["kind"] == "computeruse"}
        mcp = any(row["kind"] == "mcp" for row in held)
        if session is not None:
            session.viewer_held = viewer
            session.computeruse_run_ids = runs
            session.mcp_active = mcp
            session.holder_count = len(held)
        return {
            "viewer": viewer,
            "viewer_held": viewer,
            "computer_use": bool(runs),
            "computer_use_active": bool(runs),
            "mcp": mcp,
            "mcp_active": mcp,
            "holder_count": len(held),
            "active": session is not None,
        }

    async def _verify_binding(self, workspace_id: uuid.UUID) -> None:
        # XOpenDisplay performs an actual authenticated X11 handshake. ctypes
        # uses the guest's libX11, with no Python package dependency.
        code = (
            "import ctypes,ctypes.util,socket; "
            "x=ctypes.CDLL(ctypes.util.find_library('X11') or 'libX11.so.6'); "
            "x.XOpenDisplay.argtypes=[ctypes.c_char_p]; "
            "x.XOpenDisplay.restype=ctypes.c_void_p; "
            "x.XCloseDisplay.argtypes=[ctypes.c_void_p]; "
            "d=x.XOpenDisplay(b':1'); assert d, 'X11 binding unavailable'; "
            "x.XCloseDisplay(d); "
            "s=socket.create_connection(('127.0.0.1',6901),2); s.close()"
        )
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        rc, output = await runtime.exec_command_wait(
            info.instance_id, ["python3", "-c", code], env=self._desktop_env()
        )
        if rc != 0:
            raise RuntimeError(f"Desktop binding verification failed: {output}")

    async def _check_unfinished_incarnations(self, workspace_id: uuid.UUID) -> None:
        """Fence fresh work until old runtime/runner owners are verified closed.

        Caller holds the desktop lock; this reads only durable metadata.
        """
        instance_id = self._get_cached(workspace_id).instance_id
        rows = await self.lease_store.list_unfinished(str(workspace_id))
        if any(
            r["state"] not in {"released", "expired"}
            and (r["epoch"] != self.epoch or r["instance_id"] != instance_id)
            for r in rows
        ):
            raise RuntimeError("Previous desktop incarnation cleanup pending; retry")
        recordings = await self.lease_store.unfinished_recordings(str(workspace_id))
        if any(
            r["workspace_id"] == str(workspace_id)
            and r["state"] != "closed"
            and (r["epoch"] != self.epoch or r["instance_id"] != instance_id)
            for r in recordings
        ):
            raise RuntimeError("Previous recording incarnation cleanup pending; retry")

    async def _instance_proven_stopped(
        self, workspace_id: uuid.UUID, instance_id: str
    ) -> bool:
        """Only explicit runtime status proves guest effects no longer exist."""
        try:
            status = await self._get_runtime(workspace_id).get_workspace_status(
                instance_id
            )
            return status.instance_id == instance_id and status.status in {
                "stopped",
                "exited",
                "dead",
                "removed",
            }
        except Exception:
            return False

    async def _lease_action(
        self, workspace_id: uuid.UUID, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if action == "binding":
            self._get_cached(workspace_id)
            return self.binding()
        lease_id = str(payload.get("lease_id") or "")
        if not lease_id:
            raise ValueError("lease_id is required")
        lock = await self._desktop_lock(workspace_id)
        if action == "release":
            return await self._release_lease(workspace_id, payload)
        async with lock:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            row = await self.lease_store.get(lease_id)
            if row and row["workspace_id"] != str(workspace_id):
                raise ValueError("Desktop lease workspace mismatch")
            if action == "lease_status":
                return {
                    **self.binding(),
                    **await self._lease_projection(workspace_id),
                    "lease_state": row["state"] if row else "unknown",
                    "revision": row["revision"] if row else None,
                }
            epoch = str(payload.get("epoch") or "")
            revision = payload.get("revision", 1)
            if epoch != self.epoch:
                raise ValueError("Stale runner epoch")
            if action == "renew":
                row = await self.lease_store.renew(lease_id, epoch, revision, 180)
            else:
                await self._check_unfinished_incarnations(workspace_id)
                info = self._get_cached(workspace_id)
                row = await self.lease_store.reserve(
                    str(workspace_id),
                    info.instance_id,
                    lease_id,
                    str(payload.get("kind") or ""),
                    str(payload.get("owner_id") or ""),
                    epoch,
                    revision,
                    180,
                )
                if action == "hold":
                    # Persist activation BEFORE guest effects: cancellation or
                    # failed start must leave a protective owner for recovery.
                    row = await self.lease_store.activate(lease_id, epoch, revision)
                    try:
                        await self._ensure_desktop_process_locked(
                            workspace_id,
                            width=payload.get("desktop_width"),
                            height=payload.get("desktop_height"),
                        )
                        await self._verify_binding(workspace_id)
                    except BaseException:
                        await asyncio.shield(self.lease_store.mark_closing(lease_id))
                        raise
            return {
                **self.binding(),
                **await self._lease_projection(workspace_id),
                "lease_state": row["state"],
                "revision": row["revision"],
            }

    @live_interaction
    async def _release_lease(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
        *,
        terminal_state: str = "released",
    ) -> dict[str, Any]:
        lease_id = str(payload["lease_id"])
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            row = await self.lease_store.get(lease_id)
            if row is None:
                # A release arriving before a delayed reserve is still an
                # authoritative tombstone for that identity/revision.
                info = self._get_cached(workspace_id)
                row = await self.lease_store.reserve(
                    str(workspace_id),
                    info.instance_id,
                    lease_id,
                    str(payload.get("kind") or ""),
                    str(payload.get("owner_id") or ""),
                    str(payload.get("epoch") or self.epoch),
                    payload.get("revision", 1),
                    180,
                )
            if row["workspace_id"] != str(workspace_id):
                raise ValueError("Desktop lease workspace mismatch")
            for key in ("kind", "owner_id"):
                if key in payload and payload[key] != row[key]:
                    raise ValueError("Desktop lease identity mismatch")
            requested_revision = payload.get("revision", row["revision"])
            if requested_revision != row["revision"]:
                if (
                    row["kind"] != "viewer"
                    or not isinstance(requested_revision, int)
                    or requested_revision <= row["revision"]
                ):
                    raise ValueError("Stale desktop lease revision")
                row = await self.lease_store.reserve(
                    row["workspace_id"],
                    row["instance_id"],
                    lease_id,
                    row["kind"],
                    row["owner_id"],
                    row["epoch"],
                    requested_revision,
                    180,
                )
            row = await self.lease_store.mark_closing(lease_id)
            if (
                row["kind"] != "agent"
                and row["activated"]
                and not self._legacy_mode
                and workspace_id not in self._desktop_sessions
                and row["instance_id"] == self._get_cached(workspace_id).instance_id
            ):
                # A protective owner is sufficient evidence to reconcile its
                # process incarnation. Do not require a successful guest exec
                # probe before runtime stopped-status proof can be consulted.
                self._desktop_sessions[workspace_id] = DesktopSession(
                    workspace_id, row["instance_id"]
                )
        # Agent leases own only their process streams; do not inspect runtime
        # status or infer authority over the workspace desktop during release.
        # Stream shutdown can acquire runtime/workspace locks: never await it
        # while holding the desktop lock. Closing forbids holds/renew/spawn.
        proven_stopped = False
        if row["kind"] != "agent":
            proven_stopped = await self._instance_proven_stopped(
                workspace_id, row["instance_id"]
            )
        closed = row["state"] in {"released", "expired"}
        if not closed:
            try:
                closed = await self.close_owner_streams(lease_id) or proven_stopped
                if closed and row["kind"] == "computeruse":
                    closed = await self._cleanup_lease_recordings(lease_id)
                    if closed and self._legacy_mode:
                        closed = await self._close_lease_recording(
                            workspace_id, row["owner_id"]
                        )

            except Exception:
                logger.exception("desktop_owner_cleanup_failed", lease_id=lease_id)
                closed = proven_stopped
                if closed and row["kind"] == "computeruse":
                    closed = await self._cleanup_lease_recordings(lease_id)
        async with lock:
            # Another release may have finished already. Higher viewer revisions
            # cannot replace a closing row until this cleanup completes.
            current = await self.lease_store.get(lease_id)
            if current["revision"] != row["revision"]:
                raise ValueError("Desktop lease changed during cleanup")
            summary = await self._lease_projection(workspace_id)
            stopped = proven_stopped
            session = self._desktop_sessions.get(workspace_id)
            if proven_stopped and session and session.instance_id == row["instance_id"]:
                self._desktop_sessions.pop(workspace_id, None)
                session = None
            current_instance = (
                self._get_cached(workspace_id).instance_id
                if row["kind"] != "agent"
                else row["instance_id"]
            )
            if (
                row["kind"] != "agent"
                and row["instance_id"] != current_instance
                and not proven_stopped
            ):
                closed = False
            if session and session.instance_id != row["instance_id"]:
                session = None  # Never stop a replacement guest's display.
            own_membership = (
                int(bool(row["activated"])) if row["kind"] != "agent" else 0
            )
            if (
                row["kind"] != "agent"
                and closed
                and summary["holder_count"] == own_membership
                and session is not None
            ):
                try:
                    await self._stop_desktop_process(
                        workspace_id,
                        interrupt_recordings=False,
                        expected_session=session,
                    )
                    stopped = self._desktop_sessions.get(workspace_id) is None
                except Exception:
                    closed = False
            if closed:
                await self.lease_store.finish(lease_id, terminal_state)
            summary = await self._lease_projection(workspace_id)
            row = await self.lease_store.get(lease_id)
            return {
                **self.binding(),
                **summary,
                "ok": closed,
                "lease_state": row["state"],
                "stopped": stopped,
                "process_alive": self._desktop_sessions.get(workspace_id) is not None,
            }

    async def _close_lease_recording(
        self, workspace_id: uuid.UUID, owner_id: str
    ) -> bool:
        recording = self._desktop_recordings.get((workspace_id, owner_id))
        if recording is None:
            return True
        pid, _ = recording
        rc, _ = await self._exec_desktop_shell(
            workspace_id,
            f"kill -INT {pid} 2>/dev/null || true; sleep 0.5; "
            f"kill -TERM {pid} 2>/dev/null || true; sleep 0.5; "
            f"! kill -0 {pid} 2>/dev/null",
        )
        if rc != 0:
            return False
        self._desktop_recordings.pop((workspace_id, owner_id), None)
        return True

    async def maintenance_workspace_ids(self) -> list[uuid.UUID]:
        """Return workspaces with unfinished durable owners or recordings."""
        return [
            uuid.UUID(value)
            for value in await self.lease_store.unfinished_workspace_ids()
        ]

    async def maintain_workspace(self, workspace_id: uuid.UUID) -> None:
        """Reconcile one workspace without guest starts or global head-of-line waits.

        Bounded parallel owner cleanup and rotated admission keep a stuck owner
        from starving later owners when the interface's outer timeout cancels a
        tick. Duplicate overlapping ticks are harmless and skipped.
        """
        lock = self._maintenance_locks.setdefault(workspace_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            rows = await self.lease_store.list_unfinished(str(workspace_id))
            expired = {
                r["lease_id"]
                for r in await self.lease_store.list_expired(str(workspace_id))
            }
            if not rows and not await self.lease_store.unfinished_recordings(
                str(workspace_id)
            ):
                return
            instance = self._get_cached(workspace_id).instance_id
            releases = {
                r["lease_id"]: r
                for r in rows
                if r["lease_id"] in expired
                or r["epoch"] != self.epoch
                or r["instance_id"] != instance
            }
            recordings = await self.lease_store.unfinished_recordings(str(workspace_id))
            recording_owners = {
                r["lease_id"]
                for r in recordings
                if r["state"] == "closing"
                or (
                    self._recording_waiters.get(r["recording_id"]) is not None
                    and self._recording_waiters[r["recording_id"]].done()
                )
            }
            jobs = [
                (lease_id, releases.get(lease_id))
                for lease_id in sorted(set(releases) | recording_owners)
            ]
            if jobs:
                offset = self._maintenance_offsets.get(workspace_id, 0) % len(jobs)
                jobs = jobs[offset:] + jobs[:offset]
                self._maintenance_offsets[workspace_id] = offset + 8
                slots = asyncio.Semaphore(8)

                async def cleanup(lease_id: str, row: dict | None) -> None:
                    async with slots:
                        try:
                            operation = (
                                self._release_lease(
                                    workspace_id, row, terminal_state="expired"
                                )
                                if row
                                else self._cleanup_lease_recordings(lease_id)
                            )
                            await asyncio.wait_for(operation, timeout=8)
                        except Exception:
                            logger.exception(
                                "desktop_maintenance_owner_pending", lease_id=lease_id
                            )

                await asyncio.gather(*(cleanup(*job) for job in jobs))
            await self._lease_projection(workspace_id)

    async def reap_expired(self) -> None:
        """Compatibility maintenance entry point, concurrent across workspaces."""
        await self.recover()

    async def recover(self) -> None:
        """Reconcile unfinished ownership concurrently; never resume desktops."""

        async def maintain(workspace_id: uuid.UUID) -> None:
            try:
                await self.maintain_workspace(workspace_id)
            except Exception:
                logger.exception(
                    "desktop_lease_recovery_failed", workspace_id=str(workspace_id)
                )

        await asyncio.gather(
            *(maintain(ws) for ws in await self.maintenance_workspace_ids())
        )

    async def recover_workspace(self, workspace_id: uuid.UUID) -> None:
        """Compatibility alias for scoped maintenance."""
        await self.maintain_workspace(workspace_id)

    @property
    def _desktop_locks(self) -> dict[uuid.UUID, asyncio.Lock]:
        """Alias onto the desktop lock map's underlying dict (live)."""
        return self._desktop_lock_map.locks

    @_desktop_locks.setter
    def _desktop_locks(self, value: dict) -> None:
        self._desktop_lock_map.locks.clear()
        self._desktop_lock_map.locks.update(value)

    @property
    def _desktop_locks_guard(self) -> asyncio.Lock:
        """Alias onto the desktop lock map's guard lock."""
        return self._desktop_lock_map.guard

    @_desktop_locks_guard.setter
    def _desktop_locks_guard(self, value: asyncio.Lock) -> None:
        self._desktop_lock_map.guard = value

    async def _desktop_lock(self, workspace_id: uuid.UUID) -> asyncio.Lock:
        """Return the serialising lock for one workspace desktop lifecycle.

        The entry is created once and retained for the lifetime of the
        runner process (bounded by ever-seen workspaces; cleared on
        restart). It is never dropped: dropping after release cannot
        observe queued waiters via the public ``asyncio.Lock`` API —
        ``release()`` only schedules the first waiter's wakeup, and the
        waiter sets its locked state later — so a drop in that window
        hands a third caller a different lock object while the woken
        waiter still references the old one, silently breaking
        serialisation. One small in-memory ``asyncio.Lock`` per workspace
        is the accepted trade-off for correctness.

        Step 5: mechanics live in the canonical ``KeyedLockMap``
        (``src.services.exec_kernel``); this stays a thin delegate so
        behavior (including the never-evict guarantee) is unchanged.
        """
        return await self._desktop_lock_map.get(workspace_id)

    async def _is_desktop_session_live(self, workspace_id: uuid.UUID) -> bool:
        """Return whether the cached desktop session still accepts connections."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        exit_code, _ = await runtime.exec_command_wait(
            info.instance_id,
            [
                "sh",
                "-lc",
                (
                    "if command -v python3 >/dev/null 2>&1; then "
                    'python3 -c "import socket,sys; '
                    "sock=socket.socket(); sock.settimeout(1); "
                    "rc=sock.connect_ex(('127.0.0.1',6901)); sock.close(); "
                    'sys.exit(0 if rc == 0 else 1)"; '
                    "else "
                    "pgrep -f '^(/usr/bin/)?Xvnc :1|^(/usr/bin/)?Xtigervnc :1' "
                    ">/dev/null; "
                    "fi"
                ),
            ],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        return exit_code == 0

    def _desktop_heartbeat_payload(
        self,
        workspace_id: uuid.UUID,
        session: DesktopSession,
    ) -> dict[str, Any]:
        """Return heartbeat fields for a live desktop session."""
        return {
            "port": session.port,
            "container_ip": self.get_desktop_container_ip(workspace_id),
            "network_name": self.get_desktop_network_name(workspace_id),
            "viewer": session.viewer_held,
            "computer_use": bool(session.computeruse_run_ids),
            "mcp": session.mcp_active,
            "holder_count": session.holder_count,
        }

    @staticmethod
    def _parse_desktop_holder(holder: str) -> str:
        """Validate a desktop lease holder kind."""
        value = (holder or "").strip().lower()
        if value not in {DESKTOP_HOLDER_VIEWER, DESKTOP_HOLDER_COMPUTERUSE}:
            raise ValueError(f"Invalid desktop holder: {holder}")
        return value

    def _empty_desktop_release_result(self) -> DesktopReleaseResult:
        """Return a release result when no desktop process is tracked."""
        return DesktopReleaseResult(
            stopped=False,
            process_alive=False,
            viewer_held=False,
            computer_use_active=False,
        )

    def _desktop_release_result(
        self,
        session: DesktopSession | None,
        *,
        stopped: bool,
    ) -> DesktopReleaseResult:
        """Build a release result from the current session cache."""
        if session is None:
            return DesktopReleaseResult(
                stopped=stopped,
                process_alive=not stopped,
                viewer_held=False,
                computer_use_active=False,
            )
        return DesktopReleaseResult(
            stopped=stopped,
            process_alive=not stopped,
            viewer_held=session.viewer_held,
            computer_use_active=bool(session.computeruse_run_ids),
        )

    @staticmethod
    def _resolve_desktop_geometry(
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[int, int]:
        """Return a sanitized even framebuffer size for Xvnc."""

        def _coerce(value: int | None, default: int, minimum: int, maximum: int) -> int:
            if value is None:
                return default
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            if parsed % 2 != 0:
                parsed -= 1
            return max(minimum, min(maximum, parsed))

        return (
            _coerce(width, DEFAULT_DESKTOP_WIDTH, MIN_DESKTOP_WIDTH, MAX_DESKTOP_WIDTH),
            _coerce(
                height, DEFAULT_DESKTOP_HEIGHT, MIN_DESKTOP_HEIGHT, MAX_DESKTOP_HEIGHT
            ),
        )

    @staticmethod
    def _desktop_start_command(width: int, height: int) -> str:
        """Return the shell used to start Xvnc at a fixed geometry.

        Must not call ``opencuria-desktop-stop``. That script uses
        ``pgrep -f 'Xvnc.*:1'``, which matches this ``bash -lc`` argv and
        would kill the start process before Xvnc is launched.

        Readiness requires the X11 socket *and* the Kasm websocket port
        6901 (dependency-free ``/dev/tcp`` poll): ``xstartup`` launches
        exactly once as soon as the X11 socket exists, and the loop only
        returns once 6901 is additionally reachable so ensure never
        reports a half-ready Xvnc. The ``.xstartup-started`` marker is
        per-start: it is cleared before Xvnc launches so every restart
        re-runs ``xstartup`` even though the stop path never executes
        (the stop script would match this shell's own ``Xvnc`` argv).
        """
        geometry = f"{width}x{height}"
        return (
            "set -e\n"
            "export DISPLAY=:1\n"
            "export HOME=/root\n"
            # Xvnc runs with ``-SecurityTypes None``, but python-Xlib
            # unconditionally opens ``$XAUTHORITY``/``~/.Xauthority`` on
            # connect — every X11 Python client (PyAutoGUI, mouseinfo,
            # pyscreeze, OCR helpers, ...) requires the file to exist.
            # Touch it at start so ``execute`` snippets never die with
            # ``FileNotFoundError: ... '/root/.Xauthority'`` before even
            # connecting (``_desktop_env`` additionally exports
            # ``XAUTHORITY`` explicitly for the same reason).
            "touch /root/.Xauthority\n"
            "mkdir -p /root/.vnc\n"
            # Per-start marker: clearing it here guarantees xstartup runs
            # again after every Xvnc (re)start. It must not persist across
            # starts, otherwise a restarted Xvnc would show an empty desktop.
            "rm -f /root/.vnc/.xstartup-started\n"
            "rm -f /tmp/.X1-lock /tmp/.X11-unix/X1\n"
            f"/usr/bin/Xvnc :1 -geometry {geometry} -depth 24 "
            "-rfbport 5901 -SecurityTypes None -disableBasicAuth "
            "-websocketPort 6901 -httpd /usr/share/kasmvnc/www "
            "-interface 0.0.0.0 -AlwaysShared -AcceptKeyEvents "
            "-AcceptPointerEvents -SendCutText -AcceptCutText "
            "-AcceptSetDesktopSize=0 "
            ">>/root/.vnc/server.log 2>&1 &\n"
            "for _ in $(seq 1 120); do\n"
            # xstartup launches exactly once as soon as the X11 socket
            # exists (desktop does not wait for the 6901 websocket); the
            # loop only returns once 6901 is additionally reachable, so
            # ensure never reports a half-ready Xvnc.
            "  if [ -e /tmp/.X11-unix/X1 ] "
            "&& [ ! -f /root/.vnc/.xstartup-started ]; then\n"
            "    touch /root/.vnc/.xstartup-started\n"
            "    /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &\n"
            "  fi\n"
            "  if [ -e /tmp/.X11-unix/X1 ] "
            "&& (echo >/dev/tcp/127.0.0.1/6901) >/dev/null 2>&1; then\n"
            '    echo "Desktop session started on :1 (ws port 6901)"\n'
            "    exit 0\n"
            "  fi\n"
            "  sleep 0.25\n"
            "done\n"
            'echo "Desktop session failed to start" >&2\n'
            "tail -n 50 /root/.vnc/server.log >&2 || true\n"
            "exit 1\n"
        )

    def get_desktop_state_payload(
        self,
        workspace_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        """Return cache/network fields for desktop lifecycle announcements."""
        session = self._desktop_sessions.get(workspace_id)
        if session is None:
            return None
        return {
            "workspace_id": str(workspace_id),
            "port": session.port,
            "container_ip": self.get_desktop_container_ip(workspace_id),
            "network_name": self.get_desktop_network_name(workspace_id),
            "viewer": session.viewer_held,
            "computer_use": bool(session.computeruse_run_ids),
            "generation": session.generation,
            "mcp": session.mcp_active,
            "holder_count": session.holder_count,
        }

    @live_interaction
    async def ensure_desktop_process(
        self,
        workspace_id: uuid.UUID,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> DesktopSession:
        """Start the shared KasmVNC process without acquiring a lease.

        Idempotent: a live cached or recovered session is reused. Leases on a
        stale cache entry are copied onto the restarted session.

        Serialised per workspace via :meth:`_desktop_lock` so concurrent
        viewer and computer-use acquires single-flight one Xvnc start.
        """
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            return await self._ensure_desktop_process_locked(
                workspace_id, width=width, height=height
            )

    async def _ensure_desktop_process_locked(
        self,
        workspace_id: uuid.UUID,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> DesktopSession:
        """Ensure the desktop process while holding the workspace lock."""
        existing = self._desktop_sessions.get(workspace_id)
        preserved_viewer = existing.viewer_held if existing is not None else False
        preserved_runs = (
            set(existing.computeruse_run_ids) if existing is not None else set()
        )
        preserved_generation = existing.generation if existing is not None else 0
        if existing is not None:
            if await self._is_desktop_session_live(workspace_id):
                logger.info("desktop_already_running", workspace_id=str(workspace_id))
                return existing
            self._desktop_sessions.pop(workspace_id, None)
            logger.warning(
                "desktop_cached_session_stale",
                workspace_id=str(workspace_id),
            )

        if await self._is_desktop_session_live(workspace_id):
            recovered = DesktopSession(
                workspace_id=workspace_id,
                instance_id=self._get_cached(workspace_id).instance_id,
                viewer_held=preserved_viewer,
                computeruse_run_ids=preserved_runs,
                generation=preserved_generation + 1,
            )
            self._desktop_sessions[workspace_id] = recovered
            logger.info(
                "desktop_session_recovered_on_start",
                workspace_id=str(workspace_id),
                generation=recovered.generation,
            )
            return recovered

        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        resolved_width, resolved_height = self._resolve_desktop_geometry(width, height)

        log = logger.bind(workspace_id=str(workspace_id))

        # Stop first as its own exec. The baked stop script matches
        # ``Xvnc.*:1`` in any process argv, so it must not run inside the
        # start command whose command line contains those bytes.
        await runtime.exec_command_wait(
            info.instance_id,
            [
                "bash",
                "-lc",
                "/usr/local/bin/opencuria-desktop-stop >/dev/null 2>&1 || true",
            ],
            env={"HOME": "/root"},
        )

        start_command = self._desktop_start_command(resolved_width, resolved_height)
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            ["bash", "-lc", start_command],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        if exit_code != 0:
            log.error("desktop_start_failed", exit_code=exit_code, output=output)
            raise RuntimeError(f"Failed to start desktop session: {output}")

        session = DesktopSession(
            workspace_id=workspace_id,
            instance_id=info.instance_id,
            viewer_held=preserved_viewer,
            computeruse_run_ids=preserved_runs,
            generation=preserved_generation + 1,
        )
        self._desktop_sessions[workspace_id] = session
        log.info("desktop_started", port=session.port, generation=session.generation)
        return session

    @live_interaction
    async def acquire_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        holder: str,
        run_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> DesktopSession:
        """Ensure the desktop process and acquire a viewer or computer-use lease.

        The whole ensure+lease mutation runs under the per-workspace
        desktop lock (``async with`` serialises every holder: while one
        task holds it, no other acquire/release can touch the cached
        session, so the ``seen`` snapshot below is stable by construction
        and only this holder mutates it). ``release``/``start`` ordering
        is pinned by the same lock — see the serialisation test.
        """
        kind = self._parse_desktop_holder(holder)
        if not self._legacy_mode:
            owner = (
                "legacy-viewer"
                if kind == "viewer"
                else self._sanitize_run_id(str(run_id or ""))
            )
            lease_id = f"legacy:{workspace_id}:{kind}:{owner}"
            old = await self.lease_store.get(lease_id)
            # Compatibility attempts use fresh IDs for run owners; viewers can
            # advance their ordered intent on explicit reacquisition.
            if old and old["state"] in {"released", "expired"} and kind != "viewer":
                lease_id += ":" + str(uuid.uuid4())
            revision = (
                old["revision"] + 1
                if old and kind == "viewer" and old["state"] in {"released", "expired"}
                else (old["revision"] if old else 1)
            )
            await self._lease_action(
                workspace_id,
                "hold",
                {
                    "lease_id": lease_id,
                    "kind": kind,
                    "owner_id": owner,
                    "epoch": self.epoch,
                    "revision": revision,
                    "desktop_width": width,
                    "desktop_height": height,
                },
            )
            return self._desktop_sessions[workspace_id]
        if kind != DESKTOP_HOLDER_VIEWER:
            # Fail fast on invalid run ids before touching shared state.
            self._sanitize_run_id(str(run_id or ""))
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            # ``seen`` cannot change under us: every other acquire/release
            # path takes the same lock, which we currently hold. The merge
            # below only matters when ensure *replaces* the cache entry
            # with a fresh Xvnc incarnation (stale restart/recovery):
            # leases added to the old object before the replacement are
            # carried onto the new one so neither holder loses its lease.
            seen = self._desktop_sessions.get(workspace_id)
            seen_viewer = seen.viewer_held if seen is not None else False
            seen_runs = set(seen.computeruse_run_ids) if seen is not None else set()
            session = await self._ensure_desktop_process_locked(
                workspace_id,
                width=width,
                height=height,
            )
            if session is not seen and seen is not None:
                session.viewer_held = session.viewer_held or seen_viewer
                session.computeruse_run_ids |= seen_runs
            if kind == DESKTOP_HOLDER_VIEWER:
                session.viewer_held = True
            else:
                session.computeruse_run_ids.add(
                    self._sanitize_run_id(str(run_id or ""))
                )
            session.holder_count = int(session.viewer_held) + len(
                session.computeruse_run_ids
            )
            self._desktop_sessions[workspace_id] = session
            logger.info(
                "desktop_lease_acquired",
                workspace_id=str(workspace_id),
                holder=kind,
                run_id=run_id,
                viewer=session.viewer_held,
                computer_use=bool(session.computeruse_run_ids),
            )
            return session

    @live_interaction
    async def release_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        holder: str,
        run_id: str | None = None,
        force: bool = False,
    ) -> DesktopReleaseResult:
        """Drop a desktop lease and stop Xvnc when no holders remain.

        ``force=True`` ignores remaining leases, interrupts recordings, and
        stops the process. Used for workspace stop/remove.

        Runs under the per-workspace desktop lock. Only the session object
        observed while holding the lock may be stopped: when the last
        lease clears, the cached session is compared by identity before
        stopping so a concurrent start cannot have its new process killed.
        """
        kind = self._parse_desktop_holder(holder)
        if not self._legacy_mode:
            rows = await self.lease_store.list_unfinished(str(workspace_id))
            owner = (
                "legacy-viewer"
                if kind == "viewer"
                else self._sanitize_run_id(str(run_id or ""))
            )
            selected = (
                rows
                if force
                else [r for r in rows if r["kind"] == kind and r["owner_id"] == owner]
            )
            for row in selected:
                if row["state"] in {"reserved", "held", "closing"}:
                    await self._release_lease(workspace_id, row)
            summary = await self._lease_projection(workspace_id)
            return DesktopReleaseResult(
                stopped=not summary["active"],
                process_alive=summary["active"],
                viewer_held=summary["viewer_held"],
                computer_use_active=summary["computer_use_active"],
                mcp_active=summary["mcp_active"],
                holder_count=summary["holder_count"],
            )
        if kind != DESKTOP_HOLDER_VIEWER:
            self._sanitize_run_id(str(run_id or ""))
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            if self.capture_fence is not None:
                self.capture_fence.check_current(workspace_id)
            if force:
                session = self._desktop_sessions.get(workspace_id)
                has_recordings = any(
                    key[0] == workspace_id for key in self._desktop_recordings
                )
                if session is None and not has_recordings:
                    return DesktopReleaseResult(
                        stopped=True,
                        process_alive=False,
                        viewer_held=False,
                        computer_use_active=False,
                    )
                await self._stop_desktop_process(
                    workspace_id, interrupt_recordings=True
                )
                stopped_result = DesktopReleaseResult(
                    stopped=True,
                    process_alive=False,
                    viewer_held=False,
                    computer_use_active=False,
                )
            else:
                session = self._desktop_sessions.get(workspace_id)
                if session is None:
                    return self._empty_desktop_release_result()

                if kind == DESKTOP_HOLDER_VIEWER:
                    session.viewer_held = False
                else:
                    session.computeruse_run_ids.discard(
                        self._sanitize_run_id(str(run_id or ""))
                    )

                session.holder_count = int(session.viewer_held) + len(
                    session.computeruse_run_ids
                )
                logger.info(
                    "desktop_lease_released",
                    workspace_id=str(workspace_id),
                    holder=kind,
                    run_id=run_id,
                    viewer=session.viewer_held,
                    computer_use=bool(session.computeruse_run_ids),
                )
                if session.viewer_held or session.computeruse_run_ids:
                    return self._desktop_release_result(session, stopped=False)

                await self._stop_desktop_process(
                    workspace_id,
                    interrupt_recordings=True,
                    expected_session=session,
                )
                if self._desktop_sessions.get(workspace_id) is session:
                    # A concurrent start installed a fresh session while the
                    # stop exec ran: report it instead of claiming "stopped".
                    return self._desktop_release_result(session, stopped=False)
                stopped_result = DesktopReleaseResult(
                    stopped=True,
                    process_alive=False,
                    viewer_held=False,
                    computer_use_active=False,
                )
        # No lock cleanup here: the per-workspace lock object is retained
        # for the process lifetime so queued waiters and later callers
        # always share one serialising lock (see _desktop_lock).
        return stopped_result

    @live_interaction
    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> DesktopSession:
        """Acquire the viewer lease and ensure the desktop process is running."""
        return await self.acquire_desktop(
            workspace_id,
            holder=DESKTOP_HOLDER_VIEWER,
            width=width,
            height=height,
        )

    @live_interaction
    async def stop_desktop(self, workspace_id: uuid.UUID) -> DesktopReleaseResult:
        """Release the viewer lease. Stops Xvnc only when no computer-use hold remains."""
        return await self.release_desktop(workspace_id, holder=DESKTOP_HOLDER_VIEWER)

    async def _interrupt_desktop_recordings(self, workspace_id: uuid.UUID) -> None:
        """Send SIGINT/SIGTERM to ffmpeg recordings for *workspace_id*."""
        recordings = [
            (run_id, pid, path)
            for (ws_id, run_id), (pid, path) in self._desktop_recordings.items()
            if ws_id == workspace_id
        ]
        if not recordings:
            return
        try:
            for run_id, pid, _path in recordings:
                stop_cmd = (
                    f"kill -INT {pid} 2>/dev/null || true; "
                    "sleep 0.5; "
                    f"kill -0 {pid} 2>/dev/null && kill -TERM {pid} 2>/dev/null || true"
                )
                await self._exec_desktop_shell(workspace_id, stop_cmd)
                logger.info(
                    "desktop_recording_interrupted",
                    workspace_id=str(workspace_id),
                    run_id=run_id,
                    pid=pid,
                )
        except Exception:
            logger.exception(
                "desktop_recording_interrupt_failed",
                workspace_id=str(workspace_id),
            )
        self._desktop_recordings = {
            key: value
            for key, value in self._desktop_recordings.items()
            if key[0] != workspace_id
        }

    async def _stop_desktop_process(
        self,
        workspace_id: uuid.UUID,
        *,
        interrupt_recordings: bool,
        expected_session: DesktopSession | None = None,
    ) -> None:
        """Kill Xvnc and drop the cached desktop session.

        When *expected_session* is given, only that exact session object is
        dropped: a concurrent restart installs a new object under the same
        key, and the stop exec must not claim or clear the fresh start.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        if interrupt_recordings:
            await self._interrupt_desktop_recordings(workspace_id)

        if expected_session is not None:
            if self._desktop_sessions.get(workspace_id) is not expected_session:
                log.warning("desktop_stop_skipped_session_replaced")
                return
            if self._legacy_mode:
                self._desktop_sessions.pop(workspace_id, None)
        else:
            if self._legacy_mode:
                self._desktop_sessions.pop(workspace_id, None)
        try:
            runtime = self._get_runtime(workspace_id)
            info = self._get_cached(workspace_id)
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id, [DESKTOP_STOP_SCRIPT]
            )
            if exit_code != 0:
                log.warning("desktop_stop_nonzero", exit_code=exit_code, output=output)
            if not self._legacy_mode:
                # A separate exec: the stop script must never see this argv.
                exit_code, output = await runtime.exec_command_wait(
                    info.instance_id, ["sh", "-lc", DESKTOP_STOPPED_PROBE]
                )
                if exit_code != 0:
                    log.warning(
                        "desktop_stop_unconfirmed", exit_code=exit_code, output=output
                    )
                    raise RuntimeError("Desktop stop was not confirmed")
                self._desktop_sessions.pop(workspace_id, None)
        except Exception:
            log.exception("desktop_stop_failed")
            if not self._legacy_mode:
                raise

        self._desktop_recordings = {
            key: value
            for key, value in self._desktop_recordings.items()
            if key[0] != workspace_id
        }
        log.info("desktop_stopped")

    @staticmethod
    def _desktop_env() -> dict[str, str]:
        """Return environment variables for desktop X11 commands.

        ``XAUTHORITY`` is pinned alongside ``HOME``/``DISPLAY`` so every
        X11 client (xdotool, ffmpeg x11grab, python-Xlib/PyAutoGUI, ...)
        resolves auth through the runner-owned file instead of depending
        on ambient workspace state.
        """
        return {
            "HOME": DESKTOP_HOME,
            "DISPLAY": DESKTOP_DISPLAY,
            "XAUTHORITY": DESKTOP_XAUTHORITY_PATH,
        }

    @staticmethod
    def _sanitize_run_id(run_id: str) -> str:
        """Validate a computer-use recording run identifier."""
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise ValueError(f"Invalid run_id: {run_id}")
        return run_id

    async def _ensure_desktop_xauthority(self, workspace_id: uuid.UUID) -> None:
        """Ensure the X11 client auth file exists inside the workspace.

        Xvnc runs with ``-SecurityTypes None``, but python-Xlib
        unconditionally opens ``$XAUTHORITY``/``~/.Xauthority`` — an empty
        file is sufficient for the auth-less server. Idempotent best
        effort: failures only degrade to the previous behaviour (the
        client raises ``FileNotFoundError``) and must never fail an
        otherwise healthy ``execute``.
        """
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        try:
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                ["sh", "-lc", "touch /root/.Xauthority"],
                env=self._desktop_env(),
            )
            if exit_code != 0:
                logger.warning(
                    "desktop_xauthority_ensure_failed",
                    workspace_id=str(workspace_id),
                    exit_code=exit_code,
                    output=output,
                )
        except Exception:
            logger.exception(
                "desktop_xauthority_ensure_failed",
                workspace_id=str(workspace_id),
            )

    async def _require_desktop_live(self, workspace_id: uuid.UUID) -> None:
        """Raise when the workspace desktop session is not accepting input."""
        if not await self._is_desktop_session_live(workspace_id):
            raise RuntimeError("Desktop session is not active")

    async def _exec_desktop_shell(
        self,
        workspace_id: uuid.UUID,
        command: str,
    ) -> tuple[int, str]:
        """Execute a shell command inside the workspace desktop environment."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        return await runtime.exec_command_wait(
            info.instance_id,
            ["sh", "-lc", command],
            env=self._desktop_env(),
        )

    async def _get_desktop_geometry(
        self,
        workspace_id: uuid.UUID,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[int, int]:
        """Return desktop width and height, optionally overriding query results."""
        if width is not None and height is not None:
            return width, height

        exit_code, output = await self._exec_desktop_shell(
            workspace_id,
            "xdotool getdisplaygeometry 2>/dev/null || echo '1920 1080'",
        )
        if exit_code == 0:
            parts = output.strip().split()
            if len(parts) >= 2:
                try:
                    return int(parts[0]), int(parts[1])
                except ValueError:
                    pass

        return DEFAULT_DESKTOP_WIDTH, DEFAULT_DESKTOP_HEIGHT

    @live_interaction
    async def desktop_action(
        self,
        workspace_id: uuid.UUID,
        action: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a desktop I/O action inside the workspace display.

        Step 6b: thin dispatcher — payload/logging bind, ``execute``
        code-boundary validation, the liveness gate for every action
        except ``ensure``/``hold``/``release``, then one private
        ``_desktop_action_<action>`` helper per branch (same order and
        messages as the verbatim Step 6a method).
        """
        payload = args or {}
        log = logger.bind(workspace_id=str(workspace_id), desktop_action=action)

        if action == "activate":
            if payload.get("kind") == "agent":
                raise ValueError("Agent desktop leases are process ownership only")
            raise ValueError("Desktop lease activation is only supported through hold")
        if action == "hold" and payload.get("kind") == "agent":
            raise ValueError("Agent desktop leases are process ownership only")

        if action in {"binding", "reserve", "renew", "lease_status"} or (
            action in {"hold", "release"} and payload.get("lease_id")
        ):
            return await self._lease_action(workspace_id, action, payload)

        if action == "ensure":
            return await self._desktop_action_ensure(workspace_id, payload)

        if action == "hold":
            return await self._desktop_action_hold(workspace_id, payload)

        if action == "release":
            return await self._desktop_action_release(workspace_id, payload)

        execute_code = self._validate_desktop_execute_code(action, payload)

        if action not in {"ensure", "hold", "release"} and not (
            action == "record_stop" and not self._legacy_mode
        ):
            await self._require_desktop_live(workspace_id)

        if action == "display_info":
            return await self._desktop_action_display_info(workspace_id, payload)

        if action == "screenshot":
            return await self._desktop_action_screenshot(workspace_id, payload, log)

        if action == "move":
            return await self._desktop_action_move(workspace_id, payload)

        if action == "click":
            return await self._desktop_action_click(workspace_id, payload)

        if action == "drag":
            return await self._desktop_action_drag(workspace_id, payload)

        if action == "scroll":
            return await self._desktop_action_scroll(workspace_id, payload)

        if action == "type":
            return await self._desktop_action_type(workspace_id, payload)

        if action == "key":
            return await self._desktop_action_key(workspace_id, payload)

        if action == "open_url":
            return await self._desktop_action_open_url(workspace_id, payload)

        if action == "record_start":
            return await self._desktop_action_record_start(workspace_id, payload, log)

        if action == "record_stop":
            return await self._desktop_action_record_stop(workspace_id, payload, log)

        if action == "execute":
            return await self._desktop_action_execute(
                workspace_id, payload, execute_code
            )

        raise ValueError(f"Unknown desktop action: {action}")

    def _validate_desktop_execute_code(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> str:
        """Validate the ``execute`` code boundary; return the snippet."""
        execute_code = ""
        if action == "execute":
            raw_code = payload.get("code", "")
            if not isinstance(raw_code, str) or not raw_code.strip():
                raise ValueError("code must not be empty")
            if len(raw_code) > DESKTOP_EXECUTE_MAX_CHARS:
                raise ValueError(f"code exceeds {DESKTOP_EXECUTE_MAX_CHARS} characters")
            execute_code = raw_code

        return execute_code

    async def _desktop_action_ensure(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Ensure the shared desktop process (no lease)."""
        session = await self.ensure_desktop_process(
            workspace_id,
            width=payload.get("desktop_width"),
            height=payload.get("desktop_height"),
        )
        return {
            "ok": True,
            "display": DESKTOP_DISPLAY,
            "port": session.port,
        }

    async def _desktop_action_hold(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Ensure the process and acquire a viewer/computer-use lease."""
        holder = str(payload.get("kind") or DESKTOP_HOLDER_COMPUTERUSE)
        session = await self.acquire_desktop(
            workspace_id,
            holder=holder,
            run_id=payload.get("run_id"),
            width=payload.get("desktop_width"),
            height=payload.get("desktop_height"),
        )
        return {
            "ok": True,
            "display": DESKTOP_DISPLAY,
            "port": session.port,
            "viewer": session.viewer_held,
            "computer_use": bool(session.computeruse_run_ids),
            "mcp": session.mcp_active,
            "holder_count": session.holder_count,
        }

    async def _desktop_action_release(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Drop a lease; stop Xvnc when no holders remain."""
        holder = str(payload.get("kind") or DESKTOP_HOLDER_COMPUTERUSE)
        result = await self.release_desktop(
            workspace_id,
            holder=holder,
            run_id=payload.get("run_id"),
        )
        return {
            "ok": True,
            "stopped": result.stopped,
            "process_alive": result.process_alive,
            "viewer_held": result.viewer_held,
            "computer_use_active": result.computer_use_active,
        }

    async def _desktop_action_display_info(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the display name and framebuffer size."""
        width, height = await self._get_desktop_geometry(workspace_id)
        return {
            "ok": True,
            "display": DESKTOP_DISPLAY,
            "width": width,
            "height": height,
        }

    async def _desktop_action_screenshot(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
        log: Any,
    ) -> dict[str, Any]:
        """Capture one frame via ffmpeg x11grab."""
        width, height = await self._get_desktop_geometry(
            workspace_id,
            width=payload.get("width"),
            height=payload.get("height"),
        )
        crop_w = payload.get("crop_w")
        crop_h = payload.get("crop_h")
        crop_x = payload.get("crop_x")
        crop_y = payload.get("crop_y")
        crop_filter = ""
        result_width = width
        result_height = height
        if (
            crop_w is not None
            and crop_h is not None
            and crop_x is not None
            and crop_y is not None
        ):
            crop_w_int = int(crop_w)
            crop_h_int = int(crop_h)
            crop_x_int = int(crop_x)
            crop_y_int = int(crop_y)
            if (
                crop_w_int < 1
                or crop_h_int < 1
                or crop_x_int < 0
                or crop_y_int < 0
                or crop_x_int + crop_w_int > width
                or crop_y_int + crop_h_int > height
            ):
                raise ValueError("Invalid screenshot crop bounds")
            crop_filter = (
                f"-vf crop={crop_w_int}:{crop_h_int}:{crop_x_int}:{crop_y_int} "
            )
            result_width = crop_w_int
            result_height = crop_h_int
        image_format = str(payload.get("format") or "jpeg").strip().lower()
        if image_format not in {"jpeg", "png"}:
            raise ValueError(
                f"Invalid screenshot format: {payload.get('format')!r} "
                "(expected 'jpeg' or 'png')"
            )
        max_dimension = payload.get("max_dimension")
        scale_filter = ""
        if max_dimension is not None:
            try:
                max_dim = int(max_dimension)
            except (TypeError, ValueError):
                raise ValueError(
                    "Invalid screenshot max_dimension: "
                    f"{max_dimension!r} (expected positive integer)"
                )
            if max_dim < 1 or max_dim > 7680:
                raise ValueError(
                    "Invalid screenshot max_dimension: "
                    f"{max_dimension!r} (expected 1..7680)"
                )
            if max(result_width, result_height) > max_dim:
                factor = max_dim / max(result_width, result_height)
                out_w = max(1, int(result_width * factor))
                out_h = max(1, int(result_height * factor))
                scale_filter = f"scale={out_w}:{out_h},"
                result_width = out_w
                result_height = out_h
        if image_format == "png":
            codec_args = "-f image2 -vcodec png pipe:1"
            result_mime = "image/png"
        else:
            codec_args = "-f image2 -vcodec mjpeg pipe:1"
            result_mime = "image/jpeg"
        vf_filters: list[str] = []
        if crop_filter:
            # crop_filter is "-vf crop=... "; keep only the filter spec.
            vf_filters.append(crop_filter.replace("-vf", "").strip())
        if scale_filter:
            vf_filters.append(scale_filter.rstrip(","))
        vf_args = f"-vf {','.join(vf_filters)} " if vf_filters else ""
        ffmpeg_cmd = (
            f"ffmpeg -y -f x11grab -video_size {width}x{height} "
            f"-draw_mouse 1 -i {DESKTOP_DISPLAY} -frames:v 1 "
            f"{vf_args}"
            f"{codec_args} 2>/dev/null | base64 -w0"
        )
        exit_code, output = await self._exec_desktop_shell(workspace_id, ffmpeg_cmd)
        if exit_code != 0 or not output.strip():
            log.error("desktop_screenshot_failed", exit_code=exit_code)
            raise RuntimeError("Failed to capture desktop screenshot")
        image_b64 = output.strip()
        log.info(
            "desktop_screenshot_captured",
            format=image_format,
            width=result_width,
            height=result_height,
            image_b64_chars=len(image_b64),
        )
        return {
            "ok": True,
            "image_b64": image_b64,
            "mime": result_mime,
            "width": result_width,
            "height": result_height,
            "text": "",
        }

    async def _desktop_action_move(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Move the pointer via xdotool."""
        x = int(payload["x"])
        y = int(payload["y"])
        exit_code, output = await self._exec_desktop_shell(
            workspace_id,
            f"xdotool mousemove --sync {x} {y}",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to move mouse: {output}")
        return {"ok": True}

    async def _desktop_action_click(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Click a mouse button via xdotool."""
        button = _CLICK_BUTTONS.get(payload.get("button", "left"))
        if button is None:
            raise ValueError(f"Invalid mouse button: {payload.get('button')}")
        x = payload.get("x")
        y = payload.get("y")
        parts: list[str] = []
        if x is not None and y is not None:
            parts.append(f"xdotool mousemove --sync {int(x)} {int(y)}")
        repeat = " --repeat 2" if payload.get("double") else ""
        parts.append(f"xdotool click{repeat} {button}")
        exit_code, output = await self._exec_desktop_shell(
            workspace_id, " && ".join(parts)
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to click mouse: {output}")
        return {"ok": True}

    async def _desktop_action_drag(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Drag the pointer via xdotool."""
        start_x = int(payload["start_x"])
        start_y = int(payload["start_y"])
        end_x = int(payload["end_x"])
        end_y = int(payload["end_y"])
        command = (
            f"xdotool mousemove --sync {start_x} {start_y} mousedown 1 "
            f"mousemove --sync {end_x} {end_y} mouseup 1"
        )
        exit_code, output = await self._exec_desktop_shell(workspace_id, command)
        if exit_code != 0:
            raise RuntimeError(f"Failed to drag mouse: {output}")
        return {"ok": True}

    async def _desktop_action_scroll(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Scroll via xdotool click emulation."""
        direction = str(payload.get("direction", "")).lower()
        button = _SCROLL_BUTTONS.get(direction)
        if button is None:
            raise ValueError(f"Invalid scroll direction: {direction}")
        amount = int(payload.get("amount", 1))
        if amount < 1 or amount > 20:
            raise ValueError("Scroll amount must be between 1 and 20")
        x = payload.get("x")
        y = payload.get("y")
        parts = []
        if x is not None and y is not None:
            parts.append(f"xdotool mousemove --sync {int(x)} {int(y)}")
        parts.append(f"xdotool click --repeat {amount} {button}")
        exit_code, output = await self._exec_desktop_shell(
            workspace_id, " && ".join(parts)
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to scroll: {output}")
        return {"ok": True}

    async def _desktop_action_type(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Type text via xdotool."""
        text = str(payload.get("text", ""))
        if not text:
            raise ValueError("text must not be empty")
        exit_code, output = await self._exec_desktop_shell(
            workspace_id,
            _xdotool_type_command(text),
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to type text: {output}")
        return {"ok": True}

    async def _desktop_action_key(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Send a key combo via xdotool."""
        key = str(payload.get("key", "")).strip()
        if not key:
            raise ValueError("key must not be empty")
        modifiers = payload.get("modifiers") or []
        combo = _normalize_xdotool_key_combo(key, modifiers)
        exit_code, output = await self._exec_desktop_shell(
            workspace_id,
            f"xdotool key --clearmodifiers {shlex.quote(combo)}",
        )
        if _xdotool_key_failed(exit_code, output):
            raise RuntimeError(f"Failed to send key: {output}")
        return {"ok": True}

    async def _desktop_action_open_url(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Open an http(s) URL in the workspace browser."""
        url = str(payload.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must use http or https")
        quoted_url = shlex.quote(url)
        command = (
            "for browser in google-chrome-stable google-chrome chromium "
            "chromium-browser; do "
            f'if command -v "$browser" >/dev/null 2>&1; then '
            f'"$browser" --no-sandbox --disable-gpu --disable-dev-shm-usage '
            f"--no-first-run {quoted_url} >/dev/null 2>&1 & exit 0; fi; "
            "done; "
            f"xdg-open {quoted_url}"
        )
        exit_code, output = await self._exec_desktop_shell(workspace_id, command)
        if exit_code != 0:
            raise RuntimeError(f"Failed to open url: {output}")
        return {"ok": True}

    async def _desktop_action_record_start(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
        log: Any,
    ) -> dict[str, Any]:
        """Start an ffmpeg x11grab recording."""
        if not self._legacy_mode:
            return await self._managed_record_start(workspace_id, payload)
        run_id = self._sanitize_run_id(str(payload.get("run_id", "")))
        record_key = (workspace_id, run_id)
        existing = self._desktop_recordings.get(record_key)
        if existing is not None:
            return {"ok": True, "path": existing[1], "run_id": run_id}

        raw_path = payload.get("path")
        if raw_path:
            record_path = _sanitize_path(str(raw_path))
        else:
            record_path = f"{COMPUTER_USE_RECORD_DIR}/{run_id}/session.mp4"
        width, height = await self._get_desktop_geometry(workspace_id)
        parent_dir = os.path.dirname(record_path)
        ffmpeg_cmd = (
            f"mkdir -p {shlex.quote(parent_dir)} && "
            f"ffmpeg -y -f x11grab -video_size {width}x{height} "
            f"-framerate 10 -draw_mouse 1 -i {DESKTOP_DISPLAY} "
            "-c:v libx264 -preset ultrafast -pix_fmt yuv420p "
            f"{shlex.quote(record_path)} </dev/null "
            f">>{shlex.quote(record_path + '.log')} 2>&1 & echo $!"
        )
        exit_code, output = await self._exec_desktop_shell(workspace_id, ffmpeg_cmd)
        if exit_code != 0:
            raise RuntimeError(f"Failed to start desktop recording: {output}")
        pid_text = output.strip().splitlines()[-1].strip()
        try:
            pid = int(pid_text)
        except ValueError:
            raise RuntimeError(
                f"Failed to start desktop recording: invalid pid {pid_text!r}"
            )
        self._desktop_recordings[record_key] = (pid, record_path)
        log.info("desktop_recording_started", run_id=run_id, pid=pid)
        return {"ok": True, "path": record_path, "run_id": run_id}

    async def _desktop_action_record_stop(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
        log: Any,
    ) -> dict[str, Any]:
        """Stop an ffmpeg recording."""
        if not self._legacy_mode:
            return await self._managed_record_stop(workspace_id, payload)
        run_id = self._sanitize_run_id(str(payload.get("run_id", "")))
        record_key = (workspace_id, run_id)
        recording = self._desktop_recordings.get(record_key)
        if recording is None:
            raise RuntimeError(f"No active recording for run_id: {run_id}")
        pid, record_path = recording
        stop_cmd = (
            f"kill -INT {pid} 2>/dev/null || true; "
            "sleep 0.5; "
            f"kill -0 {pid} 2>/dev/null && kill -TERM {pid} 2>/dev/null || true"
        )
        exit_code, output = await self._exec_desktop_shell(workspace_id, stop_cmd)
        self._desktop_recordings.pop(record_key, None)
        if exit_code != 0:
            raise RuntimeError(f"Failed to stop desktop recording: {output}")
        log.info("desktop_recording_stopped", run_id=run_id, pid=pid)
        return {"ok": True, "path": record_path}

    async def _desktop_action_execute(
        self,
        workspace_id: uuid.UUID,
        payload: dict[str, Any],
        execute_code: str,
    ) -> dict[str, Any]:
        """Run a caller-provided snippet via ``python3 -c``."""
        # Generic Agent-S action execution: run exactly the passed
        # internal code (materialized by the backend core) via
        # ``python3 -c`` with the desktop env. No agent logic lives
        # here; validation only guards the RPC boundary. Validation ran
        # above; a single generic liveness probe also ran above.
        # Xvnc runs with ``-SecurityTypes None``, but python-Xlib
        # unconditionally opens ``$XAUTHORITY``/``~/.Xauthority`` —
        # without the file every snippet dies before connecting.
        # Ensure it (idempotent) on the generic execute path as well:
        # this self-heals desktops started before the start-command
        # fix and any workspace where the file was removed.
        await self._ensure_desktop_xauthority(workspace_id)
        exit_code, stdout, stderr = await asyncio.wait_for(
            self._exec_harness_command(
                workspace_id,
                ["python3", "-c", execute_code],
                workdir="/workspace",
                env=self._desktop_env(),
            ),
            timeout=DESKTOP_EXECUTE_TIMEOUT_S,
        )
        return {
            "ok": True,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
        }

    @live_interaction
    async def write_desktop_clipboard(self, workspace_id: uuid.UUID, text: str) -> None:
        """Write plain text into the desktop clipboard inside the workspace VM/container."""
        if not await self._is_desktop_session_live(workspace_id):
            raise RuntimeError("Desktop session is not active")

        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        encoded_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
        command = (
            "set -e;"
            "TOOL='';"
            "if command -v xsel >/dev/null 2>&1; then TOOL='xsel'; "
            "elif command -v xclip >/dev/null 2>&1; then TOOL='xclip'; "
            "elif command -v apt-get >/dev/null 2>&1; then "
            "apt-get update >/tmp/opencuria-clipboard-apt.log 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y xclip xsel "
            ">>/tmp/opencuria-clipboard-apt.log 2>&1 || true; "
            "if command -v xsel >/dev/null 2>&1; then TOOL='xsel'; "
            "elif command -v xclip >/dev/null 2>&1; then TOOL='xclip'; fi; "
            "fi; "
            "if [ -z \"$TOOL\" ]; then echo 'clipboard tool missing' >&2; exit 127; fi; "
            f"printf %s '{encoded_text}' | base64 -d | "
            "if [ \"$TOOL\" = 'xsel' ]; then "
            "DISPLAY=:1 xsel --clipboard --input; "
            "else DISPLAY=:1 timeout 3 xclip -selection clipboard -in >/dev/null 2>&1 || true; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            ["sh", "-lc", command],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to write desktop clipboard: {output}")

    @live_interaction
    async def read_desktop_clipboard(self, workspace_id: uuid.UUID) -> str:
        """Read plain text from the desktop clipboard inside the workspace VM/container."""
        if not await self._is_desktop_session_live(workspace_id):
            raise RuntimeError("Desktop session is not active")

        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        command = (
            "set -e;"
            "TOOL='';"
            "if command -v xclip >/dev/null 2>&1; then TOOL='xclip'; "
            "elif command -v xsel >/dev/null 2>&1; then TOOL='xsel'; "
            "elif command -v apt-get >/dev/null 2>&1; then "
            "apt-get update >/tmp/opencuria-clipboard-apt.log 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y xclip xsel "
            ">>/tmp/opencuria-clipboard-apt.log 2>&1 || true; "
            "if command -v xclip >/dev/null 2>&1; then TOOL='xclip'; "
            "elif command -v xsel >/dev/null 2>&1; then TOOL='xsel'; fi; "
            "fi; "
            "if [ -z \"$TOOL\" ]; then echo 'clipboard tool missing' >&2; exit 127; fi; "
            "if [ \"$TOOL\" = 'xclip' ]; then "
            "DISPLAY=:1 xclip -selection clipboard -o 2>/dev/null || true; "
            "else DISPLAY=:1 xsel --clipboard --output 2>/dev/null || true; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            ["sh", "-lc", command],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to read desktop clipboard: {output}")
        return output

    def get_desktop_session(self, workspace_id: uuid.UUID) -> DesktopSession | None:
        """Return the active desktop session if any."""
        return self._desktop_sessions.get(workspace_id)

    def get_desktop_container_ip(self, workspace_id: uuid.UUID) -> str:
        """Get the upstream IP address for the workspace desktop proxy."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not hasattr(runtime, "get_container_ip"):
            raise RuntimeError("Runtime does not support desktop proxy")
        return runtime.get_container_ip(info.instance_id, str(workspace_id))

    def get_desktop_network_name(self, workspace_id: uuid.UUID) -> str:
        """Get the backend-attachable network for a workspace desktop proxy."""
        runtime = self._get_runtime(workspace_id)
        if not hasattr(runtime, "get_workspace_network_name"):
            raise RuntimeError("Runtime does not support desktop networking")
        return runtime.get_workspace_network_name(str(workspace_id))


__all__ = [
    "COMPUTER_USE_RECORD_DIR",
    "DEFAULT_DESKTOP_HEIGHT",
    "DEFAULT_DESKTOP_WIDTH",
    "DESKTOP_DISPLAY",
    "DESKTOP_EXECUTE_MAX_CHARS",
    "DESKTOP_EXECUTE_TIMEOUT_S",
    "DESKTOP_HOLDER_COMPUTERUSE",
    "DESKTOP_HOLDER_VIEWER",
    "DESKTOP_HOME",
    "DESKTOP_XAUTHORITY_PATH",
    "MAX_DESKTOP_HEIGHT",
    "MAX_DESKTOP_WIDTH",
    "MIN_DESKTOP_HEIGHT",
    "MIN_DESKTOP_WIDTH",
    "_CLICK_BUTTONS",
    "_RUN_ID_RE",
    "_SCROLL_BUTTONS",
    "DesktopManager",
]
