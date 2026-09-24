"""Background (detached) process sessions.

Canonical home (Step 3) for the background process management previously
living on ``WorkspaceService`` in :mod:`src.service`:
``BackgroundProcess``, ``BACKGROUND_PROCESS_DIR`` / ``_BACKGROUND_*``
constants, the ``_background_processes`` / ``_background_lock`` /
``_background_start_locks`` (+ guard) state, and the start / status /
stop / verify-and-reattach operations.

``WorkspaceService`` keeps thin delegates (same names/signatures/
messages) plus a ``_background_processes`` property alias onto the
manager-owned dict, so existing callers and tests keep working.

Workspace resolution is injected so this module never imports
``src.service`` (no dependency cycle). ``sanitize_exec_workdir`` always
writes through to the canonical ``src.services.exec_kernel`` helper
(a callable parameter, defaulting to the canonical function) so
``sync_from_runtime``-style reassignments stay correct.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import structlog

from ...models import WorkspaceInfo
from ...runtime.base import RuntimeBackend
from ..credentials import WORKSPACE_CREDENTIAL_ENV_FILE as _CREDENTIAL_ENV_FILE
from ..exec_kernel import KeyedLockMap
from ..exec_kernel import sanitize_exec_workdir as _canonical_sanitize_exec_workdir

logger = structlog.get_logger(__name__)

BACKGROUND_PROCESS_DIR = "/workspace/.opencuria/processes"
_BACKGROUND_PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BACKGROUND_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKGROUND_STOP_GRACE_S = 2.0
_BACKGROUND_STOP_POLL_S = 0.2
#: Max candidate entries accepted by a single process_verify request.
#: Bounds per-request exec probes (one kill -0 + at most one cat per
#: candidate) so a hostile/misbehaving backend cannot fan out
#: unbounded workspace execs.
_BACKGROUND_VERIFY_MAX_ENTRIES = 100


@dataclass
class BackgroundProcess:
    """Detached background process tracked in memory (runner owns liveness)."""

    process_id: str
    workspace_id: uuid.UUID
    pid: int
    command: str
    workdir: str
    log_path: str
    exit_path: str
    name: str = ""
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


def _status_dict(
    process_id: str,
    status: str,
    exit_code: int | None,
    pid: int | None,
    error: str | None = None,
) -> dict[str, Any]:
    """Build one background status dict (single canonical shape).

    Used by :meth:`BackgroundProcessManager._background_status_locked`
    and the verify-and-reattach paths so every ``running`` / ``exited`` /
    ``unknown`` dict has identical keys/values. ``error`` is included
    only when set.
    """
    result: dict[str, Any] = {
        "process_id": process_id,
        "status": status,
        "exit_code": exit_code,
        "pid": pid,
    }
    if error is not None:
        result["error"] = error
    return result


class BackgroundProcessManager:
    """Owns workspace-scoped detached background processes.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids.
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes.
    - ``sanitize_exec_workdir``: ``(path) -> str``; defaults to the
      canonical ``src.services.exec_kernel.sanitize_exec_workdir``.
    - ``credential_env_file``: guest path sourced by the start wrapper.
      ``WorkspaceService`` passes its ``WORKSPACE_CREDENTIAL_ENV_FILE``
      constant; the default matches it.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        sanitize_exec_workdir: Callable[[str], str] | None = None,
        credential_env_file: str = _CREDENTIAL_ENV_FILE,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._sanitize_exec_workdir = (
            sanitize_exec_workdir
            if sanitize_exec_workdir is not None
            else _canonical_sanitize_exec_workdir
        )
        # Canonical default lives in ``src.services.credentials`` (Step 4
        # forward-move); the literal matches
        # ``WORKSPACE_CREDENTIAL_ENV_FILE`` so static parity holds.
        self._credential_env_file = credential_env_file
        self._background_processes: dict[uuid.UUID, dict[str, BackgroundProcess]] = {}
        self._background_lock = asyncio.Lock()
        # Serialises concurrent starts of the same process_id so two
        # parallel starts cannot orphan each other's PID (last-writer-wins
        # on the tracking dict would leak the loser's process). Entries are
        # retained for the runner lifetime (bounded by ever-seen
        # workspace/process ids, cleared on restart) — same rationale as
        # the desktop locks: dropping a lock object while a holder waits
        # would hand the next caller a different lock.
        # Step 5: the get-or-create never-evict mechanics live in the
        # canonical ``KeyedLockMap`` (``src.services.exec_kernel``);
        # ``_background_start_locks`` / ``_background_start_locks_guard``
        # stay readable/writable as live aliases onto the map's dict/guard
        # so ``WorkspaceService`` property aliases and tests poking
        # ``manager._background_start_locks[...]`` keep working.
        self._background_start_lock_map: KeyedLockMap[
            tuple[uuid.UUID, str]
        ] = KeyedLockMap()

    @staticmethod
    def _sanitize_background_file_path(value: str | None, suffix: str) -> str | None:
        """Validate a backend-assigned background log/exit path.

        Returns the path when it lives directly under
        ``BACKGROUND_PROCESS_DIR`` (prefix ``DIR + "/"``), contains no
        ``..`` segments, its basename matches
        ``^[A-Za-z0-9][A-Za-z0-9._-]*$`` and ends with *suffix*
        (``.log`` / ``.exit``).  Returns ``None`` for anything else so
        callers fall back to the legacy ``{DIR}/{process_id}`` schema.
        """
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        prefix = BACKGROUND_PROCESS_DIR.rstrip("/") + "/"
        if not cleaned.startswith(prefix):
            return None
        remainder = cleaned[len(prefix):]
        if not remainder or "/" in remainder or ".." in remainder:
            return None
        basename = remainder
        if not basename.endswith(suffix):
            return None
        if not _BACKGROUND_PROCESS_ID_RE.match(basename):
            return None
        return cleaned

    @staticmethod
    def _sanitize_process_id(process_id: str) -> str:
        """Validate a backend-assigned background process id."""
        cleaned = (process_id or "").strip()
        if not cleaned or not _BACKGROUND_PROCESS_ID_RE.match(cleaned):
            raise ValueError(f"Invalid process_id: {process_id!r}")
        return cleaned

    @staticmethod
    def _build_background_start_shell(
        command: str,
        log_path: str,
        exit_path: str,
        extra_env: dict[str, str] | None = None,
        credential_env_file: str = _CREDENTIAL_ENV_FILE,
    ) -> str:
        """Build a detached start shell for a background process.

        The wrapper sources the persistent credential env file, applies
        per-process env overrides, then runs the command detached via
        ``setsid`` and records the exit code in *exit_path*.

        The command runs in a subshell so shell-terminating commands
        (e.g. ``exit 3``) only terminate the subshell and the outer
        shell still writes ``$?`` to the exit file.

        ``credential_env_file`` defaults to the runner guest path so the
        helper stays callable without an instance (parity with the
        ``WorkspaceService`` static facade); instance calls pass the
        configured ``self._credential_env_file``.
        """
        env_assignments = " ".join(
            f"{key}={shlex.quote(str(value))}"
            for key, value in (extra_env or {}).items()
            if _BACKGROUND_ENV_KEY_RE.match(str(key))
        )
        source = (
            f"if [ -f {shlex.quote(credential_env_file)} ]; then "
            f". {shlex.quote(credential_env_file)}; fi"
        )
        if env_assignments:
            runner_cmd = f"{source}; export {env_assignments}; ( {command} )"
        else:
            runner_cmd = f"{source}; ( {command} )"
        return (
            f"mkdir -p {shlex.quote(BACKGROUND_PROCESS_DIR)} && "
            f"rm -f {shlex.quote(exit_path)} && "
            f"setsid bash -c {shlex.quote(runner_cmd + '; echo $? > ' + exit_path)}"
            f" > {shlex.quote(log_path)} 2>&1 < /dev/null & echo $!"
        )

    async def _probe_background_pid(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        pid: int,
    ) -> bool:
        """Return True when *pid* is still alive inside the workspace."""
        exit_code, _ = await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", f"kill -0 {int(pid)} 2>/dev/null"],
            workdir="/workspace",
        )
        return exit_code == 0

    async def _read_background_exit_code(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        exit_path: str,
    ) -> int | None:
        """Read the exit code recorded in *exit_path*, if any."""
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["cat", exit_path],
            workdir="/workspace",
        )
        if exit_code != 0:
            return None
        try:
            return int(output.strip().split()[0])
        except (IndexError, ValueError):
            return None

    async def _kill_background_pid(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        pid: int,
        signal: str,
    ) -> None:
        """Best-effort signal delivery to a background process group."""
        script = (
            f"kill -{signal} -{int(pid)} 2>/dev/null || "
            f"kill -{signal} {int(pid)} 2>/dev/null || true"
        )
        await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", script],
            workdir="/workspace",
        )

    @property
    def _background_start_locks(
        self,
    ) -> dict[tuple[uuid.UUID, str], asyncio.Lock]:
        """Alias onto the start-lock map's underlying dict (live)."""
        return self._background_start_lock_map.locks

    @_background_start_locks.setter
    def _background_start_locks(self, value: dict) -> None:
        self._background_start_lock_map.locks.clear()
        self._background_start_lock_map.locks.update(value)

    @property
    def _background_start_locks_guard(self) -> asyncio.Lock:
        """Alias onto the start-lock map's guard lock."""
        return self._background_start_lock_map.guard

    @_background_start_locks_guard.setter
    def _background_start_locks_guard(self, value: asyncio.Lock) -> None:
        self._background_start_lock_map.guard = value

    async def _background_start_lock(
        self, workspace_id: uuid.UUID, process_id: str
    ) -> asyncio.Lock:
        """Return the serialising lock for one workspace/process_id pair.

        Step 5: thin delegate onto the canonical ``KeyedLockMap``.
        """
        return await self._background_start_lock_map.get(
            (workspace_id, process_id)
        )

    async def _stop_background_pid_graceful(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        pid: int,
    ) -> None:
        """Best-effort stop of one background PID (TERM -> grace -> KILL)."""
        try:
            if not await self._probe_background_pid(runtime, instance_id, pid):
                return
            await self._kill_background_pid(runtime, instance_id, pid, "TERM")
            elapsed = 0.0
            while elapsed <= _BACKGROUND_STOP_GRACE_S:
                if not await self._probe_background_pid(runtime, instance_id, pid):
                    return
                await asyncio.sleep(_BACKGROUND_STOP_POLL_S)
                elapsed += _BACKGROUND_STOP_POLL_S
            if await self._probe_background_pid(runtime, instance_id, pid):
                await self._kill_background_pid(runtime, instance_id, pid, "KILL")
        except Exception:
            logger.exception(
                "background_process_restart_stop_failed",
                old_pid=pid,
            )

    async def verify_and_reattach_background_processes(
        self,
        workspace_id: uuid.UUID,
        expected: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Verify untracked ("vanished") processes and reattach live ones.

        The backend sends candidate ``{process_id, pid, log_path,
        exit_path, command?, workdir?, name?}`` dicts for DB-RUNNING rows
        that no longer appear in the runner report (typically after a
        runner restart wiped in-memory tracking). For each candidate:

        - already tracked -> normal live status (no state change);
        - untracked but PID alive -> reattach into
          ``_background_processes`` (idempotent) and report ``running``;
        - untracked and PID dead but exit file readable -> ``exited``
          with the recorded code (no tracking);
        - otherwise -> ``unknown`` (no tracking).

        A runtime/workspace failure yields a per-candidate error entry
        instead of failing the whole batch. Paths outside
        ``BACKGROUND_PROCESS_DIR`` (or with a failing process_id) are
        rejected fail-closed as ``unknown`` so a hostile payload can
        neither reattach nor probe arbitrary files.
        """
        results: list[dict[str, Any]] = []
        if not isinstance(expected, list):
            raise ValueError("expected must be a list")
        if len(expected) > _BACKGROUND_VERIFY_MAX_ENTRIES:
            raise ValueError(
                f"expected must hold at most {_BACKGROUND_VERIFY_MAX_ENTRIES} entries"
            )
        try:
            if self._get_cached is None or self._get_runtime is None:
                raise RuntimeError(
                    "BackgroundProcessManager has no workspace lookup configured"
                )
            info = self._get_cached(workspace_id)
            runtime = self._get_runtime(workspace_id)
            if not info.instance_id:
                raise RuntimeError("Workspace has no instance assigned")
        except Exception as exc:
            for candidate in expected:
                raw_id = (
                    candidate.get("process_id")
                    if isinstance(candidate, dict)
                    else None
                )
                results.append(
                    _status_dict(
                        str(raw_id or ""), "unknown", None, None, str(exc)
                    )
                )
            return results

        for candidate in expected:
            if not isinstance(candidate, dict):
                results.append(
                    _status_dict("", "unknown", None, None, "invalid candidate entry")
                )
                continue
            raw_process_id = candidate.get("process_id", "")
            try:
                cleaned_process_id = self._sanitize_process_id(
                    str(raw_process_id or "")
                )
            except ValueError as exc:
                results.append(
                    _status_dict(
                        str(raw_process_id or ""),
                        "unknown",
                        None,
                        None,
                        str(exc),
                    )
                )
                continue
            raw_pid = candidate.get("pid")
            try:
                pid = int(raw_pid)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                results.append(
                    _status_dict(
                        cleaned_process_id,
                        "unknown",
                        None,
                        None,
                        f"Invalid pid: {raw_pid!r}",
                    )
                )
                continue
            if pid <= 0:
                results.append(
                    _status_dict(
                        cleaned_process_id,
                        "unknown",
                        None,
                        pid,
                        f"Invalid pid: {raw_pid!r}",
                    )
                )
                continue
            log_path = self._sanitize_background_file_path(
                candidate.get("log_path"), ".log"
            ) or (f"{BACKGROUND_PROCESS_DIR}/{cleaned_process_id}.log")
            exit_path = self._sanitize_background_file_path(
                candidate.get("exit_path"), ".exit"
            ) or (f"{BACKGROUND_PROCESS_DIR}/{cleaned_process_id}.exit")
            try:
                async with self._background_lock:
                    tracked = self._background_processes.get(
                        workspace_id, {}
                    ).get(cleaned_process_id)
                if tracked is not None:
                    status = await self._background_status_locked(
                        runtime, info.instance_id, tracked
                    )
                    results.append(status)
                    continue
                if await self._probe_background_pid(
                    runtime, info.instance_id, pid
                ):
                    command = candidate.get("command", "")
                    if not isinstance(command, str):
                        command = ""
                    workdir = candidate.get("workdir", "/workspace")
                    try:
                        safe_workdir = self._sanitize_exec_workdir(workdir)
                    except ValueError:
                        safe_workdir = "/workspace"
                    name = candidate.get("name", "")
                    if not isinstance(name, str):
                        name = ""
                    entry = BackgroundProcess(
                        process_id=cleaned_process_id,
                        workspace_id=workspace_id,
                        pid=pid,
                        command=command.strip() if command else "",
                        workdir=safe_workdir,
                        log_path=log_path,
                        exit_path=exit_path,
                        name=name,
                    )
                    async with self._background_lock:
                        existing = self._background_processes.get(
                            workspace_id, {}
                        ).get(cleaned_process_id)
                        if existing is None:
                            self._background_processes.setdefault(
                                workspace_id, {}
                            )[cleaned_process_id] = entry
                        else:
                            # A concurrent start won the slot while the
                            # probes were in flight: report the winner's
                            # live status instead of overwriting it.
                            winner = existing
                    if existing is not None:
                        status = await self._background_status_locked(
                            runtime, info.instance_id, winner
                        )
                        results.append(status)
                        continue
                    logger.info(
                        "background_process_reattached",
                        workspace_id=str(workspace_id),
                        process_id=cleaned_process_id,
                        pid=pid,
                    )
                    results.append(
                        _status_dict(cleaned_process_id, "running", None, pid)
                    )
                    continue
                exit_code = await self._read_background_exit_code(
                    runtime, info.instance_id, exit_path
                )
                if exit_code is None:
                    results.append(
                        _status_dict(cleaned_process_id, "unknown", None, pid)
                    )
                else:
                    results.append(
                        _status_dict(
                            cleaned_process_id, "exited", exit_code, pid
                        )
                    )
            except Exception as exc:
                logger.exception(
                    "background_process_verify_failed",
                    workspace_id=str(workspace_id),
                    process_id=cleaned_process_id,
                )
                results.append(
                    _status_dict(
                        cleaned_process_id, "unknown", None, pid, str(exc)
                    )
                )
        return results

    async def _drop_background_tracking(
        self, workspace_id: uuid.UUID, *, reason: str
    ) -> int:
        """Drop in-memory tracking after the VM/container was rebooted.

        RAM processes are dead by definition, so PID entries can never
        be valid again; keeping them would report stale ``exited`` rows
        and risk signalling a reused foreign PID.
        """
        async with self._background_lock:
            entries = self._background_processes.pop(workspace_id, None)
        count = len(entries) if entries else 0
        if count:
            logger.info(
                "background_processes_dropped_after_restart",
                workspace_id=str(workspace_id),
                count=count,
                reason=reason,
            )
        return count

    async def start_background_process(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str = "",
        log_path: str | None = None,
        exit_path: str | None = None,
    ) -> dict[str, Any]:
        """Start a detached background process inside a workspace.

        Args:
            workspace_id: Target workspace.
            process_id: Backend-assigned unique id (used for log/exit files).
            command: Shell command to run detached (non-empty).
            workdir: Working directory inside the workspace VM/container.
            env: Optional per-process environment overrides.
            name: Optional human-readable process name.
            log_path: Optional backend-assigned log path. Used only when it
                passes :meth:`_sanitize_background_file_path` validation;
                otherwise the legacy ``{DIR}/{process_id}.log`` schema applies.
            exit_path: Optional backend-assigned exit path (same rule,
                ``.exit`` suffix).

        Returns:
            Dict with ``process_id``, ``pid``, ``log_path``, ``exit_path``.
        """
        cleaned_process_id = self._sanitize_process_id(process_id)
        if not (command or "").strip():
            raise ValueError("command must not be empty")
        safe_workdir = self._sanitize_exec_workdir(workdir)
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError(
                "BackgroundProcessManager has no workspace lookup configured"
            )
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        extra_env = dict(env or {})
        for key in extra_env:
            if not _BACKGROUND_ENV_KEY_RE.match(str(key)):
                raise ValueError(f"Invalid env key: {key!r}")

        legacy_log_path = f"{BACKGROUND_PROCESS_DIR}/{cleaned_process_id}.log"
        legacy_exit_path = f"{BACKGROUND_PROCESS_DIR}/{cleaned_process_id}.exit"
        resolved_log_path = (
            self._sanitize_background_file_path(log_path, ".log")
            or legacy_log_path
        )
        resolved_exit_path = (
            self._sanitize_background_file_path(exit_path, ".exit")
            or legacy_exit_path
        )
        # Restart safety: serialised per process_id so two concurrent
        # starts cannot orphan each other's PID (the tracking dict would
        # otherwise keep only the last writer and leak the loser's
        # process). A living old PID is best-effort stopped (TERM ->
        # grace -> KILL) before the new run starts; the tracking entry
        # itself is replaced below after start.
        start_lock = await self._background_start_lock(
            workspace_id, cleaned_process_id
        )
        async with start_lock:
            async with self._background_lock:
                old_entry = self._background_processes.get(
                    workspace_id, {}
                ).get(cleaned_process_id)
            old_pid = old_entry.pid if old_entry is not None else None
            if old_pid is not None:
                await self._stop_background_pid_graceful(
                    runtime, info.instance_id, old_pid
                )
            start_shell = self._build_background_start_shell(
                command.strip(),
                resolved_log_path,
                resolved_exit_path,
                extra_env,
                self._credential_env_file,
            )
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["sh", "-lc", start_shell],
                workdir=safe_workdir,
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to start background process: {output}")
            try:
                pid = int(output.strip().split()[-1])
            except (IndexError, ValueError) as exc:
                raise RuntimeError(
                    f"Failed to parse background process pid: {output!r}"
                ) from exc

            entry = BackgroundProcess(
                process_id=cleaned_process_id,
                workspace_id=workspace_id,
                pid=pid,
                command=command.strip(),
                workdir=safe_workdir,
                log_path=resolved_log_path,
                exit_path=resolved_exit_path,
                name=name or "",
            )
            async with self._background_lock:
                self._background_processes.setdefault(workspace_id, {})[
                    cleaned_process_id
                ] = entry
        logger.info(
            "background_process_started",
            workspace_id=str(workspace_id),
            process_id=cleaned_process_id,
            pid=pid,
        )
        return {
            "process_id": cleaned_process_id,
            "pid": pid,
            "log_path": resolved_log_path,
            "exit_path": resolved_exit_path,
        }

    async def _background_status_locked(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        entry: BackgroundProcess,
    ) -> dict[str, Any]:
        """Compute a live status dict for a tracked background process."""
        running = await self._probe_background_pid(runtime, instance_id, entry.pid)
        if running:
            return _status_dict(entry.process_id, "running", None, entry.pid)
        exit_code = await self._read_background_exit_code(
            runtime, instance_id, entry.exit_path
        )
        if exit_code is None:
            return _status_dict(entry.process_id, "unknown", None, entry.pid)
        return _status_dict(entry.process_id, "exited", exit_code, entry.pid)

    def _get_background_entry(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
    ) -> BackgroundProcess:
        """Return the tracked entry or raise for unknown process ids."""
        cleaned = self._sanitize_process_id(process_id)
        entry = self._background_processes.get(workspace_id, {}).get(cleaned)
        if entry is None:
            raise ValueError(
                f"Background process {cleaned} not found "
                f"for workspace {workspace_id}"
            )
        return entry

    async def get_background_status(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
    ) -> dict[str, Any]:
        """Return the live status of one tracked background process."""
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError(
                "BackgroundProcessManager has no workspace lookup configured"
            )
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with self._background_lock:
            entry = self._get_background_entry(workspace_id, process_id)
        return await self._background_status_locked(runtime, info.instance_id, entry)

    async def list_background_processes(
        self,
        workspace_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Return live statuses for all tracked background processes."""
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError(
                "BackgroundProcessManager has no workspace lookup configured"
            )
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with self._background_lock:
            entries = list(self._background_processes.get(workspace_id, {}).values())
        results: list[dict[str, Any]] = []
        for entry in entries:
            status = await self._background_status_locked(
                runtime, info.instance_id, entry
            )
            results.append(
                {
                    **status,
                    "command": entry.command,
                    "workdir": entry.workdir,
                    "log_path": entry.log_path,
                    "name": entry.name,
                    "started_at": entry.started_at.isoformat(),
                }
            )
        return results

    async def stop_background_process(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
    ) -> dict[str, Any]:
        """Stop a tracked background process and drop it from tracking.

        Sends SIGTERM, waits up to a short grace period, then escalates to
        SIGKILL. Already exited processes are cleaned up and reported as
        exited.
        """
        if self._get_cached is None or self._get_runtime is None:
            raise RuntimeError(
                "BackgroundProcessManager has no workspace lookup configured"
            )
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with self._background_lock:
            entry = self._get_background_entry(workspace_id, process_id)

        if not await self._probe_background_pid(runtime, info.instance_id, entry.pid):
            exit_code = await self._read_background_exit_code(
                runtime, info.instance_id, entry.exit_path
            )
            async with self._background_lock:
                self._background_processes.get(workspace_id, {}).pop(
                    entry.process_id, None
                )
            logger.info(
                "background_process_already_exited",
                workspace_id=str(workspace_id),
                process_id=entry.process_id,
                exit_code=exit_code,
            )
            return {
                "process_id": entry.process_id,
                "stopped": False,
                "status": "exited" if exit_code is not None else "unknown",
                "exit_code": exit_code,
                "pid": entry.pid,
            }

        await self._kill_background_pid(runtime, info.instance_id, entry.pid, "TERM")
        elapsed = 0.0
        stopped = False
        while elapsed <= _BACKGROUND_STOP_GRACE_S:
            if not await self._probe_background_pid(
                runtime, info.instance_id, entry.pid
            ):
                stopped = True
                break
            await asyncio.sleep(_BACKGROUND_STOP_POLL_S)
            elapsed += _BACKGROUND_STOP_POLL_S
        if not stopped:
            await self._kill_background_pid(
                runtime, info.instance_id, entry.pid, "KILL"
            )
            # Same grace poll as after TERM: the wrapper's exit file is
            # only written once the shell actually dies, so wait for the
            # PID to disappear before reading it (avoids "unknown" with
            # a lost exit code on fast kills).
            elapsed = 0.0
            while elapsed <= _BACKGROUND_STOP_GRACE_S:
                if not await self._probe_background_pid(
                    runtime, info.instance_id, entry.pid
                ):
                    break
                await asyncio.sleep(_BACKGROUND_STOP_POLL_S)
                elapsed += _BACKGROUND_STOP_POLL_S
            stopped = True
        exit_code = await self._read_background_exit_code(
            runtime, info.instance_id, entry.exit_path
        )
        async with self._background_lock:
            self._background_processes.get(workspace_id, {}).pop(
                entry.process_id, None
            )
        logger.info(
            "background_process_stopped",
            workspace_id=str(workspace_id),
            process_id=entry.process_id,
            pid=entry.pid,
        )
        return {
            "process_id": entry.process_id,
            "stopped": stopped,
            "status": "exited" if exit_code is not None else "unknown",
            "exit_code": exit_code,
            "pid": entry.pid,
        }

    async def _kill_all_background_processes(
        self,
        workspace_id: uuid.UUID,
        *,
        reason: str,
    ) -> None:
        """Best-effort kill of every tracked process for a workspace.

        Mirrors the single-stop protocol per PID: SIGTERM, a grace poll,
        then SIGKILL for survivors, followed by one verification probe.
        Tracking is dropped only after the kill attempt ran; when the
        workspace/runtime is gone there is nothing left to signal, so
        tracking is dropped as well (only a runtime stop/remove can
        guarantee death in that case — both callers do exactly that).
        """
        async with self._background_lock:
            entries = list(self._background_processes.get(workspace_id, {}).values())
        if not entries:
            return
        kill_attempted = False
        try:
            info = (
                self._get_cached(workspace_id)
                if self._get_cached is not None
                else None
            )
        except ValueError:
            # Registry cache miss (``WorkspaceRegistry.get_cached``
            # raises ``ValueError("... not found")`` for absent ids,
            # e.g. evicted by ``sync_from_runtime`` or a double
            # remove). Nothing left to signal — fall through to the
            # kill-skipped path below. Narrow on purpose: unexpected
            # errors must propagate, not be silently swallowed.
            info = None
        runtime = None
        if info is not None and self._get_runtime is not None:
            try:
                runtime = self._get_runtime(workspace_id)
            except (ValueError, RuntimeError):
                # Expected resolution failures: cache miss in the
                # ``get_cached``-inside-``get_runtime`` race, or a
                # missing runtime type. Both mean nothing left to
                # signal — fall through to kill-skipped. Narrow on
                # purpose: unexpected errors must propagate.
                runtime = None
        # Mirror the facade's direct-cache lookup semantics: when the
        # workspace is unknown to the cache (or its runtime is missing /
        # has no instance), there is nothing left to signal — drop
        # tracking after logging the skip.
        if info is None or runtime is None or not info.instance_id:
            logger.warning(
                "background_processes_kill_skipped",
                workspace_id=str(workspace_id),
                reason=reason,
            )
        else:
            kill_attempted = True
            for entry in entries:
                try:
                    if await self._probe_background_pid(
                        runtime, info.instance_id, entry.pid
                    ):
                        await self._kill_background_pid(
                            runtime, info.instance_id, entry.pid, "TERM"
                        )
                except Exception:
                    logger.exception(
                        "background_process_kill_failed",
                        workspace_id=str(workspace_id),
                        process_id=entry.process_id,
                        reason=reason,
                    )
            # Grace between TERM and KILL (the single-stop protocol).
            elapsed = 0.0
            while elapsed <= _BACKGROUND_STOP_GRACE_S:
                try:
                    alive = [
                        entry
                        for entry in entries
                        if await self._probe_background_pid(
                            runtime, info.instance_id, entry.pid
                        )
                    ]
                except Exception:
                    logger.exception(
                        "background_process_kill_failed",
                        workspace_id=str(workspace_id),
                        reason=reason,
                    )
                    alive = list(entries)
                if not alive:
                    break
                await asyncio.sleep(_BACKGROUND_STOP_POLL_S)
                elapsed += _BACKGROUND_STOP_POLL_S
            for entry in entries:
                try:
                    if await self._probe_background_pid(
                        runtime, info.instance_id, entry.pid
                    ):
                        await self._kill_background_pid(
                            runtime, info.instance_id, entry.pid, "KILL"
                        )
                except Exception:
                    logger.exception(
                        "background_process_kill_failed",
                        workspace_id=str(workspace_id),
                        process_id=entry.process_id,
                        reason=reason,
                    )
            # Final verification: log survivors instead of silently
            # dropping them — a setsid grandchild with its own session
            # can escape even the group kill.
            for entry in entries:
                try:
                    if await self._probe_background_pid(
                        runtime, info.instance_id, entry.pid
                    ):
                        logger.warning(
                            "background_process_survived_kill_all",
                            workspace_id=str(workspace_id),
                            process_id=entry.process_id,
                            pid=entry.pid,
                            reason=reason,
                        )
                except Exception:
                    logger.exception(
                        "background_process_kill_failed",
                        workspace_id=str(workspace_id),
                        process_id=entry.process_id,
                        reason=reason,
                    )
        async with self._background_lock:
            self._background_processes.pop(workspace_id, None)
        logger.info(
            "background_processes_killed",
            workspace_id=str(workspace_id),
            count=len(entries),
            reason=reason,
            kill_attempted=kill_attempted,
        )
