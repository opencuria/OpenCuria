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
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import structlog

from ...models import DesktopReleaseResult, DesktopSession, WorkspaceInfo
from ...runtime.base import RuntimeBackend
from ..exec_kernel import KeyedLockMap
from ..exec_kernel import sanitize_path as _sanitize_path
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
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._exec_harness_command = exec_harness_command
        self._desktop_sessions: dict[uuid.UUID, DesktopSession] = {}
        self._desktop_recordings: dict[
            tuple[uuid.UUID, str], tuple[int, str]
        ] = {}
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
            _coerce(
                width, DEFAULT_DESKTOP_WIDTH, MIN_DESKTOP_WIDTH, MAX_DESKTOP_WIDTH
            ),
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
        }

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

        start_command = self._desktop_start_command(
            resolved_width, resolved_height
        )
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
        if kind != DESKTOP_HOLDER_VIEWER:
            # Fail fast on invalid run ids before touching shared state.
            self._sanitize_run_id(str(run_id or ""))
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            # ``seen`` cannot change under us: every other acquire/release
            # path takes the same lock, which we currently hold. The merge
            # below only matters when ensure *replaces* the cache entry
            # with a fresh Xvnc incarnation (stale restart/recovery):
            # leases added to the old object before the replacement are
            # carried onto the new one so neither holder loses its lease.
            seen = self._desktop_sessions.get(workspace_id)
            seen_viewer = seen.viewer_held if seen is not None else False
            seen_runs = (
                set(seen.computeruse_run_ids) if seen is not None else set()
            )
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
        if kind != DESKTOP_HOLDER_VIEWER:
            self._sanitize_run_id(str(run_id or ""))
        lock = await self._desktop_lock(workspace_id)
        async with lock:
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
            self._desktop_sessions.pop(workspace_id, None)
        else:
            self._desktop_sessions.pop(workspace_id, None)
        try:
            runtime = self._get_runtime(workspace_id)
            info = self._get_cached(workspace_id)
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                ["/usr/local/bin/opencuria-desktop-stop"],
            )
            if exit_code != 0:
                log.warning("desktop_stop_nonzero", exit_code=exit_code, output=output)
        except Exception:
            log.exception("desktop_stop_failed")

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

        if action == "ensure":
            return await self._desktop_action_ensure(workspace_id, payload)

        if action == "hold":
            return await self._desktop_action_hold(workspace_id, payload)

        if action == "release":
            return await self._desktop_action_release(workspace_id, payload)

        execute_code = self._validate_desktop_execute_code(action, payload)

        if action not in {"ensure", "hold", "release"}:
            await self._require_desktop_live(workspace_id)

        if action == "display_info":
            return await self._desktop_action_display_info(workspace_id, payload)

        if action == "screenshot":
            return await self._desktop_action_screenshot(
                workspace_id, payload, log
            )

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
            return await self._desktop_action_record_start(
                workspace_id, payload, log
            )

        if action == "record_stop":
            return await self._desktop_action_record_stop(
                workspace_id, payload, log
            )

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
                raise ValueError(
                    f"code exceeds {DESKTOP_EXECUTE_MAX_CHARS} characters"
                )
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
    "DesktopManager",
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
]
