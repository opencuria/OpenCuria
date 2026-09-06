"""Central business logic for workspace management.

The runner is a "dumb executor" — it runs lifecycle, terminal, file,
and harness exec operations requested by the backend.  All agentic
knowledge (providers, tools, permissions, prompts) lives in the
backend harness.

The runner has no local database — all workspace state is derived from
the runtime backends (Docker, QEMU/KVM) and cached in memory.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import shlex
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import structlog

from .config import RunnerSettings
from .models import DesktopReleaseResult, DesktopSession, WorkspaceInfo
from .runtime.base import ImageArtifactInfo, PtyHandle, RuntimeBackend, WorkspaceConfig

logger = structlog.get_logger(__name__)

FILE_READ_DEFAULT_MAX_SIZE = 5 * 1024 * 1024  # 5 MB
FILE_READ_ABSOLUTE_MAX_SIZE = 100 * 1024 * 1024  # 100 MB
FILE_UPLOAD_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
FIND_FILES_DEFAULT_LIMIT = 50
FIND_FILES_PRUNE_NAMES = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    ".next",
)
_FIND_FILES_QUERY_RE = re.compile(r"^[A-Za-z0-9_/:.+-]*$")
_FIND_FILES_SUCCESS_EXIT_CODES = {0, 1, 141}
_SHELL_OPERATOR_TOKENS = {
    "|",
    "||",
    "&",
    "&&",
    ";",
    ";;",
    "(",
    ")",
    "<",
    "<<",
    "<<<",
    ">",
    ">>",
    ">|",
    "&>",
    "&>>",
}
_REDIRECTION_RE = re.compile(r"^\d*(?:>>?|<<?|<>|>&|<&|&>>?)(?:\d+|[^\s].*)?$")

WORKSPACE_CREDENTIAL_DIR = "/root/.opencuria-credentials"
WORKSPACE_CREDENTIAL_MANIFEST = "/root/.opencuria-credentials/manifest"
WORKSPACE_CREDENTIAL_ENV_FILE = "/root/.opencuria-env.sh"
WORKSPACE_CREDENTIAL_PROFILE_D = "/etc/profile.d/opencuria-env.sh"
WORKSPACE_CREDENTIAL_BASHRC = "/root/.bashrc"
WORKSPACE_CREDENTIAL_BASHRC_LINE = (
    "test -f /root/.opencuria-env.sh && . /root/.opencuria-env.sh"
)
WORKSPACE_CREDENTIAL_ENVIRONMENT = "/etc/environment"
WORKSPACE_CREDENTIAL_ENVIRONMENT_START = "# OPENCURIA_CREDENTIALS_START"
WORKSPACE_CREDENTIAL_ENVIRONMENT_END = "# OPENCURIA_CREDENTIALS_END"

DESKTOP_DISPLAY = ":1"
DESKTOP_HOME = "/root"
DEFAULT_DESKTOP_WIDTH = 1920
DEFAULT_DESKTOP_HEIGHT = 1080
MIN_DESKTOP_WIDTH = 800
MAX_DESKTOP_WIDTH = 3840
MIN_DESKTOP_HEIGHT = 600
MAX_DESKTOP_HEIGHT = 2160
COMPUTER_USE_RECORD_DIR = "/workspace/.opencuria/computeruse"
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

BACKGROUND_PROCESS_DIR = "/workspace/.opencuria/processes"
_BACKGROUND_PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BACKGROUND_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKGROUND_STOP_GRACE_S = 2.0
_BACKGROUND_STOP_POLL_S = 0.2


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


@dataclass
class TerminalSession:
    """Runtime PTY handle for an interactive terminal session."""

    handle: PtyHandle
    runtime: RuntimeBackend


class WorkspaceService:
    """Orchestrates workspace lifecycle and command execution.

    This is the *single* business-logic layer.  It is intentionally
    agnostic of the transport (WebSocket) and supports multiple
    runtime backends (Docker, QEMU/KVM) simultaneously.

    State management:
        - Each runtime backend is the point of truth for its workspaces.
        - An in-memory ``_cache`` dict maps ``workspace_id`` →
          ``WorkspaceInfo`` for fast lookups.
        - On startup, ``sync_from_runtime()`` rebuilds the cache by
          querying all registered runtimes.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend],
        settings: RunnerSettings,
    ) -> None:
        self._runtimes = runtimes
        self._settings = settings
        self._cache: dict[uuid.UUID, WorkspaceInfo] = {}
        self._terminals: dict[str, TerminalSession] = {}
        self._desktop_sessions: dict[uuid.UUID, DesktopSession] = {}
        self._desktop_recordings: dict[tuple[uuid.UUID, str], tuple[int, str]] = {}
        # Limit concurrent file-read SSH channels per workspace to avoid
        # exhausting the SSH server's MaxSessions limit (default: 10).
        # Each read_file call opens at most 1 SSH channel, so a limit of 4
        # keeps peak channel usage well below 10.
        self._file_read_semaphores: dict[uuid.UUID, asyncio.Semaphore] = {}
        # Self-healing: tracks when each workspace was first found unreachable.
        # Cleared once the workspace becomes reachable again.
        self._unreachable_since: dict[uuid.UUID, float] = {}
        # Background processes: workspace-scoped detached processes.
        # The runner owns liveness (in-memory); the backend owns list/history.
        self._background_processes: dict[uuid.UUID, dict[str, BackgroundProcess]] = {}
        self._background_lock = asyncio.Lock()

    # -- background processes --------------------------------------------------

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
    ) -> str:
        """Build a detached start shell for a background process.

        The wrapper sources the persistent credential env file, applies
        per-process env overrides, then runs the command detached via
        ``setsid`` and records the exit code in *exit_path*.

        The command runs in a subshell so shell-terminating commands
        (e.g. ``exit 3``) only terminate the subshell and the outer
        shell still writes ``$?`` to the exit file.
        """
        env_assignments = " ".join(
            f"{key}={shlex.quote(str(value))}"
            for key, value in (extra_env or {}).items()
            if _BACKGROUND_ENV_KEY_RE.match(str(key))
        )
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi"
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

    async def start_background_process(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """Start a detached background process inside a workspace.

        Args:
            workspace_id: Target workspace.
            process_id: Backend-assigned unique id (used for log/exit files).
            command: Shell command to run detached (non-empty).
            workdir: Working directory, must be under ``/workspace``.
            env: Optional per-process environment overrides.
            name: Optional human-readable process name.

        Returns:
            Dict with ``process_id``, ``pid``, ``log_path``, ``exit_path``.
        """
        cleaned_process_id = self._sanitize_process_id(process_id)
        if not (command or "").strip():
            raise ValueError("command must not be empty")
        safe_workdir = self._sanitize_path(workdir)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        extra_env = dict(env or {})
        for key in extra_env:
            if not _BACKGROUND_ENV_KEY_RE.match(str(key)):
                raise ValueError(f"Invalid env key: {key!r}")

        log_path = f"{BACKGROUND_PROCESS_DIR}/{cleaned_process_id}.log"
        exit_path = f"{BACKGROUND_PROCESS_DIR}/{cleaned_process_id}.exit"
        start_shell = self._build_background_start_shell(
            command.strip(), log_path, exit_path, extra_env
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
            log_path=log_path,
            exit_path=exit_path,
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
            "log_path": log_path,
            "exit_path": exit_path,
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
            return {
                "process_id": entry.process_id,
                "status": "running",
                "exit_code": None,
                "pid": entry.pid,
            }
        exit_code = await self._read_background_exit_code(
            runtime, instance_id, entry.exit_path
        )
        if exit_code is None:
            return {
                "process_id": entry.process_id,
                "status": "unknown",
                "exit_code": None,
                "pid": entry.pid,
            }
        return {
            "process_id": entry.process_id,
            "status": "exited",
            "exit_code": exit_code,
            "pid": entry.pid,
        }

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
        """Best-effort kill of every tracked process for a workspace."""
        async with self._background_lock:
            entries = list(self._background_processes.get(workspace_id, {}).values())
        if not entries:
            return
        try:
            info = self._cache.get(workspace_id)
            if info is None:
                return
            runtime = self._runtimes.get(info.runtime_type)
            if runtime is None or not info.instance_id:
                return
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
        finally:
            async with self._background_lock:
                self._background_processes.pop(workspace_id, None)
        logger.info(
            "background_processes_killed",
            workspace_id=str(workspace_id),
            count=len(entries),
            reason=reason,
        )

    # -- cache management --------------------------------------------------

    async def sync_from_runtime(self) -> None:
        """Rebuild the in-memory cache from live runtime state.

        Called at startup and can be called periodically to reconcile
        the cache with actual runtime state (e.g. workspaces killed
        externally).  Queries all registered runtime backends.
        """
        new_cache: dict[uuid.UUID, WorkspaceInfo] = {}

        for runtime_type, runtime in self._runtimes.items():
            infos = await runtime.list_workspaces()
            for info in infos:
                try:
                    ws_id = uuid.UUID(info.workspace_id)
                except ValueError:
                    logger.warning(
                        "skipping_invalid_workspace_id",
                        raw_id=info.workspace_id,
                    )
                    continue

                existing = self._cache.get(ws_id)

                new_cache[ws_id] = WorkspaceInfo(
                    workspace_id=ws_id,
                    instance_id=info.instance_id,
                    status=info.status,
                    runtime_type=runtime_type,
                    created_at=(
                        existing.created_at if existing else datetime.now(timezone.utc)
                    ),
                )

        # Preserve "creating" entries that are not yet visible to the runtime.
        # A workspace in the "creating" state has been registered by the service
        # layer but runtime.create_workspace() is still in progress (e.g. the
        # QEMU VM is booting).  Dropping it from the cache would cause the
        # next heartbeat to omit it and the backend to mark it as failed.
        for ws_id, existing in self._cache.items():
            if existing.status == "creating" and ws_id not in new_cache:
                new_cache[ws_id] = existing

        self._cache = new_cache
        logger.info(
            "cache_synced_from_runtime",
            workspace_count=len(self._cache),
        )

    def _get_cached(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Look up a workspace in the cache or raise."""
        info = self._cache.get(workspace_id)
        if info is None:
            raise ValueError(f"Workspace {workspace_id} not found")
        return info

    def _get_runtime(self, workspace_id: uuid.UUID) -> RuntimeBackend:
        """Return the runtime backend for a workspace."""
        info = self._get_cached(workspace_id)
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' not available for "
                f"workspace {workspace_id}"
            )
        return runtime

    def _get_runtime_by_type(self, runtime_type: str) -> RuntimeBackend:
        """Return the runtime backend by type name."""
        runtime = self._runtimes.get(runtime_type)
        if runtime is None:
            raise RuntimeError(f"Runtime '{runtime_type}' is not enabled")
        return runtime

    @property
    def supported_runtimes(self) -> list[str]:
        """Return the list of enabled runtime type names."""
        return list(self._runtimes.keys())

    # -- command execution helpers ---------------------------------------------

    def _normalise_command_args(self, raw_args: list[str] | str) -> list[str]:
        """Return command args suitable for runtime execution.

        Commands are primarily modelled as argv lists. However, configure
        commands are sometimes authored with shell operators (e.g. ``|``,
        ``&&``) split into individual args. Such operators are treated as
        literal argv tokens by Docker/SSH exec and therefore fail.

        To keep backend data backwards-compatible, detect shell operators and
        redirections (including forms with attached targets like
        ``2>/dev/null``) and route execution through ``bash -lc`` with a safely
        re-constructed command string.
        """
        if isinstance(raw_args, str):
            return ["bash", "-lc", raw_args]

        args = [str(arg) for arg in raw_args]
        if (
            len(args) >= 2
            and args[0] in {"bash", "sh"}
            and args[1]
            in {
                "-c",
                "-lc",
            }
        ):
            return args

        if not any(
            token in _SHELL_OPERATOR_TOKENS or _REDIRECTION_RE.match(token)
            for token in args
        ):
            return args

        command_str = " ".join(
            token
            if token in _SHELL_OPERATOR_TOKENS or _REDIRECTION_RE.match(token)
            else shlex.quote(token)
            for token in args
        )
        return ["bash", "-lc", command_str]

    async def _exec_command(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> tuple[int, str]:
        """Execute a structured command dict inside a workspace.

        Args:
            runtime: The runtime backend to use.
            instance_id: Runtime-specific instance ID.
            command: Dict with keys ``args``, ``workdir``, ``env``,
                ``description``.

        Returns:
            Tuple of (exit_code, output).
        """
        wrapped_command = self._wrap_command_with_persistent_env(command)
        command_args = self._normalise_command_args(wrapped_command["args"])
        return await runtime.exec_command_wait(
            instance_id,
            command=command_args,
            workdir=wrapped_command.get("workdir"),
            env=wrapped_command.get("env"),
        )

    async def _exec_command_stream(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> AsyncIterator[str]:
        """Execute a structured command dict and stream output lines.

        Args:
            runtime: The runtime backend to use.
            instance_id: Runtime-specific instance ID.
            command: Dict with keys ``args``, ``workdir``, ``env``,
                ``description``.

        Yields:
            Raw output lines from the command.
        """
        wrapped_command = self._wrap_command_with_persistent_env(command)
        command_args = self._normalise_command_args(wrapped_command["args"])
        async for line in runtime.exec_command(
            instance_id,
            command=command_args,
            workdir=wrapped_command.get("workdir"),
            env=wrapped_command.get("env"),
        ):
            yield line

    # -- persistent workspace credentials -------------------------------------

    @staticmethod
    def _build_tar_entries(
        files: list[tuple[str, bytes, int]],
    ) -> bytes:
        """Build a tar archive containing multiple files."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for filename, content, mode in files:
                info = tarfile.TarInfo(name=filename)
                info.size = len(content)
                info.mode = mode
                tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    @staticmethod
    def _credential_path_helpers() -> list[str]:
        """Return shell helper functions used by inject and remove scripts."""

        return [
            'opencuria_credential_home="${HOME:-/root}"',
            "opencuria_resolve_credential_path() {",
            '  raw_path="$1"',
            "  tilde_prefix='~/'",
            "  home_prefix='${HOME}/'",
            '  if [ "$raw_path" = "~" ] || [ "$raw_path" = "${HOME}" ] || [ "$raw_path" = "${opencuria_credential_home}" ]; then',
            '    printf "%s\\n" "$opencuria_credential_home"',
            "    return",
            "  fi",
            '  if [ "${raw_path#"$tilde_prefix"}" != "$raw_path" ]; then',
            '    printf "%s/%s\\n" "$opencuria_credential_home" "${raw_path#"$tilde_prefix"}"',
            "    return",
            "  fi",
            '  if [ "${raw_path#"$home_prefix"}" != "$raw_path" ]; then',
            '    printf "%s/%s\\n" "$opencuria_credential_home" "${raw_path#"$home_prefix"}"',
            "    return",
            "  fi",
            '  if [ "${raw_path#/}" != "$raw_path" ]; then',
            '    printf "%s\\n" "$raw_path"',
            "    return",
            "  fi",
            '  printf "%s/%s\\n" "$opencuria_credential_home" "$raw_path"',
            "}",
            "opencuria_strip_environment_block() {",
            f"  env_file={shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}",
            '  if [ ! -f "$env_file" ]; then',
            "    return",
            "  fi",
            "  tmp_env=$(mktemp)",
            f"  awk '/{WORKSPACE_CREDENTIAL_ENVIRONMENT_START}/{{skip=1}} "
            f"/{WORKSPACE_CREDENTIAL_ENVIRONMENT_END}/{{skip=0; next}} !skip' "
            '"$env_file" > "$tmp_env" || true',
            '  cat "$tmp_env" > "$env_file"',
            '  rm -f "$tmp_env"',
            "}",
        ]

    def _wrap_command_with_persistent_env(self, command: dict) -> dict:
        """Source persistent workspace credentials before running a command."""

        normalised_args = self._normalise_command_args(command["args"])
        extra_env = command.get("env") or {}
        extra_exports = "; ".join(
            f"export {key}={shlex.quote(str(value))}"
            for key, value in extra_env.items()
        )
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi"
        )
        if extra_exports:
            source = f"{source}; {extra_exports}"
        wrapper = f'{source}; exec "$@"'
        return {
            **command,
            "args": [
                "bash",
                "-lc",
                wrapper,
                "opencuria-exec",
                *normalised_args,
            ],
            "env": {},
        }

    async def remove_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        log,
    ) -> None:
        """Idempotently remove persisted credential material from a workspace."""

        cleanup_script = "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                *self._credential_path_helpers(),
                f"manifest={shlex.quote(WORKSPACE_CREDENTIAL_MANIFEST)}",
                'if [ -f "$manifest" ]; then',
                '  while IFS= read -r file_path || [ -n "$file_path" ]; do',
                '    [ -z "$file_path" ] && continue',
                '    rm -f "$(opencuria_resolve_credential_path "$file_path")"',
                '  done < "$manifest"',
                "fi",
                "opencuria_strip_environment_block",
                f"rm -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} "
                f"{shlex.quote(WORKSPACE_CREDENTIAL_PROFILE_D)}",
                f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)} ]; then",
                "  tmp_bashrc=$(mktemp)",
                f"  grep -vxF {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f'{shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)} > "$tmp_bashrc" || true',
                f'  cat "$tmp_bashrc" > {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}',
                '  rm -f "$tmp_bashrc"',
                "fi",
                "rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519_*",
                "rm -f /root/.ssh/config /root/.ssh/known_hosts",
                f"rm -rf {shlex.quote(WORKSPACE_CREDENTIAL_DIR)}",
                "rm -rf /tmp/opencuria-op-*",
                "find /var/lib/cloud/instances -type f "
                "\\( -name 'user-data.txt' -o -name 'user-data.txt.i' "
                "-o -name 'cloud-config.txt' -o -path '*/scripts/runcmd' \\) "
                "-delete 2>/dev/null || true",
            ]
        )
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", cleanup_script],
            workdir="/root",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to remove workspace credentials: {output}")
        log.info("workspace_credentials_removed")

    async def inject_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        files: list[dict[str, Any]] | None,
        ssh_keys: list[str] | None,
        log,
    ) -> bool:
        """Persist credentials on the workspace disk, replacing any previous set.

        Returns True when credential material was written, False when the
        workspace has no attached secrets after a clean remove.
        """

        await self.remove_workspace_credentials(runtime, instance_id, log)

        env_vars = env_vars or {}
        credential_files = files or []
        ssh_keys = ssh_keys or []
        if not env_vars and not credential_files and not ssh_keys:
            return False

        staging_dir = WORKSPACE_CREDENTIAL_DIR
        files_dir = f"{staging_dir}/files"
        ssh_dir = f"{staging_dir}/ssh"
        install_path = f"{staging_dir}/install.sh"
        archive_files: list[tuple[str, bytes, int]] = []
        installed_paths: list[str] = [
            WORKSPACE_CREDENTIAL_ENV_FILE,
            WORKSPACE_CREDENTIAL_PROFILE_D,
            WORKSPACE_CREDENTIAL_MANIFEST,
        ]
        helper_lines = self._credential_path_helpers()
        install_lines = [
            "#!/bin/sh",
            "set -eu",
            *helper_lines,
            f"mkdir -p {shlex.quote(WORKSPACE_CREDENTIAL_DIR)} /root/.ssh /etc/profile.d",
            f"install -m 600 {shlex.quote(staging_dir + '/env.sh')} "
            f"{shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}",
            f"install -m 644 {shlex.quote(staging_dir + '/profile.d.sh')} "
            f"{shlex.quote(WORKSPACE_CREDENTIAL_PROFILE_D)}",
            "opencuria_strip_environment_block",
            f"printf '%s\\n' {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT_START)} "
            f">> {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}",
        ]

        env_export_lines = [
            "#!/bin/sh",
            'export PATH="/root/.local/bin:$PATH"',
        ]
        for key, value in env_vars.items():
            env_export_lines.append(f"export {key}={shlex.quote(str(value))}")
            install_lines.append(
                "printf '%s\\n' "
                f"{shlex.quote(f'{key}={value}')} "
                f">> {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}"
            )
        install_lines.append(
            f"printf '%s\\n' {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT_END)} "
            f">> {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}"
        )
        archive_files.append(
            ("env.sh", ("\n".join(env_export_lines) + "\n").encode("utf-8"), 0o600)
        )
        archive_files.append(
            (
                "profile.d.sh",
                (f"{WORKSPACE_CREDENTIAL_BASHRC_LINE}\n").encode("utf-8"),
                0o644,
            )
        )

        install_lines.extend(
            [
                f"touch {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}",
                f"if ! grep -qxF {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f"{shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}; then",
                f"  printf '%s\\n' {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f">> {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}",
                "fi",
            ]
        )

        for index, credential_file in enumerate(credential_files, start=1):
            source_relpath = f"files/credential_{index}"
            source_abspath = f"{files_dir}/credential_{index}"
            target_path = str(credential_file["target_path"])
            mode = int(credential_file.get("mode", 0o600))
            content = str(credential_file.get("content", ""))
            archive_files.append((source_relpath, content.encode("utf-8"), 0o600))
            install_lines.extend(
                [
                    "target_path=$(opencuria_resolve_credential_path "
                    f"{shlex.quote(target_path)})",
                    'mkdir -p "$(dirname "$target_path")"',
                    f'install -m {mode:o} {shlex.quote(source_abspath)} "$target_path"',
                ]
            )
            installed_paths.append(target_path)

        if ssh_keys:
            config_lines = [
                "Host *",
                "    StrictHostKeyChecking accept-new",
                "    UserKnownHostsFile /root/.ssh/known_hosts",
                "    IdentitiesOnly yes",
            ]
            for index, key_pem in enumerate(ssh_keys):
                key_name = "id_ed25519" if index == 0 else f"id_ed25519_{index + 1}"
                archive_files.append(
                    (
                        f"ssh/{key_name}",
                        key_pem.rstrip().encode("utf-8") + b"\n",
                        0o600,
                    )
                )
                install_lines.append(
                    f"install -m 600 {shlex.quote(ssh_dir + '/' + key_name)} "
                    f"{shlex.quote('/root/.ssh/' + key_name)}"
                )
                config_lines.append(f"    IdentityFile /root/.ssh/{key_name}")
                installed_paths.append(f"/root/.ssh/{key_name}")
            archive_files.append(("ssh/known_hosts", b"", 0o600))
            archive_files.append(
                (
                    "ssh/config",
                    ("\n".join(config_lines) + "\n").encode("utf-8"),
                    0o600,
                )
            )
            install_lines.extend(
                [
                    f"install -m 600 {shlex.quote(ssh_dir + '/config')} /root/.ssh/config",
                    f"install -m 600 {shlex.quote(ssh_dir + '/known_hosts')} "
                    "/root/.ssh/known_hosts",
                ]
            )
            installed_paths.extend(["/root/.ssh/config", "/root/.ssh/known_hosts"])

        manifest = "".join(f"{path}\n" for path in installed_paths)
        archive_files.append(("manifest", manifest.encode("utf-8"), 0o600))
        archive_files.append(
            ("install.sh", ("\n".join(install_lines) + "\n").encode("utf-8"), 0o700)
        )

        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=[
                "mkdir",
                "-p",
                staging_dir,
                f"{staging_dir}/files",
                f"{staging_dir}/ssh",
            ],
            workdir="/root",
        )
        if exit_code != 0:
            raise RuntimeError(
                f"Failed to create credential staging directory: {output}"
            )

        archive_data = self._build_tar_entries(archive_files)
        await runtime.put_archive(instance_id, staging_dir, archive_data)
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", f". {shlex.quote(install_path)}"],
            workdir="/root",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to inject workspace credentials: {output}")

        log.info(
            "workspace_credentials_injected",
            has_env=bool(env_vars),
            file_count=len(credential_files),
            ssh_key_count=len(ssh_keys),
        )
        return True

    # -- workspace lifecycle ---------------------------------------------------

    async def create_workspace(
        self,
        repos: list[str],
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
        workspace_id: uuid.UUID | None = None,
        runtime_type: str = "docker",
        image_tag: str | None = None,
        base_image_path: str | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Create a new workspace, inject credentials, and clone repos.

        Args:
            repos: Git repository URLs to clone into the workspace.
            env_vars: Environment variables persisted in the workspace
                until a controlled stop.
            files: Credential files persisted in the workspace until a
                controlled stop.
            ssh_keys: SSH private keys persisted in the workspace until a
                controlled stop.
            workspace_id: Workspace ID assigned by the backend.
            runtime_type: Which runtime to use (``"docker"`` or ``"qemu"``).

        Returns the workspace UUID and whether credentials were injected.
        """
        if workspace_id is None:
            workspace_id = uuid.uuid4()

        runtime = self._get_runtime_by_type(runtime_type)

        log = logger.bind(
            workspace_id=str(workspace_id),
            runtime=runtime_type,
        )
        log.info("creating_workspace", repos=repos)

        # Build runtime-appropriate config
        if runtime_type == "docker":
            if not image_tag:
                raise RuntimeError("Docker workspace creation requires an image tag")
            volume_name = f"opencuria-workspace-{workspace_id}"
            config = WorkspaceConfig(
                workspace_id=str(workspace_id),
                image=image_tag,
                env_vars={},
                volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
                network=self._settings.docker_network,
                labels={"opencuria.workspace-id": str(workspace_id)},
            )
        else:
            if not base_image_path:
                raise RuntimeError("QEMU workspace creation requires a base image path")
            # QEMU — image is base QCOW2 path, no Docker volumes
            config = WorkspaceConfig(
                workspace_id=str(workspace_id),
                image=base_image_path,
                env_vars={},
                network=self._settings.qemu_network,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                labels={"opencuria.workspace-id": str(workspace_id)},
            )

        # Register a "creating" cache entry *before* calling
        # runtime.create_workspace() so that heartbeat syncs during VM boot
        # (which can take 60 s+ for QEMU) do not drop this workspace and
        # cause the backend to mark it as failed.  instance_id is unknown at
        # this point — it will be updated once create_workspace() returns.
        self._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id="",
            status="creating",
            runtime_type=runtime_type,
        )

        try:
            instance_id = await runtime.create_workspace(config)
        except Exception:
            log.exception("workspace_creation_failed")
            self._cache.pop(workspace_id, None)
            raise

        # Update cache with the real instance_id now that the runtime has assigned it.
        self._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id=instance_id,
            status="creating",
            runtime_type=runtime_type,
        )

        credentials_present = await self.inject_workspace_credentials(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

        for repo_url in repos:
            log.info("cloning_repo", repo=repo_url)
            exit_code, output = await self._exec_command(
                runtime,
                instance_id,
                {
                    "args": ["git", "clone", repo_url],
                    "workdir": "/workspace",
                    "env": {},
                    "description": f"Clone repository: {repo_url}",
                },
            )
            if exit_code != 0:
                log.warning("repo_clone_failed", repo=repo_url, output=output)
            else:
                log.info("repo_cloned", repo=repo_url)

        self._cache[workspace_id].status = "running"

        log.info("workspace_ready", credentials_present=credentials_present)
        return workspace_id, credentials_present

    async def stop_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Remove credentials then stop a running workspace.

        Returns False because credentials are stripped before the instance
        is stopped. Raises if credential removal fails so the workspace
        stays running with secrets still present.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        await self.remove_workspace_credentials(runtime, info.instance_id, log)
        await self._kill_all_background_processes(workspace_id, reason="stop")
        await self.release_desktop(
            workspace_id, holder=DESKTOP_HOLDER_VIEWER, force=True
        )
        await runtime.stop_workspace(info.instance_id)
        info.status = "exited"
        log.info("workspace_stopped")
        return False

    async def resume_workspace(
        self,
        workspace_id: uuid.UUID,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Resume a stopped workspace and re-inject persistent credentials."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        if info.runtime_type == "qemu":
            if (
                qemu_vcpus is None
                or qemu_memory_mb is None
                or qemu_disk_size_gb is None
            ):
                raise RuntimeError("Missing QEMU resource settings for resume")
            await runtime.reconfigure_workspace(
                info.instance_id,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                restart=False,
            )

        await runtime.start_workspace(info.instance_id)
        info.status = "running"
        credentials_present = await self.inject_workspace_credentials(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        log.info("workspace_resumed", credentials_present=credentials_present)
        return credentials_present

    async def inject_credentials(
        self,
        workspace_id: uuid.UUID,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Replace persistent credentials on a running workspace."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        return await self.inject_workspace_credentials(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

    async def update_workspace_resources(
        self,
        workspace_id: uuid.UUID,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> None:
        """Reconfigure resources for an existing QEMU workspace."""
        info = self._get_cached(workspace_id)
        if info.runtime_type != "qemu":
            raise RuntimeError(
                "Workspace runtime does not support resource reconfiguration"
            )
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        await runtime.reconfigure_workspace(
            info.instance_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            restart=True,
        )
        info.status = "running"

    async def remove_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace and clean up resources."""
        log = logger.bind(workspace_id=str(workspace_id))
        await self._kill_all_background_processes(workspace_id, reason="remove")
        info = self._cache.pop(workspace_id, None)
        self._desktop_sessions.pop(workspace_id, None)
        self._desktop_recordings = {
            key: value
            for key, value in self._desktop_recordings.items()
            if key[0] != workspace_id
        }

        if info and info.instance_id:
            runtime = self._runtimes.get(info.runtime_type)
            if runtime:
                await runtime.remove_workspace(info.instance_id)

        log.info("workspace_removed")

    async def cleanup_unknown_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Best-effort cleanup for a runtime workspace unknown to the backend.

        Returns ``True`` when a cached runtime instance was found and cleanup
        was attempted. Returns ``False`` when the workspace was already absent.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        await self._kill_all_background_processes(
            workspace_id, reason="cleanup_unknown"
        )
        info = self._cache.pop(workspace_id, None)
        self._unreachable_since.pop(workspace_id, None)

        if info is None:
            log.info("unknown_workspace_already_absent")
            return False

        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' not available for cleanup"
            )

        if info.instance_id:
            await runtime.remove_workspace(info.instance_id)

        log.warning(
            "unknown_workspace_cleaned",
            runtime_type=info.runtime_type,
            instance_id=info.instance_id,
        )
        return True

    # -- self-healing SSH health check -----------------------------------------

    async def _check_workspace_reachable(
        self, workspace_id: uuid.UUID, info: WorkspaceInfo
    ) -> bool:
        """Return True if the workspace responds to a lightweight exec probe.

        Uses a short timeout so the loop does not block for a long time.
        """
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None or not info.instance_id:
            return True  # cannot check — assume reachable to avoid false restarts

        try:
            exit_code, _ = await asyncio.wait_for(
                runtime.exec_command_wait(
                    info.instance_id,
                    command=["echo", "ok"],
                ),
                timeout=15,
            )
            return exit_code == 0
        except Exception:
            return False

    async def run_health_check_loop(self) -> None:
        """Periodically probe running workspaces and restart unreachable ones.

        Runs indefinitely; cancel the task to stop it.

        A workspace is restarted when it has been continuously unreachable for
        more than ``settings.ssh_unreachable_timeout`` seconds.  After a
        restart, the unreachable timer is cleared so the workspace gets a
        fresh chance to come up.
        """
        interval = self._settings.ssh_health_check_interval
        timeout = self._settings.ssh_unreachable_timeout

        log = logger.bind(loop="health_check")
        log.info(
            "health_check_loop_started",
            check_interval_s=interval,
            unreachable_timeout_s=timeout,
        )

        while True:
            try:
                await asyncio.sleep(interval)

                # Snapshot the cache — do not hold it across awaits.
                candidates = [
                    (ws_id, info)
                    for ws_id, info in self._cache.items()
                    if info.status == "running"
                ]

                for ws_id, info in candidates:
                    reachable = await self._check_workspace_reachable(ws_id, info)

                    if reachable:
                        # Clear any existing failure timer.
                        self._unreachable_since.pop(ws_id, None)
                        continue

                    # Workspace is unreachable.
                    first_failure = self._unreachable_since.setdefault(
                        ws_id, time.monotonic()
                    )
                    unreachable_for = time.monotonic() - first_failure

                    log.warning(
                        "workspace_unreachable",
                        workspace_id=str(ws_id),
                        unreachable_for_s=round(unreachable_for),
                        threshold_s=timeout,
                    )

                    if unreachable_for >= timeout:
                        log.error(
                            "workspace_self_healing_restart",
                            workspace_id=str(ws_id),
                            runtime=info.runtime_type,
                        )
                        try:
                            runtime = self._runtimes.get(info.runtime_type)
                            if runtime and info.instance_id:
                                await runtime.restart_workspace(info.instance_id)
                                # Reset status and clear the failure timer.
                                if ws_id in self._cache:
                                    self._cache[ws_id].status = "running"
                                self._unreachable_since.pop(ws_id, None)
                                log.info(
                                    "workspace_self_healed",
                                    workspace_id=str(ws_id),
                                )
                        except Exception:
                            log.exception(
                                "workspace_self_heal_failed",
                                workspace_id=str(ws_id),
                            )

            except asyncio.CancelledError:
                log.info("health_check_loop_stopped")
                break
            except Exception:
                log.exception("health_check_loop_error")

    async def list_workspaces(self) -> list[WorkspaceInfo]:
        """Return all known workspaces, refreshing from the runtime."""
        await self.sync_from_runtime()
        return list(self._cache.values())

    async def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Return a single workspace by ID, checking live status."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        # Refresh status from runtime
        if info.instance_id:
            try:
                status = await runtime.get_workspace_status(info.instance_id)
                info.status = status.status
            except Exception:
                info.status = "unknown"

        return info

    def get_workspace_statuses(self) -> list[dict]:
        """Return lightweight status list for heartbeat reporting.

        Reads from the in-memory cache without hitting the runtime,
        so it's fast enough for periodic heartbeats.
        """
        return [
            {
                "workspace_id": str(info.workspace_id),
                "status": info.status,
                "runtime_type": info.runtime_type,
            }
            for info in self._cache.values()
        ]

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

    async def get_workspace_heartbeat_statuses(self) -> list[dict]:
        """Return workspace heartbeat payload including live desktop sessions."""
        payload: list[dict] = []
        for info in self._cache.values():
            workspace_id = info.workspace_id
            item = {
                "workspace_id": str(workspace_id),
                "status": info.status,
                "runtime_type": info.runtime_type,
            }

            session = self._desktop_sessions.get(workspace_id)
            if session is not None:
                try:
                    if await self._is_desktop_session_live(workspace_id):
                        item["desktop"] = self._desktop_heartbeat_payload(
                            workspace_id, session
                        )
                    else:
                        self._desktop_sessions.pop(workspace_id, None)
                        item["desktop"] = None
                        logger.warning(
                            "desktop_session_pruned_from_cache",
                            workspace_id=str(workspace_id),
                        )
                except Exception:
                    self._desktop_sessions.pop(workspace_id, None)
                    item["desktop"] = None
                    logger.exception(
                        "desktop_session_health_check_failed",
                        workspace_id=str(workspace_id),
                    )

            processes: list[dict[str, Any]] = []
            for entry in self._background_processes.get(workspace_id, {}).values():
                try:
                    info_for_proc = self._cache.get(workspace_id)
                    runtime_for_proc = (
                        self._runtimes.get(info_for_proc.runtime_type)
                        if info_for_proc is not None
                        else None
                    )
                    if info_for_proc is None or runtime_for_proc is None:
                        raise RuntimeError("runtime unavailable")
                    if not info_for_proc.instance_id:
                        raise RuntimeError("no instance assigned")
                    processes.append(
                        await self._background_status_locked(
                            runtime_for_proc,
                            info_for_proc.instance_id,
                            entry,
                        )
                    )
                except Exception:
                    logger.exception(
                        "background_heartbeat_failed",
                        workspace_id=str(workspace_id),
                        process_id=entry.process_id,
                    )
                    processes.append(
                        {
                            "process_id": entry.process_id,
                            "status": "unknown",
                            "exit_code": None,
                            "pid": entry.pid,
                        }
                    )
            item["processes"] = processes

            payload.append(item)

        return payload

    async def recover_desktop_sessions_from_runtime(self) -> None:
        """Rebuild in-memory desktop sessions from live runtime state."""
        for workspace_id, info in self._cache.items():
            if info.status != "running" or workspace_id in self._desktop_sessions:
                continue

            try:
                if not await self._is_desktop_session_live(workspace_id):
                    continue
            except Exception:
                logger.exception(
                    "desktop_session_recovery_failed",
                    workspace_id=str(workspace_id),
                )
                continue

            self._desktop_sessions[workspace_id] = DesktopSession(
                workspace_id=workspace_id,
                instance_id=info.instance_id,
            )
            logger.info(
                "desktop_session_recovered",
                workspace_id=str(workspace_id),
            )

    async def get_vm_metrics(self) -> dict[str, dict[str, Any]]:
        """Collect host-observed metrics for QEMU workspaces."""
        qemu_runtime = self._runtimes.get("qemu")
        if qemu_runtime is None:
            return {}

        get_workspace_usage = getattr(qemu_runtime, "get_workspace_usage", None)
        if not callable(get_workspace_usage):
            return {}

        metrics: dict[str, dict[str, Any]] = {}
        for workspace_id, info in self._cache.items():
            if info.runtime_type != "qemu":
                continue
            try:
                usage = await get_workspace_usage(info.instance_id)
            except Exception:
                logger.exception(
                    "vm_metrics_collect_failed",
                    workspace_id=str(workspace_id),
                )
                continue

            if usage is None:
                continue

            metrics[str(workspace_id)] = usage

        return metrics

    # -- interactive terminal --------------------------------------------------

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        Persistent workspace credentials are sourced via a login shell.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if not await runtime.workspace_exists(info.instance_id):
            self._cache.pop(workspace_id, None)
            raise RuntimeError("Workspace instance no longer exists")

        terminal_command = [
            "/bin/bash",
            "-lc",
            (
                f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
                f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} "
                ">/dev/null 2>&1; fi; exec /bin/bash -l"
            ),
        ]

        handle = await runtime.exec_pty(
            info.instance_id,
            cols=cols,
            rows=rows,
            workdir="/workspace",
            env={"TERM": "xterm-256color"},
            command=terminal_command,
        )

        terminal_id = str(uuid.uuid4())
        self._terminals[terminal_id] = TerminalSession(
            handle=handle,
            runtime=runtime,
        )
        log.info("terminal_started", terminal_id=terminal_id)
        return terminal_id

    async def read_terminal(self, terminal_id: str) -> AsyncIterator[bytes]:
        """Yield raw bytes from the PTY as they arrive.

        Stops when the PTY is closed or returns empty data (EOF).
        """
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime

        while not handle.closed:
            data = await runtime.pty_read(handle)
            if not data:
                break
            yield data

    async def write_terminal(self, terminal_id: str, data: bytes) -> None:
        """Write raw bytes (user input) to the PTY stdin."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime
        await runtime.pty_write(handle, data)

    async def resize_terminal(self, terminal_id: str, cols: int, rows: int) -> None:
        """Resize the PTY window."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime
        await runtime.pty_resize(handle, cols, rows)

    async def close_terminal(self, terminal_id: str) -> None:
        """Close a PTY session and release resources."""
        entry = self._terminals.pop(terminal_id, None)
        if entry is None:
            return
        await entry.runtime.pty_close(entry.handle)
        logger.info("terminal_closed", terminal_id=terminal_id)

    # -- desktop session (KasmVNC) -----------------------------------------

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
        """
        geometry = f"{width}x{height}"
        return (
            "set -e\n"
            "export DISPLAY=:1\n"
            "export HOME=/root\n"
            "mkdir -p /root/.vnc\n"
            "rm -f /tmp/.X1-lock /tmp/.X11-unix/X1\n"
            f"/usr/bin/Xvnc :1 -geometry {geometry} -depth 24 "
            "-rfbport 5901 -SecurityTypes None -disableBasicAuth "
            "-websocketPort 6901 -httpd /usr/share/kasmvnc/www "
            "-interface 0.0.0.0 -AlwaysShared -AcceptKeyEvents "
            "-AcceptPointerEvents -SendCutText -AcceptCutText "
            "-AcceptSetDesktopSize=0 "
            ">>/root/.vnc/server.log 2>&1 &\n"
            "for _ in $(seq 1 120); do\n"
            "  if [ -e /tmp/.X11-unix/X1 ]; then\n"
            "    /root/.vnc/xstartup >>/root/.vnc/xstartup.log 2>&1 &\n"
            '    echo "Desktop session started on :1 (ws port 6901)"\n'
            "    exit 0\n"
            "  fi\n"
            "  sleep 0.25\n"
            "done\n"
            'echo "Desktop session failed to start" >&2\n'
            "tail -n 50 /root/.vnc/server.log >&2 || true\n"
            "exit 1\n"
        )

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
        """
        existing = self._desktop_sessions.get(workspace_id)
        preserved_viewer = existing.viewer_held if existing is not None else False
        preserved_runs = (
            set(existing.computeruse_run_ids) if existing is not None else set()
        )
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
            )
            self._desktop_sessions[workspace_id] = recovered
            logger.info(
                "desktop_session_recovered_on_start",
                workspace_id=str(workspace_id),
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
        )
        self._desktop_sessions[workspace_id] = session
        log.info("desktop_started", port=session.port)
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
        """Ensure the desktop process and acquire a viewer or computer-use lease."""
        kind = self._parse_desktop_holder(holder)
        session = await self.ensure_desktop_process(
            workspace_id,
            width=width,
            height=height,
        )
        if kind == DESKTOP_HOLDER_VIEWER:
            session.viewer_held = True
        else:
            session.computeruse_run_ids.add(self._sanitize_run_id(str(run_id or "")))
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
        """
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
            await self._stop_desktop_process(workspace_id, interrupt_recordings=True)
            return DesktopReleaseResult(
                stopped=True,
                process_alive=False,
                viewer_held=False,
                computer_use_active=False,
            )

        kind = self._parse_desktop_holder(holder)
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

        await self._stop_desktop_process(workspace_id, interrupt_recordings=True)
        return DesktopReleaseResult(
            stopped=True,
            process_alive=False,
            viewer_held=False,
            computer_use_active=False,
        )

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
    ) -> None:
        """Kill Xvnc and drop the cached desktop session."""
        log = logger.bind(workspace_id=str(workspace_id))
        if interrupt_recordings:
            await self._interrupt_desktop_recordings(workspace_id)

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
        """Return environment variables for desktop X11 commands."""
        return {"HOME": DESKTOP_HOME, "DISPLAY": DESKTOP_DISPLAY}

    @staticmethod
    def _sanitize_run_id(run_id: str) -> str:
        """Validate a computer-use recording run identifier."""
        if not run_id or not _RUN_ID_RE.match(run_id):
            raise ValueError(f"Invalid run_id: {run_id}")
        return run_id

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
        """Execute a desktop I/O action inside the workspace display."""
        payload = args or {}
        log = logger.bind(workspace_id=str(workspace_id), desktop_action=action)

        if action == "ensure":
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

        if action == "hold":
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

        if action == "release":
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

        if action not in {"ensure", "hold", "release"}:
            await self._require_desktop_live(workspace_id)

        if action == "display_info":
            width, height = await self._get_desktop_geometry(workspace_id)
            return {
                "ok": True,
                "display": DESKTOP_DISPLAY,
                "width": width,
                "height": height,
            }

        if action == "screenshot":
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
            ffmpeg_cmd = (
                f"ffmpeg -y -f x11grab -video_size {width}x{height} "
                f"-draw_mouse 1 -i {DESKTOP_DISPLAY} -frames:v 1 "
                f"{crop_filter}"
                "-f image2 -vcodec mjpeg pipe:1 2>/dev/null | base64 -w0"
            )
            exit_code, output = await self._exec_desktop_shell(workspace_id, ffmpeg_cmd)
            if exit_code != 0 or not output.strip():
                log.error("desktop_screenshot_failed", exit_code=exit_code)
                raise RuntimeError("Failed to capture desktop screenshot")
            return {
                "ok": True,
                "image_b64": output.strip(),
                "mime": "image/jpeg",
                "width": result_width,
                "height": result_height,
                "text": "",
            }

        if action == "move":
            x = int(payload["x"])
            y = int(payload["y"])
            exit_code, output = await self._exec_desktop_shell(
                workspace_id,
                f"xdotool mousemove --sync {x} {y}",
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to move mouse: {output}")
            return {"ok": True}

        if action == "click":
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

        if action == "drag":
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

        if action == "scroll":
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

        if action == "type":
            text = str(payload.get("text", ""))
            if not text:
                raise ValueError("text must not be empty")
            exit_code, output = await self._exec_desktop_shell(
                workspace_id,
                f"xdotool type --delay 0 -- {shlex.quote(text)}",
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to type text: {output}")
            return {"ok": True}

        if action == "key":
            key = str(payload.get("key", "")).strip()
            if not key:
                raise ValueError("key must not be empty")
            modifiers = payload.get("modifiers") or []
            if not isinstance(modifiers, list):
                raise ValueError("modifiers must be a list")
            combo = "+".join([*modifiers, key])
            exit_code, output = await self._exec_desktop_shell(
                workspace_id,
                f"xdotool key -- {shlex.quote(combo)}",
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to send key: {output}")
            return {"ok": True}

        if action == "open_url":
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

        if action == "record_start":
            run_id = self._sanitize_run_id(str(payload.get("run_id", "")))
            record_key = (workspace_id, run_id)
            existing = self._desktop_recordings.get(record_key)
            if existing is not None:
                return {"ok": True, "path": existing[1], "run_id": run_id}

            raw_path = payload.get("path")
            if raw_path:
                record_path = self._sanitize_path(str(raw_path))
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

        if action == "record_stop":
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

        raise ValueError(f"Unknown desktop action: {action}")

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

    # -- file operations -------------------------------------------------------

    @staticmethod
    def _sanitize_path(path: str) -> str:
        """Ensure *path* is under ``/workspace`` and prevent traversal."""
        normalized = os.path.normpath(path)
        if normalized != "/workspace" and not normalized.startswith("/workspace/"):
            raise ValueError(f"Path must be under /workspace: {path}")
        return normalized

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Validate and return a safe filename for workspace uploads."""
        if not filename:
            raise ValueError("Filename must not be empty")

        if filename != os.path.basename(filename):
            raise ValueError("Filename must not contain path separators")

        if filename in {".", ".."}:
            raise ValueError("Invalid filename")

        return filename

    @staticmethod
    def _build_single_file_tar(filename: str, content: bytes) -> bytes:
        """Build a tar archive containing exactly one file."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    @staticmethod
    def _convert_archive_to_tar(content: bytes) -> bytes:
        """Convert an uploaded archive payload to a plain tar stream."""
        source = io.BytesIO(content)
        target = io.BytesIO()

        with tarfile.open(fileobj=source, mode="r:*") as src_tar:
            with tarfile.open(fileobj=target, mode="w") as dst_tar:
                for member in src_tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name.split("/"):
                        raise ValueError("Archive contains unsafe paths")
                    if member.issym() or member.islnk():
                        raise ValueError("Archive contains unsafe links")

                    extracted = None
                    if member.isfile():
                        extracted = src_tar.extractfile(member)
                    dst_tar.addfile(member, extracted)

        return target.getvalue()

    async def list_files(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> list[dict]:
        """List files and directories at *path* inside the workspace.

        Returns a list of dicts with ``name``, ``path``, ``type``, ``size``.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=[
                "find",
                safe_path,
                "-maxdepth",
                "1",
                "-mindepth",
                "1",
                "-printf",
                r"%y\t%s\t%p\n",
            ],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to list files: {output}")

        entries: list[dict] = []
        for line in output.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            file_type_char, size_str, file_path = parts
            entries.append(
                {
                    "name": os.path.basename(file_path),
                    "path": file_path,
                    "type": "directory" if file_type_char == "d" else "file",
                    "size": int(size_str) if size_str.isdigit() else 0,
                }
            )

        # Sort: directories first, then alphabetically
        entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
        return entries

    @staticmethod
    def sanitize_find_query(query: str) -> str:
        """Return a safe ``find -ipath`` query fragment.

        Only characters that the chat ``@`` mention regex allows are accepted.
        ``..`` is rejected even though ``.`` is otherwise valid.
        """
        cleaned = (query or "").strip()
        if ".." in cleaned or not _FIND_FILES_QUERY_RE.fullmatch(cleaned):
            raise ValueError("Invalid find query")
        return cleaned

    @classmethod
    def build_find_files_command(cls, query: str, limit: int) -> list[str]:
        """Build ``bash -lc`` argv that finds workspace files up to *limit*.

        Prunes common junk directories. An empty *query* lists shallower paths
        first; a non-empty query uses case-insensitive ``-ipath``.
        """
        capped = max(1, min(int(limit), FIND_FILES_DEFAULT_LIMIT))
        prune = " -o ".join(
            f"-name {shlex.quote(name)}" for name in FIND_FILES_PRUNE_NAMES
        )
        match = ""
        if query:
            match = f"-ipath {shlex.quote(f'*{query}*')} "
        pipeline = (
            f"find {shlex.quote('/workspace')} \\( {prune} \\) -prune "
            f"-o -type f {match}-printf '%d\\t%p\\n' "
            f"| sort -n | head -n {capped + 1}"
        )
        return ["bash", "-lc", pipeline]

    async def find_files(
        self,
        workspace_id: uuid.UUID,
        query: str = "",
        limit: int = FIND_FILES_DEFAULT_LIMIT,
    ) -> dict:
        """Search workspace files for mention autocomplete.

        Returns ``{"paths": [{"path", "name"}], "truncated": bool}``. Results
        are capped at ``FIND_FILES_DEFAULT_LIMIT``.
        """
        safe_query = self.sanitize_find_query(query)
        capped = max(1, min(int(limit), FIND_FILES_DEFAULT_LIMIT))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        command = self.build_find_files_command(safe_query, capped)
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=command,
            workdir="/workspace",
        )
        if exit_code not in _FIND_FILES_SUCCESS_EXIT_CODES:
            raise RuntimeError(f"Failed to find files: {output}")

        paths: list[dict] = []
        for line in output.strip().splitlines():
            parts = line.split("\t", 1)
            file_path = parts[-1].strip()
            if not file_path:
                continue
            if file_path != "/workspace" and not file_path.startswith(
                "/workspace/"
            ):
                continue
            paths.append(
                {
                    "name": os.path.basename(file_path),
                    "path": file_path,
                }
            )

        truncated = len(paths) > capped
        return {"paths": paths[:capped], "truncated": truncated}

    async def read_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        max_size: int | None = None,
    ) -> dict:
        """Read a file from the workspace container.

        Returns a dict with ``content`` (base64), ``size``, ``truncated``,
        and ``mime_type``.

        Concurrent reads are throttled via a per-workspace semaphore to
        avoid exceeding the SSH server's MaxSessions limit when many images
        are fetched simultaneously.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # One semaphore per workspace; created lazily.
        sem = self._file_read_semaphores.get(workspace_id)
        if sem is None:
            sem = asyncio.Semaphore(4)
            self._file_read_semaphores[workspace_id] = sem

        if max_size is None:
            read_limit = FILE_READ_DEFAULT_MAX_SIZE
        else:
            read_limit = int(max_size)
            if read_limit <= 0:
                raise ValueError("max_size must be a positive integer")
            if read_limit > FILE_READ_ABSOLUTE_MAX_SIZE:
                raise ValueError(
                    f"max_size exceeds allowed maximum ({FILE_READ_ABSOLUTE_MAX_SIZE} bytes)"
                )

        async with sem:
            # Combine stat + read into a single SSH exec to halve the number
            # of SSH channels opened compared to two sequential commands.
            # Output format:
            #   line 1 = file size (bytes)
            #   line 2 = MIME type
            #   rest   = base64 content
            # Single quotes in safe_path are already prevented by _sanitize_path
            # (normpath keeps paths clean), so direct interpolation is safe here.
            shell_cmd = (
                # Guard: exit 1 immediately if the file does not exist.
                # Without this, the else-branch's `head | base64` pipeline
                # exits 0 even on a missing file, causing a ValueError when
                # we try to parse the empty first line as an integer.
                f"test -f '{safe_path}' || exit 1; "
                f"SZ=$(stat -c '%s' '{safe_path}'); "
                f"MT=$(file --mime-type -b '{safe_path}' 2>/dev/null || echo 'application/octet-stream'); "
                f'echo "$SZ"; '
                f'echo "$MT"; '
                f'if [ "$SZ" -le {read_limit} ]; then '
                f"  base64 '{safe_path}'; "
                f"else "
                f"  head -c {read_limit} '{safe_path}' | base64; "
                f"fi"
            )
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["sh", "-c", shell_cmd],
                workdir="/workspace",
            )

        if exit_code != 0:
            raise RuntimeError(f"Failed to read file: {output}")

        # Parse output: first line is size, second line MIME type, remainder base64.
        lines = output.splitlines()
        if len(lines) < 2:
            raise RuntimeError("Invalid file read response format")
        file_size = int(lines[0].strip())
        mime_type = lines[1].strip() or "application/octet-stream"
        content_output = "\n".join(lines[2:]) if len(lines) > 2 else ""
        truncated = file_size > read_limit

        return {
            "content": content_output.strip(),
            "size": file_size,
            "truncated": truncated,
            "mime_type": mime_type,
        }

    async def upload_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        filename: str,
        content_b64: str,
        is_directory: bool = False,
    ) -> None:
        """Upload a file into the workspace container.

        Args:
            workspace_id: Target workspace.
            path: Directory path to upload into.
            filename: Name of the file to create.
            content_b64: Base64-encoded file content.
            is_directory: If True, content is a tar.gz archive to extract.
        """
        safe_path = self._sanitize_path(path)
        safe_filename = self._sanitize_filename(filename)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # Check upload size
        raw_size = len(content_b64) * 3 // 4  # approximate decoded size
        if raw_size > FILE_UPLOAD_MAX_SIZE:
            raise ValueError(
                f"Upload exceeds maximum size of {FILE_UPLOAD_MAX_SIZE} bytes"
            )

        # Ensure target directory exists
        await runtime.exec_command_wait(
            info.instance_id,
            command=["mkdir", "-p", safe_path],
            workdir="/workspace",
        )

        try:
            decoded_content = base64.b64decode(content_b64, validate=True)
        except Exception as exc:  # pragma: no cover - safety net
            raise ValueError("Invalid base64 upload payload") from exc

        if is_directory:
            archive_data = self._convert_archive_to_tar(decoded_content)
        else:
            archive_data = self._build_single_file_tar(safe_filename, decoded_content)

        await runtime.put_archive(
            info.instance_id,
            safe_path,
            archive_data,
        )

        logger.info(
            "file_uploaded",
            workspace_id=str(workspace_id),
            path=safe_path,
            filename=safe_filename,
        )

    async def download_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Download a file or directory from the workspace container.

        Returns a dict with ``content`` (base64), ``filename``, ``is_archive``.
        For directories, the content is a tar.gz archive.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # Check if it's a directory
        exit_code, _ = await runtime.exec_command_wait(
            info.instance_id,
            command=["test", "-d", safe_path],
            workdir="/workspace",
        )
        is_dir = exit_code == 0

        if is_dir:
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=[
                    "sh",
                    "-c",
                    f"tar czf - -C '{os.path.dirname(safe_path)}' "
                    f"'{os.path.basename(safe_path)}' | base64",
                ],
                workdir="/workspace",
            )
            filename = os.path.basename(safe_path) + ".tar.gz"
        else:
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["base64", safe_path],
                workdir="/workspace",
            )
            filename = os.path.basename(safe_path)

        if exit_code != 0:
            raise RuntimeError(f"Failed to download: {output}")

        return {
            "content": output.strip(),
            "filename": filename,
            "is_archive": is_dir,
        }

    async def stat_path(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Stat a path inside the workspace container.

        Returns a dict with ``path``, ``is_dir``, ``size``, ``mime_type``.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        shell_cmd = (
            f"if [ -e '{safe_path}' ]; then "
            f"if [ -d '{safe_path}' ]; then echo 'dir'; "
            f"du -sb '{safe_path}' | cut -f1; "
            f"echo 'inode/directory'; "
            f"else stat -c '%s' '{safe_path}'; "
            f"file --mime-type -b '{safe_path}' 2>/dev/null "
            "|| echo 'application/octet-stream'; "
            f"fi; else echo 'missing'; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["sh", "-c", shell_cmd],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to stat path: {output}")
        lines = output.strip().splitlines()
        if not lines or lines[0].strip() == "missing":
            raise FileNotFoundError(f"No such file or directory: {path}")
        is_dir = lines[0].strip() == "dir"
        size = int(lines[1].strip()) if len(lines) > 1 else 0
        mime_type = lines[2].strip() if len(lines) > 2 else "application/octet-stream"
        return {
            "path": safe_path,
            "is_dir": is_dir,
            "size": size,
            "mime_type": mime_type,
        }

    async def write_file_content(
        self,
        workspace_id: uuid.UUID,
        path: str,
        content_b64: str,
        mode: int = 0o644,
    ) -> None:
        """Write file content atomically inside the workspace container.

        Args:
            workspace_id: Target workspace.
            path: Absolute path under ``/workspace``.
            content_b64: Base64-encoded file content.
            mode: File permission bits applied after the write.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        try:
            decoded = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 file payload") from exc
        if mode < 0 or mode > 0o777:
            raise ValueError(f"Invalid file mode: {mode!r}")

        archive = self._build_single_file_tar(os.path.basename(safe_path), decoded)
        await runtime.put_archive(
            info.instance_id,
            os.path.dirname(safe_path) or "/workspace",
            archive,
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["chmod", format(mode, "o"), safe_path],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to set file mode: {output}")
        logger.info(
            "file_written",
            workspace_id=str(workspace_id),
            path=safe_path,
        )

    async def exec_harness_command(
        self,
        workspace_id: uuid.UUID,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Execute a harness command with separated stdout and stderr.

        Runs the command via a shell wrapper that multiplexes the two
        streams into tagged base64 frames, then decodes them back into
        separate buffers. Returns ``(exit_code, stdout, stderr)``.
        """
        safe_workdir = self._sanitize_path(workdir)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if isinstance(command, str):
            argv: list[str] = ["bash", "-lc", command]
        else:
            argv = [str(arg) for arg in command]
            if not argv:
                raise ValueError("command must not be empty")
        marker_out = "OPENCURIA_STDOUT"
        marker_err = "OPENCURIA_STDERR"
        inner = " ".join(shlex.quote(arg) for arg in argv)
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi; "
        )
        wrapper = (
            f"{source}"
            f"__oc_out=$(mktemp); __oc_err=$(mktemp); "
            f'sh -c {shlex.quote(inner)} >"$__oc_out" 2>"$__oc_err"; '
            f"__oc_code=$?; "
            f'echo {marker_out}; base64 "$__oc_out"; '
            f'echo {marker_err}; base64 "$__oc_err"; '
            f'echo "EXIT:$__oc_code"; rm -f "$__oc_out" "$__oc_err"; '
            f"exit $__oc_code"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["sh", "-lc", wrapper],
            workdir=safe_workdir,
            env=env,
        )
        stdout, stderr = self._parse_harness_exec_output(output)
        return exit_code, stdout, stderr

    async def exec_harness_command_stream(
        self,
        workspace_id: uuid.UUID,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
    ):
        """Execute a harness command and yield ``(stream, data)`` chunks.

        Yields ``("stdout", text)`` / ``("stderr", text)`` tuples while the
        command runs, then a final ``("exit", str(exit_code))`` tuple.
        """
        safe_workdir = self._sanitize_path(workdir)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if isinstance(command, str):
            argv: list[str] = ["bash", "-lc", command]
        else:
            argv = [str(arg) for arg in command]
            if not argv:
                raise ValueError("command must not be empty")
        marker_out = "OPENCURIA_LINE_STDOUT:"
        marker_err = "OPENCURIA_LINE_STDERR:"
        inner = " ".join(shlex.quote(arg) for arg in argv)
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi; "
        )
        # Portable fifo-based streaming wrapper: multiplexes the child
        # stdout/stderr into tagged lines on the combined output stream,
        # then reports the exit code on the last line.
        portable = (
            f"{source}"
            "__oc_dir=$(mktemp -d); "
            "__oc_o=$__oc_dir/o; __oc_e=$__oc_dir/e; "
            'mkfifo "$__oc_o" "$__oc_e"; '
            f"(sh -c {shlex.quote(inner)} "
            '>"$__oc_o" 2>"$__oc_e"; echo $? >"$__oc_dir/code") & '
            "__oc_pid=$!; "
            "(while IFS= read -r __oc_l; do "
            f"printf '{marker_out}%s\\n' \"$__oc_l\"; "
            'done <"$__oc_o" & '
            "while IFS= read -r __oc_m; do "
            f"printf '{marker_err}%s\\n' \"$__oc_m\"; "
            'done <"$__oc_e" & wait); '
            'wait $__oc_pid; __oc_code=$(cat "$__oc_dir/code"); '
            'rm -rf "$__oc_dir"; '
            'echo "OPENCURIA_EXIT:$__oc_code"'
        )
        exit_code = 0
        async for line in runtime.exec_command(
            info.instance_id,
            command=["sh", "-lc", portable],
            workdir=safe_workdir,
            env=env,
        ):
            if line.startswith(marker_out):
                yield ("stdout", line[len(marker_out) :])
            elif line.startswith(marker_err):
                yield ("stderr", line[len(marker_err) :])
            elif line.startswith("OPENCURIA_EXIT:"):
                exit_code = int(line.split(":", 1)[1].strip() or 0)
                yield ("exit", str(exit_code))
            else:
                yield ("stdout", line)

    @staticmethod
    def _parse_harness_exec_output(output: str) -> tuple[str, str]:
        """Split tagged exec wrapper output into (stdout, stderr)."""
        marker_out = "OPENCURIA_STDOUT"
        marker_err = "OPENCURIA_STDERR"
        if marker_out not in output or marker_err not in output:
            return output, ""
        stdout_b64 = output.split(marker_out, 1)[1].split(marker_err, 1)[0]
        remainder = output.split(marker_err, 1)[1]
        stderr_b64 = remainder.split("EXIT:", 1)[0]
        import base64 as _b64

        def _decode(payload: str) -> str:
            cleaned = "".join(payload.split())
            if not cleaned:
                return ""
            return _b64.b64decode(cleaned).decode("utf-8", errors="replace")

        return _decode(stdout_b64), _decode(stderr_b64)

    # ── Image artifact operations ─────────────────────────────────────

    async def build_image(
        self,
        *,
        runtime_type: str,
        build_job_id: str,
        dockerfile_content: str = "",
        image_tag: str = "",
        base_distro: str = "",
        init_script: str = "",
        image_path: str = "",
        progress_callback=None,
    ) -> dict[str, str]:
        """Build runtime image from definition payload.

        Returns a dict containing ``image_tag`` and/or ``image_path``.
        """
        if runtime_type == "docker":
            if not dockerfile_content.strip():
                raise RuntimeError(
                    "dockerfile_content is required for docker image builds"
                )
            if not image_tag.strip():
                raise RuntimeError("image_tag is required for docker image builds")
            try:
                import docker  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("docker SDK is not available") from exc

            context_stream = io.BytesIO()
            with tarfile.open(fileobj=context_stream, mode="w") as tar:
                df_bytes = dockerfile_content.encode("utf-8")
                df_info = tarfile.TarInfo(name="Dockerfile")
                df_info.size = len(df_bytes)
                tar.addfile(df_info, io.BytesIO(df_bytes))

            context_stream.seek(0)
            client = docker.from_env()
            image, logs = await asyncio.to_thread(
                client.images.build,
                fileobj=context_stream,
                custom_context=True,
                rm=True,
                tag=image_tag,
                pull=False,
                forcerm=True,
            )
            for entry in logs:
                if progress_callback is None:
                    continue
                line = ""
                if isinstance(entry, dict):
                    line = str(entry.get("stream") or entry.get("status") or "").strip()
                else:
                    line = str(entry).strip()
                if line:
                    await progress_callback(line)
            return {"image_tag": image_tag}

        if runtime_type == "qemu":
            if not image_path.strip():
                raise RuntimeError("image_path is required for qemu image builds")
            runtime = self._get_runtime_by_type("qemu")
            build_image = getattr(runtime, "build_image", None)
            if build_image is None:
                raise RuntimeError("QEMU runtime does not support image builds")
            return await build_image(
                base_distro=base_distro,
                init_script=init_script,
                image_path=image_path,
                progress_callback=progress_callback,
            )

        raise RuntimeError(f"Unsupported runtime_type for image build: {runtime_type}")

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
    ) -> "ImageArtifactInfo":
        """Create an image artifact from a workspace.

        The runtime must support artifact capture.
        """
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' does not support image artifact capture"
            )
        artifact = await runtime.create_image_artifact(info.instance_id, name)
        logger.info(
            "image_artifact_created",
            workspace_id=str(workspace_id),
            image_artifact_id=artifact.artifact_id,
            name=name,
        )
        return artifact

    async def list_image_artifacts(
        self,
        workspace_id: uuid.UUID,
    ) -> list["ImageArtifactInfo"]:
        """List all captured image artifacts for a workspace."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            return []
        return await runtime.list_image_artifacts(info.instance_id)

    async def delete_image_artifact(
        self,
        workspace_id: uuid.UUID,
        image_artifact_id: str,
    ) -> None:
        """Delete a captured image artifact."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' does not support image artifact deletion"
            )
        await runtime.delete_image_artifact(image_artifact_id)
        logger.info(
            "image_artifact_deleted",
            workspace_id=str(workspace_id),
            image_artifact_id=image_artifact_id,
        )

    async def delete_image_reference(
        self,
        *,
        runtime_type: str,
        image_ref: str,
    ) -> str:
        """Delete a concrete runtime image reference without requiring a workspace.

        Returns 'deleted' or 'already_absent' to indicate the result.
        """
        if runtime_type == "docker":
            if not image_ref.strip():
                raise RuntimeError("image_ref is required for docker image deletion")
            try:
                import docker  # type: ignore[import-not-found]
                from docker.errors import ImageNotFound  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("docker SDK is not available") from exc

            client = docker.from_env()
            try:
                await asyncio.to_thread(
                    client.images.remove, image=image_ref, force=True
                )
                logger.info("docker_image_deleted", image_ref=image_ref)
                return "deleted"
            except ImageNotFound:
                logger.info("docker_image_already_absent", image_ref=image_ref)
                return "already_absent"

        if runtime_type == "qemu":
            if not image_ref.strip():
                raise RuntimeError("image_ref is required for qemu image deletion")
            runtime = self._get_runtime_by_type("qemu")
            try:
                await runtime.delete_image_artifact(image_ref)
                logger.info("qemu_image_deleted", image_ref=image_ref)
                return "deleted"
            except FileNotFoundError:
                logger.info("qemu_image_already_absent", image_ref=image_ref)
                return "already_absent"

        raise RuntimeError(
            f"Unsupported runtime_type for image deletion: {runtime_type}"
        )

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
        runtime_type: str,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Create a workspace from an image artifact and inject credentials.

        Credentials remain on disk until a controlled stop.
        """
        runtime = self._get_runtime_by_type(runtime_type)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{runtime_type}' does not support image artifact cloning"
            )

        instance_id = await runtime.create_workspace_from_image_artifact(
            image_artifact_id,
            str(new_workspace_id),
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

        self._cache[new_workspace_id] = WorkspaceInfo(
            workspace_id=new_workspace_id,
            instance_id=instance_id,
            status="running",
            runtime_type=runtime_type,
        )

        log = logger.bind(
            workspace_id=str(new_workspace_id),
            image_artifact_id=image_artifact_id,
            runtime_type=runtime_type,
        )
        log.info("workspace_created_from_image_artifact")

        credentials_present = await self.inject_workspace_credentials(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        return new_workspace_id, credentials_present
