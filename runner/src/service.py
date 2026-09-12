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

from . import git as git_ops
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
# Ubuntu 22.04 ships xdotool 3.20160805, which has almost no key aliases
# and treats a bare "--" as an invalid option. Map LLM-friendly names to
# X11 keysyms that this version actually sends.
_XDOTOOL_KEY_ALIASES = {
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "spacebar": "space",
    "backspace": "BackSpace",
    "bksp": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "pageup": "Page_Up",
    "pagedown": "Page_Down",
    "pgup": "Page_Up",
    "pgdn": "Page_Down",
    "home": "Home",
    "end": "End",
    "insert": "Insert",
    "ins": "Insert",
    "capslock": "Caps_Lock",
}
_XDOTOOL_MODIFIER_ALIASES = {
    "control": "ctrl",
    "ctrl": "ctrl",
    "command": "super",
    "cmd": "super",
    "meta": "super",
    "win": "super",
    "windows": "super",
    "super": "super",
    "option": "alt",
    "alt": "alt",
    "shift": "shift",
}
_XDOTOOL_KEY_FAILURE_MARKERS = (
    "No such key name",
    "Ignoring it",
    "Invalid --option",
)
_XDOTOOL_FUNCTION_KEY_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")

BACKGROUND_PROCESS_DIR = "/workspace/.opencuria/processes"
_BACKGROUND_PROCESS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_BACKGROUND_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BACKGROUND_STOP_GRACE_S = 2.0
_BACKGROUND_STOP_POLL_S = 0.2


def _collapse_xdotool_token(token: str) -> str:
    """Return a case- and separator-insensitive lookup key."""
    return token.strip().lower().replace("-", "").replace("_", "").replace(" ", "")


def _normalize_xdotool_token(token: str, *, modifier: bool = False) -> str:
    """Map one key or modifier name to an xdotool 3.20160805 token."""
    raw = str(token).strip()
    if not raw:
        raise ValueError("key token must not be empty")
    collapsed = _collapse_xdotool_token(raw)
    if modifier:
        alias = _XDOTOOL_MODIFIER_ALIASES.get(collapsed)
        if alias is not None:
            return alias
        key_alias = _XDOTOOL_KEY_ALIASES.get(collapsed)
        if key_alias is not None:
            return key_alias
        return raw
    alias = _XDOTOOL_KEY_ALIASES.get(collapsed)
    if alias is not None:
        return alias
    modifier_alias = _XDOTOOL_MODIFIER_ALIASES.get(collapsed)
    if modifier_alias is not None:
        return modifier_alias
    function_key = _XDOTOOL_FUNCTION_KEY_RE.fullmatch(collapsed)
    if function_key:
        return f"F{function_key.group(1)}"
    return raw


def _normalize_xdotool_key_combo(key: str, modifiers: list[Any]) -> str:
    """Build an xdotool key combo from a key name and optional modifiers."""
    if not isinstance(modifiers, list):
        raise ValueError("modifiers must be a list")
    mod_tokens: list[str] = []
    for item in modifiers:
        if not isinstance(item, str):
            raise ValueError("modifiers must be a list of strings")
        stripped = item.strip()
        if not stripped:
            continue
        mod_tokens.append(_normalize_xdotool_token(stripped, modifier=True))
    parts = [part.strip() for part in str(key).split("+") if part.strip()]
    if not parts:
        raise ValueError("key must not be empty")
    *combo_mods, key_part = parts
    tokens = [
        *mod_tokens,
        *(
            _normalize_xdotool_token(part, modifier=True)
            for part in combo_mods
        ),
        _normalize_xdotool_token(key_part),
    ]
    return "+".join(tokens)


def _xdotool_type_command(text: str) -> str:
    """Build an xdotool type command compatible with Ubuntu 22.04."""
    quoted = shlex.quote(text)
    if text.startswith("-"):
        return (
            f"printf '%s' {quoted} | "
            "xdotool type --delay 0 --clearmodifiers --file -"
        )
    return f"xdotool type --delay 0 --clearmodifiers {quoted}"


def _xdotool_key_failed(exit_code: int, output: str) -> bool:
    """Return True when xdotool did not actually deliver the key."""
    if exit_code != 0:
        return True
    return any(marker in output for marker in _XDOTOOL_KEY_FAILURE_MARKERS)


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
        # Git operations: serialised per workspace/repo so concurrent
        # snapshot + mutation requests cannot interleave mid-sequence.
        self._git_locks: dict[tuple[uuid.UUID, str], asyncio.Lock] = {}
        self._git_locks_guard = asyncio.Lock()
        # Desktop lifecycle: serialised per workspace so a concurrent
        # viewer start and a computer-use hold cannot stop/start the
        # shared Xvnc :1 process twice, and a release racing a start
        # cannot stop the freshly started process. Entries are kept for
        # the lifetime of the runner process (bounded by ever-seen
        # workspaces, cleared on restart) and never dropped: removing an
        # entry after release is not safe without waiter knowledge —
        # release wakes the first waiter before it re-acquires, so a
        # drop in that window hands a third caller a different lock
        # object and lifecycle operations run in parallel.
        self._desktop_locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._desktop_locks_guard = asyncio.Lock()

    # -- background processes --------------------------------------------------

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
        # Restart safety: if the same process_id still tracks a living PID,
        # best-effort stop it (TERM -> grace -> KILL) before starting the
        # new run. The tracking entry itself is replaced below after start.
        old_entry = self._background_processes.get(workspace_id, {}).get(
            cleaned_process_id
        )
        if old_entry is not None:
            try:
                if await self._probe_background_pid(
                    runtime, info.instance_id, old_entry.pid
                ):
                    await self._kill_background_pid(
                        runtime, info.instance_id, old_entry.pid, "TERM"
                    )
                    elapsed = 0.0
                    while elapsed <= _BACKGROUND_STOP_GRACE_S:
                        if not await self._probe_background_pid(
                            runtime, info.instance_id, old_entry.pid
                        ):
                            break
                        await asyncio.sleep(_BACKGROUND_STOP_POLL_S)
                        elapsed += _BACKGROUND_STOP_POLL_S
                    if await self._probe_background_pid(
                        runtime, info.instance_id, old_entry.pid
                    ):
                        await self._kill_background_pid(
                            runtime, info.instance_id, old_entry.pid, "KILL"
                        )
            except Exception:
                logger.exception(
                    "background_process_restart_stop_failed",
                    workspace_id=str(workspace_id),
                    process_id=cleaned_process_id,
                    old_pid=old_entry.pid,
                )
        start_shell = self._build_background_start_shell(
            command.strip(), resolved_log_path, resolved_exit_path, extra_env
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
        """Remove a workspace and clean up resources.

        The per-workspace desktop lock is acquired first and held across
        the entire desktop-state/cache transition (recording interrupt
        while the cache is still available, then cache pop plus
        session/recording clear with no lock release in between). A
        concurrent ensure/start holding the lock therefore completes
        first; remove only pops the cache afterwards. A queued start/hold
        acquiring the same retained lock afterwards finds no cache entry
        and fails cleanly instead of resurrecting a session. The lock
        object itself is kept for the process lifetime (never dropped) so
        queued waiters and later callers always share one serialising
        lock. Runtime removal runs after the lock is released: it must
        never block behind a stuck Xvnc start while holding the desktop
        lock. ``_interrupt_desktop_recordings``/``_exec_desktop_shell``
        take no desktop lock and are awaited while holding it;
        correctness wins over head-of-line blocking (runtime commands
        have their own limits). Recording-interrupt errors still clear
        state and attempt runtime removal.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        await self._kill_all_background_processes(workspace_id, reason="remove")
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            # Interrupt while the cache is still available:
            # _exec_desktop_shell needs _get_cached, so popping first
            # would turn every interrupt into a "not found" failure.
            # _interrupt_desktop_recordings already swallows per-command
            # errors and clears the recordings dict; the outer guard only
            # covers unexpected failures so state clear + runtime remove
            # still run.
            try:
                await self._interrupt_desktop_recordings(workspace_id)
            except Exception:
                logger.exception(
                    "desktop_recording_interrupt_failed",
                    workspace_id=str(workspace_id),
                )
            info = self._cache.pop(workspace_id, None)
            self._desktop_sessions.pop(workspace_id, None)
            # Final sweep for entries added during the interrupt awaits
            # (record_start is lock-free); still under the same hold, so
            # no waiter could publish a session in between.
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

        Same atomicity as :meth:`remove_workspace`: the desktop lock is
        held across recording interrupt (cache still available), cache pop
        (plus unreachable-timer pop), and session/recording clear, with no
        release in between. The lock object is retained afterwards (never
        dropped) so queued waiters keep sharing one lock object.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        await self._kill_all_background_processes(
            workspace_id, reason="cleanup_unknown"
        )
        lock = await self._desktop_lock(workspace_id)
        async with lock:
            # Same ordering as remove_workspace: interrupt while the cache
            # is still available (per-command errors are swallowed inside
            # the helper; the guard only covers unexpected failures), then
            # pop and sweep with no lock release in between.
            try:
                await self._interrupt_desktop_recordings(workspace_id)
            except Exception:
                logger.exception(
                    "desktop_recording_interrupt_failed",
                    workspace_id=str(workspace_id),
                )
            info = self._cache.pop(workspace_id, None)
            self._unreachable_since.pop(workspace_id, None)
            self._desktop_sessions.pop(workspace_id, None)
            self._desktop_recordings = {
                key: value
                for key, value in self._desktop_recordings.items()
                if key[0] != workspace_id
            }

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
        """
        async with self._desktop_locks_guard:
            lock = self._desktop_locks.get(workspace_id)
            if lock is None:
                lock = asyncio.Lock()
                self._desktop_locks[workspace_id] = lock
            return lock

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
            return {
                "ok": True,
                "image_b64": output.strip(),
                "mime": result_mime,
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
                _xdotool_type_command(text),
            )
            if exit_code != 0:
                raise RuntimeError(f"Failed to type text: {output}")
            return {"ok": True}

        if action == "key":
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

        if action == "execute":
            # Generic Agent-S action execution: run exactly the passed
            # internal code (materialized by the backend core) via
            # ``python3 -c`` with the desktop env. No agent logic lives
            # here; validation only guards the RPC boundary. Validation ran
            # above; a single generic liveness probe also ran above.
            exit_code, stdout, stderr = await asyncio.wait_for(
                self.exec_harness_command(
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
    def _sanitize_exec_workdir(path: str) -> str:
        """Normalize an exec working directory (not sandboxed to /workspace)."""
        raw_input = path or ""
        if "\x00" in raw_input or "\n" in raw_input:
            raise ValueError(f"Invalid workdir: {path}")
        raw = raw_input.strip() or "/workspace"
        candidate = raw if os.path.isabs(raw) else f"/workspace/{raw}"
        normalized = os.path.normpath(candidate)
        if not os.path.isabs(normalized):
            raise ValueError(f"Invalid workdir: {path}")
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

    async def _realpath_under_workspace(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        path: str,
    ) -> str:
        """Resolve symlinks for *path* and ensure it stays in /workspace.

        Runs ``realpath -m`` inside the workspace, which resolves symlinks
        and ``..`` segments. Raises ``ValueError`` (fail-closed) when the
        resolved path escapes ``/workspace``. Falls back to *path* when
        ``realpath`` is unavailable in the image (coreutils ships it on
        Ubuntu, so this is only a safety net). Note: check-then-use is
        inherently TOCTOU-prone if the workspace mutates the link between
        the check and the file operation; accepted here as defense-in-depth
        on top of the ``/workspace`` sandbox.
        """
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["realpath", "-m", path],
            workdir="/workspace",
        )
        if exit_code != 0:
            return path
        resolved = output.strip().splitlines()
        if not resolved or not resolved[0]:
            return path
        real = resolved[0].strip()
        if real != "/workspace" and not real.startswith("/workspace/"):
            raise ValueError(f"Path escapes /workspace: {path}")
        return real

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
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

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
            safe_path = await self._realpath_under_workspace(
                runtime, info.instance_id, safe_path
            )
            # Combine stat + read into a single SSH exec to halve the number
            # of SSH channels opened compared to two sequential commands.
            # Output format:
            #   line 1 = file size (bytes)
            #   line 2 = MIME type
            #   rest   = base64 content
            # Paths are embedded via shlex.quote so a quote in the path
            # cannot break out of the shell quoting.
            qpath = shlex.quote(safe_path)
            shell_cmd = (
                # Guard: exit 1 immediately if the file does not exist.
                # Without this, the else-branch's `head | base64` pipeline
                # exits 0 even on a missing file, causing a ValueError when
                # we try to parse the empty first line as an integer.
                f"test -f {qpath} || exit 1; "
                f"SZ=$(stat -c '%s' {qpath}); "
                f"MT=$(file --mime-type -b {qpath} 2>/dev/null "
                "|| echo 'application/octet-stream'); "
                f'echo "$SZ"; '
                f'echo "$MT"; '
                f'if [ "$SZ" -le {read_limit} ]; then '
                f"  base64 {qpath}; "
                f"else "
                f"  head -c {read_limit} {qpath} | base64; "
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
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

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
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

        # Check if it's a directory
        exit_code, _ = await runtime.exec_command_wait(
            info.instance_id,
            command=["test", "-d", safe_path],
            workdir="/workspace",
        )
        is_dir = exit_code == 0

        if is_dir:
            qp_dir = shlex.quote(os.path.dirname(safe_path))
            qp_base = shlex.quote(os.path.basename(safe_path))
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=[
                    "sh",
                    "-c",
                    f"tar czf - -C {qp_dir} {qp_base} | base64",
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
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )

        qpath = shlex.quote(safe_path)
        shell_cmd = (
            f"if [ -e {qpath} ]; then "
            f"if [ -d {qpath} ]; then echo 'dir'; "
            f"du -sb {qpath} | cut -f1; "
            f"echo 'inode/directory'; "
            f"else stat -c '%s' {qpath}; "
            f"file --mime-type -b {qpath} 2>/dev/null "
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
        safe_path = await self._realpath_under_workspace(
            runtime, info.instance_id, safe_path
        )
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
        safe_workdir = self._sanitize_exec_workdir(workdir)
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
        safe_workdir = self._sanitize_exec_workdir(workdir)
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

    # ── Git operations (dumb-executor git service) ──────────────────────

    async def _git_lock(
        self, workspace_id: uuid.UUID, repo_root: str
    ) -> asyncio.Lock:
        """Return the serialising lock for one workspace/repo pair."""
        key = (workspace_id, repo_root)
        async with self._git_locks_guard:
            lock = self._git_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._git_locks[key] = lock
            return lock

    async def _git_exec(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        argv: list[str],
        *,
        workdir: str,
        env: dict[str, str],
        timeout: float,
        check_git: bool = False,
    ) -> tuple[int, str]:
        """Run one git argv inside the workspace with a timeout.

        Every git argv runs behind the fixed sourcing wrapper
        (:func:`src.git.git_exec_wrapper_argv`) so the persistent
        credential file is sourced and ``GITHUB_TOKEN`` is inherited by
        git and its askpass child — without ever appearing in argv, URLs
        or logs.  User-controlled git arguments stay separate argv
        elements behind ``exec "$@"`` and are never shell-interpreted.
        Only non-git probes (``realpath``/``find``/``test``/``rm``) run
        unwrapped.  Raises :class:`GitError` with code ``missing_git``
        when the git binary is absent.
        """
        if argv[:1] == ["git"]:
            command = git_ops.git_exec_wrapper_argv(list(argv))
        else:
            command = list(argv)
        try:
            exit_code, output = await asyncio.wait_for(
                runtime.exec_command_wait(
                    instance_id,
                    command=command,
                    workdir=workdir,
                    env=env,
                ),
                timeout,
            )
        except asyncio.TimeoutError as exc:
            raise git_ops.GitError("timeout", "Git operation timed out") from exc
        # Missing-binary detection is deliberately narrow: only a nonzero
        # exit of 126/127 *plus* a binary marker counts as missing_git.
        # Plain "not found" text from successful (exit 0) or exit-1 git
        # output — e.g. filenames or git's own messages — must stay a
        # normal operation result, never a false-positive missing_git.
        # _git_exec wraps git argv (sh -c wrapper) but inspects the
        # unwrapped argv, so both wrapped ["git", ...] and bare
        # ["realpath", "-m", ...] probes are covered here.
        lowered = output.lower() if isinstance(output, str) else ""
        missing_markers = (
            "command not found",
            "not recognized",
            "no such file or directory",
            "no such file",
            "not found",
        )
        is_git_argv = argv[:1] == ["git"]
        is_realpath_probe = argv[:1] == ["realpath", "-m"]
        if (
            exit_code in (126, 127)
            and (is_git_argv or is_realpath_probe)
            and any(m in lowered for m in missing_markers)
        ):
            # Only treat as missing-git when the marker refers to the
            # binary under test, not to repo content leaking into output.
            if is_git_argv or "realpath" in lowered:
                raise git_ops.GitError(
                    "missing_git",
                    "git is not available inside the workspace",
                    exit_code=exit_code,
                    stderr=output,
                )
        if check_git and exit_code != 0 and "not a git repository" in lowered:
            raise git_ops.GitError(
                "not_a_repo",
                "Path is not inside a git repository",
                exit_code=exit_code,
                stderr=output,
            )
        return exit_code, output if isinstance(output, str) else str(output)

    async def _git_realpath_contained(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        path: str,
        env: dict[str, str],
        *,
        context: str,
        display: str | None = None,
    ) -> str:
        """Resolve *path* via ``realpath -m`` and enforce ``/workspace``.

        *context* is ``"repo"`` for worktree roots and ``"metadata"`` for
        gitdir/common-dir values.  Fail-closed: realpath errors, empty or
        multiline output, and containment escapes all raise.  Messages only
        ever carry ``/workspace`` paths (never external host paths):
        metadata failures echo *display* (the repo root), never *path*.
        """
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            ["realpath", "-m", path],
            workdir="/workspace",
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        safe_display = display if display is not None else path
        if exit_code != 0:
            if context == "repo":
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Could not resolve repo path: {safe_display}",
                    exit_code=exit_code,
                    stderr="",
                )
            raise git_ops.GitError(
                "unsafe_repository",
                f"Repository metadata outside workspace: {safe_display}",
                exit_code=exit_code,
                stderr="",
            )
        resolved = git_ops.parse_single_path_output(output)
        if resolved is None:
            if context == "repo":
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Could not resolve repo path: {safe_display}",
                    exit_code=exit_code,
                    stderr="",
                )
            raise git_ops.GitError(
                "unsafe_repository",
                f"Repository metadata outside workspace: {safe_display}",
                exit_code=exit_code,
                stderr="",
            )
        if not git_ops.is_workspace_path(resolved):
            raise git_ops.GitError(
                "unsafe_repository",
                f"Repository metadata outside workspace: {safe_display}",
                exit_code=exit_code,
                stderr="",
            )
        return resolved

    async def _git_verify_metadata_paths(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> str:
        """Verify gitdir + common-dir live under ``/workspace`` (fail-closed).

        *repo_root* is an already containment-checked ``/workspace`` path
        used as ``workdir`` and as the only path echoed in errors.  All
        rev-parse outputs must be single-line; empty/multiline/unexpected
        values reject.  ``--absolute-git-dir`` must be absolute; common-dir
        prefers ``--path-format=absolute`` with a plain ``--git-common-dir``
        fallback (relative values resolve against the verified git dir —
        see below — never against the worktree root).  Both values are
        passed through ``realpath -m`` containment.  Returns the verified
        (realpath) git dir.  No shell concatenation: all probes are fixed
        argv lists.
        """
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--absolute-git-dir"],
            workdir=repo_root,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {repo_root}",
                exit_code=exit_code,
                stderr="",
            )
        git_dir_raw = git_ops.parse_single_path_output(output)
        if git_dir_raw is None or not git_dir_raw.startswith("/"):
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {repo_root}",
                exit_code=exit_code,
                stderr="",
            )
        git_dir = await self._git_realpath_contained(
            runtime, instance_id, git_dir_raw, env,
            context="metadata", display=repo_root,
        )
        # Common dir: absolute preferred, plain fallback for older git.
        # ``--path-format`` only affects following args, so it must precede
        # ``--git-common-dir``.  The plain (no --path-format) output is
        # relative to the *process cwd* on old git (prefix-relative per
        # setup.c/relative_path: "../.git" observed from a subdir of a
        # normal repo, "../../external-gitdir" from a separate-git-dir
        # root).  The cwd git ran in is *repo_root* — the worktree root
        # here, so prefix is empty — but when the plain fallback engages,
        # a relative value may also encode the in-gitdir ``commondir``
        # indirection ("../.." inside a linked worktree gitdir).  Git
        # resolves the commondir file relative to the git dir (see
        # get_common_dir_noenv: "%s/commondir" + "%s/<data>" joined onto
        # gitdir), never relative to the worktree root, so both candidate
        # bases (cwd + git dir) are checked: either both must resolve
        # under /workspace (fail-closed) or the candidate rejects.
        common_candidate: str | None = None
        exit_code_c, output_c = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            workdir=repo_root,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c == 0:
            parsed = git_ops.parse_single_path_output(output_c)
            if parsed is None:
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_c,
                    stderr="",
                )
            if parsed.startswith("/"):
                common_candidate = parsed
            else:
                # Explicit --path-format=absolute must yield an absolute
                # path; anything else is unexpected (or hostile) output.
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_c,
                    stderr="",
                )
        else:
            exit_code_f, output_f = await self._git_exec(
                runtime,
                instance_id,
                ["git", "rev-parse", "--git-common-dir"],
                workdir=repo_root,
                env=env,
                timeout=git_ops.GIT_READ_TIMEOUT_S,
            )
            if exit_code_f != 0:
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_f,
                    stderr="",
                )
            parsed_f = git_ops.parse_single_path_output(output_f)
            if parsed_f is None:
                raise git_ops.GitError(
                    "not_a_repo",
                    f"Path is not a git repository: {repo_root}",
                    exit_code=exit_code_f,
                    stderr="",
                )
            if parsed_f.startswith("/"):
                common_candidate = os.path.normpath(parsed_f)
            else:
                # Old-git plain fallback: the value is cwd-relative (the
                # cwd being *repo_root*), but the same ".."-shaped value
                # can also encode a gitdir-relative commondir indirection
                # (git joins commondir content onto the git dir, see
                # get_common_dir_noenv).  Resolve against BOTH bases and
                # require both contained: realpath containment below still
                # runs on the chosen candidate, and the extra base check
                # here closes the gap where resolving only against
                # repo_root would normalise "../.." to "/".
                cwd_candidate = os.path.normpath(
                    os.path.join(repo_root, parsed_f)
                )
                gitdir_candidate = os.path.normpath(
                    os.path.join(git_dir, parsed_f)
                )
                if not (
                    git_ops.is_workspace_path(cwd_candidate)
                    and git_ops.is_workspace_path(gitdir_candidate)
                ):
                    raise git_ops.GitError(
                        "unsafe_repository",
                        f"Repository metadata outside workspace: {repo_root}",
                        exit_code=exit_code_f,
                        stderr="",
                    )
                common_candidate = cwd_candidate
        if common_candidate is None or not common_candidate.startswith("/"):
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {repo_root}",
                exit_code=exit_code_c,
                stderr="",
            )
        await self._git_realpath_contained(
            runtime, instance_id, common_candidate, env,
            context="metadata", display=repo_root,
        )
        return git_dir

    async def _git_verify_repo_root(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        resolved: str,
        env: dict[str, str],
    ) -> str:
        """Verify *resolved* is a repo root with in-workspace metadata.

        Central root+metadata check shared by explicit resolution and
        discovery: ``--show-toplevel`` must equal *resolved*, then gitdir +
        common-dir must resolve under ``/workspace``.  *resolved* must
        already be a ``/workspace`` path (realpath-contained by the caller).
        """
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--show-toplevel"],
            workdir=resolved,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a git repository: {resolved}",
                exit_code=exit_code,
                stderr="",
            )
        toplevel = git_ops.parse_single_path_output(output)
        if toplevel is None or toplevel != resolved:
            raise git_ops.GitError(
                "not_a_repo",
                f"Path is not a repository root: {resolved}",
                exit_code=exit_code,
                stderr="",
            )
        await self._git_verify_metadata_paths(
            runtime, instance_id, resolved, env
        )
        return resolved

    async def _git_resolve_repo_root(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_arg: str,
        env: dict[str, str],
    ) -> str:
        """Resolve *repo_arg* to a verified repository root (fail-closed).

        Steps: normalise under ``/workspace`` → ``realpath -m`` symlink
        containment (a realpath failure is fatal — no unsandboxed
        fallback, symlinks must resolve) → ``git rev-parse
        --show-toplevel`` must equal the resolved path (no
        ``.git``-outside access, no subdirectories) → gitdir + common-dir
        must resolve under ``/workspace`` (no external ``.git`` file,
        symlink, ``--separate-git-dir``, worktree or submodule metadata).
        See :meth:`_git_verify_repo_root` for the shared check.
        """
        normalized = git_ops.normalize_repo_arg(repo_arg)
        resolved = await self._git_realpath_contained(
            runtime, instance_id, normalized, env, context="repo"
        )
        # Extra lexical guard so ``ValueError`` (invalid_argument) still
        # surfaces for direct escapes even if realpath mapping changes.
        if not git_ops.is_workspace_path(resolved):
            raise ValueError(f"Repo path escapes /workspace: {repo_arg!r}")
        return await self._git_verify_repo_root(
            runtime, instance_id, resolved, env
        )

    async def _git_discover_repos(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env: dict[str, str],
    ) -> list[str]:
        """Discover repository roots under ``/workspace`` (capped).

        Detects a repo directly in ``/workspace`` plus cloned repos at any
        depth (``find -prune`` never descends into ``.git`` internals).
        Every candidate goes through the central root+metadata check
        (:meth:`_git_verify_repo_root`); unsafe/unresolvable candidates
        (external gitdir/common-dir, symlink escapes, subdirectories) are
        omitted fail-closed.
        """
        roots: list[str] = []
        try:
            verified = await self._git_verify_repo_root(
                runtime, instance_id, "/workspace", env
            )
        except (git_ops.GitError, ValueError):
            verified = None
        if verified is not None:
            roots.append("/workspace")
        exit_code, output = await self._git_exec(
            runtime,
            instance_id,
            git_ops.discovery_find_args("/workspace"),
            workdir="/workspace",
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code == 0 and output.strip():
            candidates = git_ops.discovery_repo_roots(output)
            for candidate in candidates:
                if candidate in roots:
                    continue
                try:
                    resolved = await self._git_realpath_contained(
                        runtime, instance_id, candidate, env, context="repo"
                    )
                except (git_ops.GitError, ValueError):
                    # Fail closed: unresolvable candidates are skipped,
                    # never trusted unresolved.
                    continue
                if not git_ops.is_workspace_path(resolved):
                    continue
                if "/.git/" in resolved:
                    continue
                # The workspace root itself is only listed once (verified
                # above); skip the duplicate find hit.
                if resolved == "/workspace":
                    continue
                try:
                    await self._git_verify_repo_root(
                        runtime, instance_id, resolved, env
                    )
                except (git_ops.GitError, ValueError):
                    continue
                roots.append(resolved)
                if len(roots) >= git_ops.GIT_DISCOVERY_MAX_REPOS:
                    break
        return sorted(set(roots))[: git_ops.GIT_DISCOVERY_MAX_REPOS]

    async def list_git_repositories(
        self, workspace_id: uuid.UUID
    ) -> list[str]:
        """Return discovered repository roots for *workspace_id* (internal)."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        async with git_ops.github_askpass_context(runtime, info.instance_id):
            env = git_ops.build_git_env()
            return await self._git_discover_repos(runtime, info.instance_id, env)

    # -- git snapshot helpers -------------------------------------------------

    async def _git_list_entry(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build a lightweight list entry for one repository root.

        Only ``rev-parse --abbrev-ref HEAD`` + ``rev-parse HEAD`` (no
        status, no log, no for-each-ref).  Detached HEAD (abbrev ``HEAD``)
        and unborn repos (no ``HEAD`` commit) map to
        ``current_branch=None``; unborn additionally maps to
        ``head_hash=None``.
        """
        timeout = git_ops.GIT_READ_TIMEOUT_S
        exit_code_b, abbrev_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        current_branch: str | None = None
        if exit_code_b == 0 and abbrev_out.strip():
            head_name = abbrev_out.strip().splitlines()[0].strip()
            # Detached HEAD reports literally "HEAD".
            current_branch = None if head_name in {"", "HEAD"} else head_name
        exit_code_h, head_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "HEAD"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        head_hash: str | None = None
        if exit_code_h == 0 and head_out.strip():
            head_hash = head_out.strip().splitlines()[0].strip() or None
        if head_hash is None:
            # Unborn repo: no commit exists, so no branch is meaningful
            # even when rev-parse --abbrev-ref echoed one.
            current_branch = None
        name = repo_root.rstrip("/").rsplit("/", 1)[-1] or "workspace"
        return {
            "id": repo_root,
            "name": name,
            "path": repo_root,
            "current_branch": current_branch,
            "head_hash": head_hash,
        }

    async def _git_repo_snapshot_no_log(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build the snapshot dict for one repository root (no history)."""
        timeout = git_ops.GIT_READ_TIMEOUT_S

        exit_code, status_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "status", "--porcelain=v2", "--branch",
             "--untracked-files=all", "-z"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        changes: list[dict[str, Any]] = []
        branch_header: dict[str, Any] = {
            "oid": None, "head": None, "upstream": None, "ahead": 0, "behind": 0,
        }
        status_ok = exit_code == 0
        if status_ok:
            changes, branch_header = git_ops.changes_from_status_v2(status_out)
        else:
            # Fall back to v1 when v2 is unavailable (old git).
            exit_code1, v1_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "status", "--porcelain=v1", "-b", "-z"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            if exit_code1 != 0:
                raise git_ops.GitError(
                    "git_failed",
                    "git status failed",
                    exit_code=exit_code,
                    stderr=status_out,
                )
            exit_code_h, head_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "rev-parse", "HEAD"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            head_hash = head_out.strip().splitlines()[0].strip() if exit_code_h == 0 else ""
            changes, branch_header = git_ops.parse_status_v1(v1_out, head_hash)
            branch_header["oid"] = head_hash or None

        if branch_header.get("oid") is None:
            exit_code_h, head_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "rev-parse", "HEAD"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            if exit_code_h == 0 and head_out.strip():
                branch_header["oid"] = head_out.strip().splitlines()[0].strip()

        # Branch list with upstream tracking + remote refs.
        us = git_ops._GIT_US
        rs = git_ops._GIT_RS
        ref_format = (
            f"%(refname){us}%(refname:short){us}%(objectname){us}"
            f"%(objecttype){us}%(upstream:short){us}%(upstream:track){us}%(HEAD)"
        )
        exit_code_r, refs_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "for-each-ref", f"--format={ref_format}{rs}",
             "refs/heads", "refs/remotes", "refs/tags"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        branches: list[dict[str, Any]] = []
        remote_refs: list[dict[str, Any]] = []
        if exit_code_r == 0:
            parsed_branches, parsed_remotes = git_ops.parse_for_each_ref(refs_out)
            for item in parsed_branches:
                ahead, behind = git_ops.parse_ahead_behind(item.get("track", ""))
                branches.append(
                    {
                        "name": item["name"],
                        "tip_hash": item["tip_hash"],
                        "upstream": item.get("upstream"),
                        "ahead": ahead,
                        "behind": behind,
                    }
                )
            branches.sort(key=lambda item: item["name"])
            for item in parsed_remotes:
                remote_refs.append({"name": item["name"], "tip_hash": item["tip_hash"]})
            remote_refs.sort(key=lambda item: item["name"])

        exit_code_m, remotes_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "remote"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        remotes: list[str] = []
        if exit_code_m == 0:
            remotes = sorted(
                line.strip() for line in remotes_out.splitlines() if line.strip()
            )

        exit_code_d, default_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        default_remote: str | None = None
        if exit_code_d == 0 and default_out.strip():
            default_remote = default_out.strip().splitlines()[0].strip() or None

        # Merge / rebase / cherry-pick state.
        merge_state = await self._git_merge_state(runtime, instance_id, repo_root, env)

        name = repo_root.rstrip("/").rsplit("/", 1)[-1] or "workspace"
        return {
            "id": repo_root,
            "name": name,
            "path": repo_root,
            "current_branch": branch_header.get("head"),
            "head_hash": branch_header.get("oid"),
            "branches": branches,
            "remote_refs": remote_refs,
            "remotes": remotes,
            "default_remote": default_remote,
            "upstream": branch_header.get("upstream"),
            "ahead": int(branch_header.get("ahead", 0)),
            "behind": int(branch_header.get("behind", 0)),
            "merge_state": merge_state,
            "changes": [
                {
                    "path": item["path"],
                    "old_path": item.get("old_path"),
                    "status": item["status"],
                    "staged": item.get("staged") is not None,
                    "staged_kind": item.get("staged"),
                    "unstaged": item.get("unstaged"),
                    "conflict": item.get("conflict"),
                    "diff": [],
                }
                for item in changes
            ],
        }

    async def _git_repo_history(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        *,
        limit: int = git_ops.GIT_HISTORY_DEFAULT_LIMIT,
        skip: int = 0,
        branch: str | None = None,
    ) -> dict[str, Any]:
        """Return one paginated history page for a repository root.

        Only ``git log --max-count limit+1 --skip skip`` (``--all`` when
        *branch* is None, else ``refs/heads/<branch>``).  The extra probe
        commit yields ``has_more``.
        """
        timeout = git_ops.GIT_READ_TIMEOUT_S
        capped_limit = max(1, min(int(limit), git_ops.GIT_HISTORY_MAX_LIMIT))
        capped_skip = max(0, int(skip))
        # Paginated history (newest first) + has_more probe.
        # ``--all --date-order`` covers every local branch, remote-tracking
        # ref, tag and stash entry — like vscode-git-graph — while detached
        # HEAD commits stay included (HEAD is an implicit starting point).
        # ``--exclude`` precedes ``--all`` (option order matters) to hide
        # only the internal notes fan-out (refs/notes/*); stash (refs/stash)
        # remains visible on purpose.
        us = git_ops._GIT_US
        rs = git_ops._GIT_RS
        log_format = (
            f"%H{us}%h{us}%P{us}%aN{us}%aE{us}%aI{us}"
            f"%cN{us}%cE{us}%cI{us}%s{us}%b{rs}"
        )
        argv = [
            "git", "log", "--exclude=refs/notes/*",
        ]
        if branch is not None:
            argv.append(f"refs/heads/{branch}")
        else:
            argv.append("--all")
        argv += [
            "--date-order", f"--format={log_format}",
            f"--max-count={capped_limit + 1}", f"--skip={capped_skip}",
        ]
        exit_code_l, log_out = await self._git_exec(
            runtime,
            instance_id,
            argv,
            workdir=repo_root,
            env=env,
            timeout=timeout,
        )
        commits: list[dict[str, Any]] = []
        has_more = False
        if exit_code_l == 0 and log_out.strip():
            parsed = git_ops.parse_log_us_rs(log_out)
            has_more = len(parsed) > capped_limit
            for item in parsed[:capped_limit]:
                commits.append(
                    {
                        "hash": item["hash"],
                        "message": item["message"],
                        "body": item.get("body", ""),
                        "author": item.get("author", ""),
                        "author_email": item.get("author_email", ""),
                        "timestamp": item.get("author_date", ""),
                        "author_date": item.get("author_date", ""),
                        "committer": item.get("committer", ""),
                        "committer_email": item.get("committer_email", ""),
                        "committer_date": item.get("committer_date", ""),
                        "parents": item.get("parents", []),
                    }
                )
        if exit_code_l != 0:
            # Unborn repo: no commits exist yet — empty page, not an error.
            exit_code_h, _ = await self._git_exec(
                runtime, instance_id,
                ["git", "rev-parse", "--verify", "HEAD"],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code_h != 0:
                return {
                    "commits": [],
                    "has_more": False,
                    "history_skip": capped_skip,
                    "history_limit": capped_limit,
                }
            raise git_ops.GitError(
                "git_failed",
                "git log failed",
                exit_code=exit_code_l,
                stderr=log_out,
            )
        return {
            "commits": commits,
            "has_more": has_more,
            "history_skip": capped_skip,
            "history_limit": capped_limit,
        }

    async def _git_merge_state(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Detect MERGE_HEAD / rebase / cherry-pick state files.

        Re-resolves the verified git dir (already sandbox-checked before
        every operation) fail-closed so the ``test -e`` probes below can
        never touch external metadata.  Any verification failure yields
        the neutral (no-merge) state.
        """
        state: dict[str, Any] = {
            "merging": False,
            "rebasing": False,
            "cherry_picking": False,
        }
        exit_code, git_dir_out = await self._git_exec(
            runtime,
            instance_id,
            ["git", "rev-parse", "--absolute-git-dir"],
            workdir=repo_root,
            env=env,
            timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            return state
        git_dir_raw = git_ops.parse_single_path_output(git_dir_out)
        if git_dir_raw is None or not git_dir_raw.startswith("/"):
            return state
        try:
            git_dir = await self._git_realpath_contained(
                runtime, instance_id, git_dir_raw, env,
                context="metadata", display=repo_root,
            )
        except (git_ops.GitError, ValueError):
            return state
        checks = {
            "merging": ["MERGE_HEAD"],
            "rebasing": ["rebase-merge", "rebase-apply"],
            "cherry_picking": ["CHERRY_PICK_HEAD"],
        }
        for flag, names in checks.items():
            for name in names:
                exit_code_t, _ = await self._git_exec(
                    runtime,
                    instance_id,
                    ["test", "-e", f"{git_dir}/{name}"],
                    workdir=repo_root,
                    env=env,
                    timeout=git_ops.GIT_READ_TIMEOUT_S,
                )
                if exit_code_t == 0:
                    state[flag] = True
                    break
        return state

    # -- git diff assembly -----------------------------------------------------

    async def _git_working_diff(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        changes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build staged/unstaged/untracked diff entries for a repo."""
        timeout = git_ops.GIT_READ_TIMEOUT_S
        if changes is None:
            exit_code, status_out = await self._git_exec(
                runtime,
                instance_id,
                ["git", "status", "--porcelain=v2", "--branch",
                 "--untracked-files=all", "-z"],
                workdir=repo_root,
                env=env,
                timeout=timeout,
            )
            if exit_code != 0:
                raise git_ops.GitError(
                    "git_failed", "git status failed",
                    exit_code=exit_code, stderr=status_out,
                )
            changes, _ = git_ops.changes_from_status_v2(status_out)

        staged_entries: list[dict[str, Any]] = []
        unstaged_entries: list[dict[str, Any]] = []

        staged_paths = sorted(
            {item["path"] for item in changes if item.get("staged") is not None}
        )
        unstaged_paths = sorted(
            {
                item["path"]
                for item in changes
                if item.get("unstaged") not in (None,)
            }
        )

        if staged_paths:
            staged_patches = await self._git_diff_paths(
                runtime, instance_id, repo_root, env,
                ["git", "diff", "--cached", "--no-color", "--no-ext-diff",
                 "--src-prefix=a/", "--dst-prefix=b/",
                 f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                 "--patch", "--no-commit-id", "--", *staged_paths],
            )
            staged_numstat = await self._git_diff_numstat_raw(
                runtime, instance_id, repo_root, env,
                ["git", "diff", "--cached", "--no-color", "--no-ext-diff",
                 "--numstat", "-z", "--", *staged_paths],
                ["git", "diff", "--cached", "--no-color", "--no-ext-diff",
                 "--raw", "-z", "--", *staged_paths],
            )
            staged_entries = self._git_join_diff_parts(staged_patches, staged_numstat)

        if unstaged_paths:
            tracked_unstaged = sorted(
                item["path"]
                for item in changes
                if item.get("unstaged") not in (None, "untracked")
            )
            untracked = sorted(
                item["path"]
                for item in changes
                if item.get("unstaged") == "untracked"
            )
            combined: list[dict[str, Any]] = []
            if tracked_unstaged:
                entries = await self._git_diff_paths(
                    runtime, instance_id, repo_root, env,
                    ["git", "diff", "--no-color", "--no-ext-diff",
                     "--src-prefix=a/", "--dst-prefix=b/",
                     f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                     "--patch", "--no-commit-id", "--", *tracked_unstaged],
                )
                numstat = await self._git_diff_numstat_raw(
                    runtime, instance_id, repo_root, env,
                    ["git", "diff", "--no-color", "--no-ext-diff",
                     "--numstat", "-z", "--", *tracked_unstaged],
                    ["git", "diff", "--no-color", "--no-ext-diff",
                     "--raw", "-z", "--", *tracked_unstaged],
                )
                combined.extend(self._git_join_diff_parts(entries, numstat))
            for rel in untracked:
                combined.append(
                    await self._git_untracked_entry(
                        runtime, instance_id, repo_root, env, rel
                    )
                )
            combined.sort(key=lambda item: item["new_path"])
            unstaged_entries = combined

        staged_entries.sort(key=lambda item: item["new_path"])
        return {"staged": staged_entries, "unstaged": unstaged_entries}

    async def _git_diff_paths(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        argv: list[str],
    ) -> dict[str, str]:
        """Run a patch diff argv and split per-file patches."""
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git diff failed",
                exit_code=exit_code, stderr=output,
            )
        return git_ops.split_patch_per_file(output)

    async def _git_diff_numstat_raw(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        numstat_argv: list[str],
        raw_argv: list[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Run numstat + raw diff argvs and parse them."""
        exit_code_n, numstat_out = await self._git_exec(
            runtime, instance_id, numstat_argv,
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        numstat: dict[str, dict[str, Any]] = {}
        if exit_code_n == 0:
            numstat = git_ops.parse_numstat_z(numstat_out)
        exit_code_r, raw_out = await self._git_exec(
            runtime, instance_id, raw_argv,
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        raw: dict[str, dict[str, Any]] = {}
        if exit_code_r == 0:
            raw = git_ops.parse_raw_z(raw_out)
        return numstat, raw

    def _git_join_diff_parts(
        self,
        patches: dict[str, str],
        numstat_raw: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Join patch/numstat/raw parts into file change dicts."""
        numstat, raw = numstat_raw
        return git_ops.build_file_changes(raw=raw, numstat=numstat, patches=patches)

    async def _git_untracked_entry(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        rel: str,
    ) -> dict[str, Any]:
        """Render an untracked path as an added-file diff entry."""
        rel_validated = git_ops.normalize_relative_path(rel)
        timeout = git_ops.GIT_READ_TIMEOUT_S
        exit_code, output = await self._git_exec(
            runtime, instance_id,
            ["git", "diff", "--no-index", "--no-color", "--no-ext-diff",
             "--src-prefix=a/", "--dst-prefix=b/",
             f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
             "--patch", "--", "/dev/null", rel_validated],
            workdir=repo_root, env=env, timeout=timeout,
        )
        # --no-index exits 1 when a diff exists; that is the happy path.
        if exit_code not in {0, 1}:
            # Fall back to a capped direct read when diff fails (e.g. the
            # path is a directory or unreadable).
            return await self._git_untracked_fallback(
                runtime, instance_id, repo_root, env, rel_validated
            )
        patches = git_ops.split_patch_per_file(output)
        # --no-index labels paths oddly; take the single patch if present.
        patch_text = next(iter(patches.values()), "")
        hunks, has_textual = git_ops.parse_patch_hunks(patch_text[: git_ops.GIT_MAX_DIFF_BYTES + 64])
        truncated = len(patch_text.encode("utf-8", "ignore")) > git_ops.GIT_MAX_DIFF_BYTES
        binary = bool(patch_text) and not has_textual and "Binary files " in patch_text
        return {
            "old_path": rel_validated,
            "new_path": rel_validated,
            "status": "A",
            "additions": sum(
                1 for hunk in hunks for line in hunk["lines"] if line["type"] == "add"
            ),
            "deletions": 0,
            "binary": binary,
            "truncated": truncated,
            "has_textual_diff": has_textual and not binary,
            "diff": hunks,
        }

    async def _git_untracked_fallback(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        rel: str,
    ) -> dict[str, Any]:
        """Render an unreadable/odd untracked path without patch text."""
        return {
            "old_path": rel,
            "new_path": rel,
            "status": "A",
            "additions": 0,
            "deletions": 0,
            "binary": False,
            "truncated": False,
            "has_textual_diff": False,
            "diff": [],
        }

    async def _git_commit_details(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        commit_hash: str,
    ) -> dict[str, Any]:
        """Build commit details with file changes, numstat and hunks."""
        timeout = git_ops.GIT_READ_TIMEOUT_S
        full_hash = git_ops.validate_commit_hash(commit_hash)
        us = "\x1f"
        rs = "\x1e"
        header_format = (
            f"%H{us}%P{us}%aN{us}%aE{us}%aI{us}%cN{us}%cE{us}%cI{us}%s{us}%b{rs}"
        )
        exit_code, header_out = await self._git_exec(
            runtime, instance_id,
            ["git", "show", "--no-patch", f"--format={header_format}", full_hash, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "unknown_commit", f"Unknown commit: {commit_hash}",
                exit_code=exit_code, stderr=header_out,
            )
        fields = header_out.split(rs)[0].split(us)
        if len(fields) < 10:
            raise git_ops.GitError(
                "unknown_commit", f"Unknown commit: {commit_hash}",
                exit_code=exit_code, stderr=header_out,
            )
        full, parents_raw, author, author_email, author_date = fields[0].strip(), fields[1], fields[2].strip(), fields[3].strip(), fields[4].strip()
        committer, committer_email, committer_date = fields[5].strip(), fields[6].strip(), fields[7].strip()
        subject, body = fields[8].strip(), fields[9].strip("\n")
        exit_code_r, parents_check = await self._git_exec(
            runtime, instance_id,
            ["git", "rev-list", "--parents", "-n", "1", full],
            workdir=repo_root, env=env, timeout=timeout,
        )
        parents = parents_raw.split() if parents_raw.strip() else []
        if exit_code_r == 0 and parents_check.strip():
            tokens = parents_check.strip().split()
            parents = tokens[1:] if len(tokens) > 1 else []

        is_root = len(parents) == 0
        if is_root:
            numstat_argv = ["git", "diff-tree", "--root", "--no-commit-id",
                            "--numstat", "-z", "-r", "-M", full, "--"]
            raw_argv = ["git", "diff-tree", "--root", "--no-commit-id",
                        "--raw", "-z", "-r", "-M", full, "--"]
            patch_argv = ["git", "show", "--no-color", "--no-ext-diff",
                          "--pretty=format:", "--patch",
                          f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                          "--src-prefix=a/", "--dst-prefix=b/", "-M", full, "--"]
        else:
            numstat_argv = ["git", "diff-tree", "--no-commit-id",
                            "--numstat", "-z", "-r", "-M",
                            f"{parents[0]}", full, "--"]
            raw_argv = ["git", "diff-tree", "--no-commit-id",
                        "--raw", "-z", "-r", "-M",
                        f"{parents[0]}", full, "--"]
            patch_argv = ["git", "diff", "--no-color", "--no-ext-diff",
                          f"{parents[0]}", full, "--patch",
                          f"--unified={git_ops.GIT_DIFF_CONTEXT_LINES}",
                          "--src-prefix=a/", "--dst-prefix=b/", "-M", "--"]
        numstat, raw = await self._git_diff_numstat_raw(
            runtime, instance_id, repo_root, env, numstat_argv, raw_argv
        )
        exit_code_p, patch_out = await self._git_exec(
            runtime, instance_id, patch_argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        patches = git_ops.split_patch_per_file(patch_out) if exit_code_p == 0 else {}
        file_changes = git_ops.build_file_changes(raw=raw, numstat=numstat, patches=patches)
        return {
            "hash": full,
            "message": subject,
            "body": body,
            "author": author,
            "author_email": author_email,
            "author_date": author_date,
            "committer": committer,
            "committer_email": committer_email,
            "committer_date": committer_date,
            "parents": parents,
            "file_changes": file_changes,
        }

    # -- git RPC entry point ---------------------------------------------------

    async def execute_git_operation(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        repo_path: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one whitelisted git operation inside a workspace.

        Single RPC entry point for productive git integration.  All repo
        paths are resolved fail-closed under ``/workspace`` and verified
        as repository roots.  Mutations return a fresh ``snapshot`` so the
        backend/webapp can update atomically.

        Args:
            workspace_id: Target workspace.
            operation: Whitelisted operation name (see
                :data:`src.git.GIT_OPERATIONS`).
            repo_path: Absolute repo path under ``/workspace``.  Omitted
                for ``list_repos`` (discovers all repos).
            args: Operation-specific arguments (paths, branch names,
                messages, pagination cursors).

        Returns:
            JSON-serialisable dict with ``ok`` plus ``snapshot`` /
            ``repos`` / ``diff`` / ``details`` payloads, or a structured
            error (``ok=False``, ``code``, ``message``, ``stderr``).
        """
        params = dict(args or {})
        if operation not in git_ops.GIT_OPERATIONS:
            return {
                "ok": False,
                "code": "unknown_operation",
                "message": f"Unknown git operation: {operation}",
                "stderr": "",
                "exit_code": None,
            }
        try:
            info = self._get_cached(workspace_id)
            runtime = self._get_runtime(workspace_id)
            if not info.instance_id:
                raise RuntimeError("Workspace has no instance assigned")
        except (ValueError, RuntimeError) as exc:
            return git_ops.git_error_payload(exc)

        timeout = git_ops.git_timeout_for(operation)
        try:
            outcome = await asyncio.wait_for(
                self._execute_git_operation_inner(
                    workspace_id, info.instance_id, runtime,
                    operation, repo_path, params,
                ),
                timeout + 30.0,
            )
            if not isinstance(outcome, dict):
                return {
                    "ok": False, "code": "git_failed",
                    "message": "Git operation returned no result",
                    "stderr": "", "exit_code": None,
                }
            outcome.setdefault("ok", True)
            return outcome
        except asyncio.TimeoutError:
            return {
                "ok": False, "code": "timeout",
                "message": "Git operation timed out",
                "stderr": "", "exit_code": None,
            }
        except Exception as exc:  # noqa: BLE001 - contract is structured errors
            return git_ops.git_error_payload(exc)

    async def _execute_git_operation_inner(
        self,
        workspace_id: uuid.UUID,
        instance_id: str,
        runtime: RuntimeBackend,
        operation: str,
        repo_path: str | None,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the git operation body (locks + auth + dispatch).

        The Git RPC accepts no caller-provided env and no unknown
        operation-specific args: both are rejected fail-closed via
        :func:`src.git.check_git_args_allowed` before any askpass/repo
        work, so ``env``/``GIT_CONFIG_*``/``PATH``/token injection via a
        future REST/MCP caller is impossible.  Auth comes only from the
        secure base env plus the persistent workspace credential file
        (sourced inside the workspace by the exec wrapper) and the
        throwaway askpass script derived from it.
        """
        timeout = git_ops.git_timeout_for(operation)
        # Defense-in-depth strict allow-list (mirrors the backend map).
        # Runs before any askpass probe or repo command.
        git_ops.check_git_args_allowed(operation, params)
        # Pull contract: branch without remote is a strict reject before
        # any askpass/repo work (mirrors backend/MCP/REST validation).
        if operation == "pull" and params.get("branch") not in (None, ""):
            remote = params.get("remote")
            if remote is None or str(remote).strip() == "":
                raise ValueError("remote is required when branch is set for pull")
        async with git_ops.github_askpass_context(
            runtime, instance_id
        ) as askpass:
            env = git_ops.build_git_env(askpass_script=askpass)
            if operation == "list_repos":
                repos = await self._git_discover_repos(runtime, instance_id, env)
                entries: list[dict[str, Any]] = []
                for root in repos:
                    lock = await self._git_lock(workspace_id, root)
                    async with lock:
                        entries.append(
                            await self._git_list_entry(
                                runtime, instance_id, root, env
                            )
                        )
                return {"ok": True, "repos": entries}

            # All other operations require an explicit repository root.
            if not repo_path:
                raise ValueError(f"repo_path is required for operation {operation!r}")
            repo_root = await self._git_resolve_repo_root(
                runtime, instance_id, repo_path, env
            )
            if operation in ("repo_snapshot", "repo_history"):
                lock = await self._git_lock(workspace_id, repo_root)
                async with lock:
                    if operation == "repo_snapshot":
                        snapshot = await self._git_repo_snapshot_no_log(
                            runtime, instance_id, repo_root, env
                        )
                        history = await self._git_repo_history(
                            runtime, instance_id, repo_root, env,
                            limit=git_ops.GIT_HISTORY_PAGE_SIZE,
                            skip=0,
                        )
                        snapshot.update(history)
                        return {"ok": True, "snapshot": snapshot}
                    branch = git_ops.validate_optional_branch(
                        params.get("branch")
                    )
                    if branch is not None and not await self._git_verify_branch_exists(
                        runtime, instance_id, repo_root, env, branch
                    ):
                        raise git_ops.GitError(
                            "unknown_branch", f"Unknown branch: {branch}",
                            exit_code=None, stderr="",
                        )
                    try:
                        history_limit = int(
                            params.get(
                                "history_limit",
                                git_ops.GIT_HISTORY_DEFAULT_LIMIT,
                            )
                        )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid history_limit: {params.get('history_limit')!r}"
                        ) from exc
                    try:
                        history_skip = int(params.get("history_skip", 0))
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Invalid history_skip: {params.get('history_skip')!r}"
                        ) from exc
                    history = await self._git_repo_history(
                        runtime, instance_id, repo_root, env,
                        limit=history_limit, skip=history_skip,
                        branch=branch,
                    )
                    return {"ok": True, "repo_path": repo_root, **history}
            lock = await self._git_lock(workspace_id, repo_root)
            async with lock:
                handler = {
                    "working_diff": self._git_op_working_diff,
                    "commit_details": self._git_op_commit_details,
                    "stage": self._git_op_stage,
                    "unstage": self._git_op_unstage,
                    "discard": self._git_op_discard,
                    "commit": self._git_op_commit,
                    "fetch": self._git_op_fetch,
                    "pull": self._git_op_pull,
                    "push": self._git_op_push,
                    "sync": self._git_op_sync,
                    "checkout_branch": self._git_op_checkout_branch,
                    "checkout_commit": self._git_op_checkout_commit,
                    "checkout_remote_branch": self._git_op_checkout_remote_branch,
                    "create_branch": self._git_op_create_branch,
                    "rename_branch": self._git_op_rename_branch,
                    "delete_branch": self._git_op_delete_branch,
                    "merge_into_current": self._git_op_merge_into_current,
                    "merge_current_into": self._git_op_merge_current_into,
                    "merge_abort": self._git_op_merge_abort,
                }[operation]
                return await handler(
                    runtime, instance_id, repo_root, env, params, timeout,
                    workspace_id=workspace_id,
                )

    async def _git_fresh_snapshot(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Return a fresh single-repo snapshot after a mutation."""
        snapshot = await self._git_repo_snapshot_no_log(
            runtime, instance_id, repo_root, env
        )
        history = await self._git_repo_history(
            runtime, instance_id, repo_root, env,
            limit=git_ops.GIT_HISTORY_PAGE_SIZE, skip=0,
        )
        snapshot.update(history)
        return snapshot

    def _git_mutation_result(
        self, snapshot: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        """Wrap a mutation outcome with its fresh snapshot."""
        return {"ok": True, "snapshot": snapshot, **extra}

    # -- read operations --------------------------------------------------------

    async def _git_op_working_diff(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Return staged vs unstaged working-tree diffs."""
        diff = await self._git_working_diff(runtime, instance_id, repo_root, env)
        return {"ok": True, "repo_path": repo_root, "diff": diff}

    async def _git_op_commit_details(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Return full details for one commit."""
        commit = params.get("commit") or ""
        if not commit:
            raise ValueError("commit is required for commit_details")
        details = await self._git_commit_details(
            runtime, instance_id, repo_root, env, str(commit)
        )
        return {"ok": True, "repo_path": repo_root, "details": details}

    # -- staging operations -----------------------------------------------------

    @staticmethod
    def _git_require_paths(params: dict[str, Any], operation: str) -> list[str]:
        """Extract and validate repo-relative paths from *params*."""
        raw = params.get("paths", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"paths is required for operation {operation!r}")
        paths = [git_ops.normalize_relative_path(str(item)) for item in raw]
        if len(paths) > 256:
            raise ValueError("Too many paths (max 256)")
        return paths

    async def _git_op_stage(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Stage paths via ``git add -- <paths>``."""
        paths = self._git_require_paths(params, "stage")
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "add", "--", *paths],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git add failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_unstage(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Unstage paths (unborn-safe via ``rm --cached`` fallback)."""
        paths = self._git_require_paths(params, "unstage")
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "restore", "--staged", "--", *paths],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0 and "could not resolve HEAD" in output:
            # Unborn HEAD: nothing to restore from; staged additions are
            # dropped from the index instead (matches VS Code behaviour).
            exit_code, output = await self._git_exec(
                runtime, instance_id, ["git", "rm", "--cached", "-r", "--", *paths],
                workdir=repo_root, env=env, timeout=timeout,
            )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git unstage failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_discard(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Discard worktree changes; untracked paths are cleaned exactly."""
        paths = self._git_require_paths(params, "discard")
        exit_code, status_out = await self._git_exec(
            runtime, instance_id,
            ["git", "status", "--porcelain=v2", "--branch",
             "--untracked-files=all", "-z"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git status failed",
                exit_code=exit_code, stderr=status_out,
            )
        changes, _ = git_ops.changes_from_status_v2(status_out)
        by_path = {item["path"]: item for item in changes}
        to_restore: list[str] = []
        to_clean: list[str] = []
        for rel in paths:
            state = by_path.get(rel)
            if state is None:
                continue
            if state.get("unstaged") == "untracked":
                to_clean.append(rel)
            else:
                to_restore.append(rel)
        if to_restore:
            exit_code, output = await self._git_exec(
                runtime, instance_id,
                ["git", "restore", "--worktree", "--", *to_restore],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code != 0:
                raise git_ops.GitError(
                    "git_failed", "git discard failed",
                    exit_code=exit_code, stderr=output,
                )
        for rel in to_clean:
            exit_code, output = await self._git_exec(
                runtime, instance_id, ["git", "clean", "-f", "--", rel],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code != 0:
                raise git_ops.GitError(
                    "git_failed", "git clean failed",
                    exit_code=exit_code, stderr=output,
                )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    # -- commit ------------------------------------------------------------------

    async def _git_op_commit(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Commit the index only; message passed as a single argv element.

        Git argv runs behind the fixed sourcing wrapper (persistent
        credentials only), and both runtimes preserve the argv boundary
        behind ``exec "$@"`` (Docker exec argv, SSH single-quote
        escaping), so ``-m <message>`` cannot inject flags or shell
        operators.  Plain ``message`` is the transport (argv-safe).
        """
        text = str(params.get("message", "") or "")
        if not text.strip():
            raise ValueError("message is required for commit")
        # Identity fallback per missing field only: an existing
        # user.name/user.email repo value is respected as-is; only the
        # missing side is supplied via ``-c`` from the backend fallback.
        extra: list[str] = []
        exit_code_n, _ = await self._git_exec(
            runtime, instance_id, ["git", "config", "--get", "user.name"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        exit_code_e, _ = await self._git_exec(
            runtime, instance_id, ["git", "config", "--get", "user.email"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_n != 0 or exit_code_e != 0:
            # Backend-supplied fallback identity; only actually used
            # fields are validated here before ``-c`` argv use (no
            # NUL/CR/LF, length-capped).
            if exit_code_n != 0:
                author_name = git_ops.validate_author_name(
                    str(params.get("author_name", "") or "opencuria"),
                )
                extra += ["-c", f"user.name={author_name}"]
            if exit_code_e != 0:
                author_email = git_ops.validate_author_email(
                    str(params.get("author_email", "") or "opencuria@localhost"),
                )
                extra += ["-c", f"user.email={author_email}"]
        commit_argv = ["git", *extra, "commit", "--quiet",
                       "--allow-empty-message", "-m", text]
        exit_code, output = await self._git_exec(
            runtime, instance_id, commit_argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "nothing to commit" in lowered or "no changes added" in lowered:
                raise git_ops.GitError(
                    "nothing_to_commit", "Nothing to commit",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git commit failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    # -- network operations --------------------------------------------------------

    def _git_remote_arg(self, params: dict[str, Any]) -> str | None:
        """Return the validated remote name, if any."""
        remote = params.get("remote")
        if remote is None or str(remote).strip() == "":
            return None
        name = str(remote).strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
            raise ValueError(f"Invalid remote: {remote!r}")
        return name

    async def _git_op_fetch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Fetch with prune (non-interactive, PAT via askpass)."""
        argv = ["git", "fetch", "--prune"]
        remote = self._git_remote_arg(params)
        if remote:
            argv.append(remote)
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "network_failed", "git fetch failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_current_branch(
        self, runtime, instance_id, repo_root, env
    ) -> str | None:
        """Return the current branch name, or None when detached/unborn."""
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "branch", "--show-current"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code != 0:
            return None
        name = output.strip().splitlines()[0].strip() if output.strip() else ""
        return name or None

    async def _git_op_pull(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Pull following repo config (ff/merge), never interactive."""
        argv = ["git", "pull", "--no-edit"]
        remote = self._git_remote_arg(params)
        branch = params.get("branch")
        if branch not in (None, "") and remote is None:
            raise ValueError("remote is required when branch is set for pull")
        if remote:
            argv.append(remote)
            if branch:
                argv.append(git_ops.validate_branch_name(str(branch)))
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "no tracking information" in lowered or "no upstream" in lowered:
                raise git_ops.GitError(
                    "no_upstream", "No upstream configured for the current branch",
                    exit_code=exit_code, stderr=output,
                )
            if "conflict" in lowered or "automatic merge failed" in lowered:
                snapshot = await self._git_fresh_snapshot(
                    runtime, instance_id, repo_root, env
                )
                result = self._git_mutation_result(snapshot, repo_path=repo_root)
                result["conflict"] = True
                result["ok"] = False
                result["code"] = "conflict"
                result["message"] = "Merge conflict — resolve or run merge_abort"
                return result
            raise git_ops.GitError(
                "network_failed", "git pull failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_push(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Push following the branch upstream when one exists.

        Matches VS Code/Git semantics: with an upstream configured and
        no explicit remote (and ``set_upstream`` not true), run plain
        ``git push`` so branch push config/upstream wins.  An explicit
        remote is honoured as ``git push <remote>``.  Without upstream
        (or when ``set_upstream`` is true) set it via
        ``git push -u <explicit remote or origin> <current>``.
        """
        current = await self._git_current_branch(runtime, instance_id, repo_root, env)
        if current is None:
            raise git_ops.GitError(
                "detached_head", "Cannot push while HEAD is detached",
                exit_code=None, stderr="",
            )
        explicit_remote = self._git_remote_arg(params)
        exit_code_u, upstream_out = await self._git_exec(
            runtime, instance_id,
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        has_upstream = exit_code_u == 0 and bool(upstream_out.strip())
        set_upstream = bool(params.get("set_upstream"))
        if has_upstream and not set_upstream:
            if explicit_remote is not None:
                argv = ["git", "push", explicit_remote]
            else:
                argv = ["git", "push"]
        else:
            argv = ["git", "push", "-u", explicit_remote or "origin", current]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "network_failed", "git push failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_sync(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Sync = pull, then push when ahead of upstream."""
        pull_result = await self._git_op_pull(
            runtime, instance_id, repo_root, env, params, timeout
        )
        if pull_result.get("ok") is False:
            return pull_result
        snapshot = pull_result.get("snapshot", {})
        if int(snapshot.get("ahead", 0)) > 0:
            return await self._git_op_push(
                runtime, instance_id, repo_root, env, params, timeout
            )
        return pull_result

    # -- branch operations ---------------------------------------------------------

    async def _git_verify_branch_exists(
        self, runtime, instance_id, repo_root, env, branch: str
    ) -> bool:
        """Return True when local branch *branch* exists."""
        exit_code, _ = await self._git_exec(
            runtime, instance_id,
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        return exit_code == 0

    async def _git_verify_ref_exists(
        self, runtime, instance_id, repo_root, env, ref: str
    ) -> bool:
        """Return True when *ref* resolves (branch, tag or commit)."""
        exit_code, _ = await self._git_exec(
            runtime, instance_id,
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        return exit_code == 0

    async def _git_op_checkout_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Checkout a local branch (dirty-worktree errors surface)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        exit_code_c, _ = await self._git_exec(
            runtime, instance_id, ["git", "check-ref-format", "--branch", branch],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c != 0:
            raise ValueError(f"Invalid branch: {branch!r}")
        if not await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {branch}",
                exit_code=None, stderr="",
            )
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "checkout", branch, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "commit your changes or stash them" in lowered:
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_checkout_commit(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Detach HEAD at *commit* (dirty-worktree errors surface)."""
        commit = git_ops.validate_commit_hash(str(params.get("commit", "")))
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "checkout", "--detach", commit, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "commit your changes or stash them" in lowered:
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code, stderr=output,
                )
            if "unknown revision" in lowered or "bad revision" in lowered:
                raise git_ops.GitError(
                    "unknown_commit", f"Unknown commit: {commit}",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_checkout_remote_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Track a remote branch locally (fetch + checkout/create)."""
        remote_ref = git_ops.validate_remote_ref(str(params.get("remote_ref", "")))
        # Default local branch: strip only the remote prefix
        # ("origin/feat/x" -> "feat/x", like `git switch <remote_ref>`).
        _remote, _, _short = remote_ref.partition("/")
        local_raw = params.get("local_name")
        local = (
            git_ops.validate_local_branch(str(local_raw))
            if local_raw not in (None, "")
            else git_ops.validate_local_branch(_short)
        )
        exit_code_f, fetch_out = await self._git_exec(
            runtime, instance_id, ["git", "fetch", _remote],
            workdir=repo_root, env=env, timeout=git_ops.GIT_NETWORK_TIMEOUT_S,
        )
        if exit_code_f != 0:
            raise git_ops.GitError(
                "network_failed", "git fetch failed",
                exit_code=exit_code_f, stderr=fetch_out,
            )
        exit_code_r, _ = await self._git_exec(
            runtime, instance_id,
            ["git", "show-ref", "--verify", "--quiet",
             f"refs/remotes/{remote_ref}"],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_r != 0:
            raise git_ops.GitError(
                "unknown_branch", f"Unknown remote branch: {remote_ref}",
                exit_code=exit_code_r, stderr="",
            )
        if await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, local
        ):
            argv = ["git", "checkout", local, "--"]
        else:
            argv = ["git", "checkout", "-b", local, "--track", remote_ref, "--"]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if (
                "commit your changes or stash them" in lowered
                or "overwritten by checkout" in lowered
            ):
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_create_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Create a branch; optionally check it out (``checkout=True``)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        exit_code_c, _ = await self._git_exec(
            runtime, instance_id, ["git", "check-ref-format", "--branch", branch],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c != 0:
            raise ValueError(f"Invalid branch: {branch!r}")
        if await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "branch_exists", f"Branch already exists: {branch}",
                exit_code=None, stderr="",
            )
        start = params.get("start_point")
        argv = ["git", "branch", "--", branch]
        if start:
            start_ref = str(start).strip()
            # Accept hashes or validated branch names as start points.
            try:
                start_ref = git_ops.validate_commit_hash(start_ref)
            except ValueError:
                start_ref = git_ops.validate_branch_name(start_ref, field="start_point")
            if not await self._git_verify_ref_exists(
                runtime, instance_id, repo_root, env, start_ref
            ):
                raise git_ops.GitError(
                    "unknown_commit", f"Unknown start point: {start}",
                    exit_code=None, stderr="",
                )
            argv.append(start_ref)
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git branch failed",
                exit_code=exit_code, stderr=output,
            )
        if params.get("checkout"):
            exit_code_o, output_o = await self._git_exec(
                runtime, instance_id, ["git", "checkout", branch, "--"],
                workdir=repo_root, env=env, timeout=timeout,
            )
            if exit_code_o != 0:
                raise git_ops.GitError(
                    "git_failed", "git checkout failed",
                    exit_code=exit_code_o, stderr=output_o,
                )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_rename_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Rename a branch (defaults to the current branch)."""
        old = str(params.get("old_branch") or "").strip()
        new = git_ops.validate_branch_name(str(params.get("new_branch", "")), field="new_branch")
        exit_code_c, _ = await self._git_exec(
            runtime, instance_id, ["git", "check-ref-format", "--branch", new],
            workdir=repo_root, env=env, timeout=git_ops.GIT_READ_TIMEOUT_S,
        )
        if exit_code_c != 0:
            raise ValueError(f"Invalid branch: {new!r}")
        if await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, new
        ):
            raise git_ops.GitError(
                "branch_exists", f"Branch already exists: {new}",
                exit_code=None, stderr="",
            )
        if old:
            old_validated = git_ops.validate_branch_name(old, field="old_branch")
            if not await self._git_verify_branch_exists(
                runtime, instance_id, repo_root, env, old_validated
            ):
                raise git_ops.GitError(
                    "unknown_branch", f"Unknown branch: {old_validated}",
                    exit_code=None, stderr="",
                )
            argv = ["git", "branch", "-m", "--", old_validated, new]
        else:
            current = await self._git_current_branch(runtime, instance_id, repo_root, env)
            if current is None:
                raise git_ops.GitError(
                    "detached_head", "Cannot rename while HEAD is detached",
                    exit_code=None, stderr="",
                )
            argv = ["git", "branch", "-m", "--", new]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git branch rename failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    async def _git_op_delete_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Safe-delete a branch (``-d`` only; current branch protected)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        current = await self._git_current_branch(runtime, instance_id, repo_root, env)
        if current is not None and current == branch:
            raise git_ops.GitError(
                "current_branch", "Cannot delete the current branch",
                exit_code=None, stderr="",
            )
        if not await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {branch}",
                exit_code=None, stderr="",
            )
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "branch", "-d", "--", branch],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            if "not fully merged" in lowered:
                raise git_ops.GitError(
                    "not_merged", f"Branch is not fully merged: {branch}",
                    exit_code=exit_code, stderr=output,
                )
            raise git_ops.GitError(
                "git_failed", "git branch delete failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

    # -- merge operations ----------------------------------------------------------

    def _git_merge_msg(self, params: dict[str, Any], default: str) -> list[str]:
        """Return ``-m <message>`` argv for merges (single argv element)."""
        message = params.get("message")
        if message is None or str(message).strip() == "":
            return []
        text = str(message)
        if len(text) > 4096:
            raise ValueError("merge message too long (max 4096 chars)")
        return ["-m", text]

    async def _git_op_merge_into_current(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Merge *branch* into the current branch (fast-forward allowed)."""
        branch = git_ops.validate_branch_name(str(params.get("branch", "")))
        if not await self._git_verify_ref_exists(
            runtime, instance_id, repo_root, env, branch
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {branch}",
                exit_code=None, stderr="",
            )
        argv = ["git", "merge", "--no-edit", *self._git_merge_msg(params, branch),
                "--", branch]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        if exit_code != 0:
            lowered = output.lower()
            if "conflict" in lowered or "automatic merge failed" in lowered:
                result = self._git_mutation_result(snapshot, repo_path=repo_root)
                result["conflict"] = True
                result["ok"] = False
                result["code"] = "conflict"
                result["message"] = "Merge conflict — resolve or run merge_abort"
                result["stderr"] = git_ops._redact(output)
                return result
            raise git_ops.GitError(
                "git_failed", "git merge failed",
                exit_code=exit_code, stderr=output,
            )
        result = self._git_mutation_result(snapshot, repo_path=repo_root)
        result["conflict"] = False
        return result

    async def _git_op_merge_current_into(
        self, runtime, instance_id, repo_root, env, params, timeout,
        workspace_id: uuid.UUID | None = None, **_: Any
    ) -> dict[str, Any]:
        """Merge the current branch into *target* and return to the start branch.

        The target branch is checked out temporarily and the original
        (source) branch is merged into it.  On success the runner checks
        back out to the original branch.  On conflict it *stays* on the
        target so the user can resolve/abort there — the result payload
        reports ``stayed_on_target=True`` and the conflict flag.  On a
        non-conflict merge failure the runner makes a best-effort return
        to the source branch (conflict state, if any, stays for manual
        resolution).
        """
        target = git_ops.validate_branch_name(str(params.get("target", "")), field="target")
        if not await self._git_verify_branch_exists(
            runtime, instance_id, repo_root, env, target
        ):
            raise git_ops.GitError(
                "unknown_branch", f"Unknown branch: {target}",
                exit_code=None, stderr="",
            )
        source = await self._git_current_branch(runtime, instance_id, repo_root, env)
        if source is None:
            raise git_ops.GitError(
                "detached_head", "Cannot merge while HEAD is detached",
                exit_code=None, stderr="",
            )
        if source == target:
            raise ValueError("source and target branches must differ")
        exit_code_o, output_o = await self._git_exec(
            runtime, instance_id, ["git", "checkout", target, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code_o != 0:
            lowered = output_o.lower()
            if "commit your changes or stash them" in lowered:
                raise git_ops.GitError(
                    "dirty_worktree",
                    "Working tree has uncommitted changes",
                    exit_code=exit_code_o, stderr=output_o,
                )
            raise git_ops.GitError(
                "git_failed", "git checkout failed",
                exit_code=exit_code_o, stderr=output_o,
            )
        argv = ["git", "merge", "--no-edit", *self._git_merge_msg(params, source),
                "--", source]
        exit_code, output = await self._git_exec(
            runtime, instance_id, argv,
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            lowered = output.lower()
            snapshot = await self._git_fresh_snapshot(
                runtime, instance_id, repo_root, env
            )
            if "conflict" in lowered or "automatic merge failed" in lowered:
                result = self._git_mutation_result(snapshot, repo_path=repo_root)
                result["conflict"] = True
                result["stayed_on_target"] = True
                result["target"] = target
                result["source"] = source
                result["ok"] = False
                result["code"] = "conflict"
                result["message"] = (
                    f"Merge conflict on {target} — resolve or run merge_abort"
                )
                result["stderr"] = git_ops._redact(output)
                return result
            # Non-conflict failure: best-effort return to the source branch.
            try:
                await self._git_exec(
                    runtime, instance_id, ["git", "checkout", source, "--"],
                    workdir=repo_root, env=env, timeout=timeout,
                )
            except Exception:  # noqa: BLE001 - best effort return
                pass
            raise git_ops.GitError(
                "git_failed", "git merge failed",
                exit_code=exit_code, stderr=output,
            )
        # Success: return to the original branch.
        exit_code_b, output_b = await self._git_exec(
            runtime, instance_id, ["git", "checkout", source, "--"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code_b != 0:
            raise git_ops.GitError(
                "git_failed",
                f"Merged into {target} but could not return to {source}",
                exit_code=exit_code_b, stderr=output_b,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        result = self._git_mutation_result(snapshot, repo_path=repo_root)
        result["conflict"] = False
        result["stayed_on_target"] = False
        result["target"] = target
        result["source"] = source
        return result

    async def _git_op_merge_abort(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Abort an in-progress merge (no-op error when none active)."""
        state = await self._git_merge_state(runtime, instance_id, repo_root, env)
        if not state.get("merging"):
            raise git_ops.GitError(
                "no_merge", "No merge in progress",
                exit_code=None, stderr="",
            )
        exit_code, output = await self._git_exec(
            runtime, instance_id, ["git", "merge", "--abort"],
            workdir=repo_root, env=env, timeout=timeout,
        )
        if exit_code != 0:
            raise git_ops.GitError(
                "git_failed", "git merge --abort failed",
                exit_code=exit_code, stderr=output,
            )
        snapshot = await self._git_fresh_snapshot(runtime, instance_id, repo_root, env)
        return self._git_mutation_result(snapshot, repo_path=repo_root)

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
