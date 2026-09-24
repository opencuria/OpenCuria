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
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog

from . import git as git_ops
from .config import RunnerSettings
from .models import DesktopReleaseResult, DesktopSession, WorkspaceInfo
from .runtime.base import RuntimeBackend

logger = structlog.get_logger(__name__)

# -- Step 1 canonical re-exports -------------------------------------------
# Pure/stateless helpers now live canonically in ``src.services``. They are
# re-exported here so ``src.service.<NAME>`` keeps working for existing
# importers (``src/interfaces/websocket.py``, tests). Do not add new logic
# here — edit the canonical modules instead.
from .services.exec_kernel import (  # noqa: F401,E402
    _REDIRECTION_RE,
    _SHELL_OPERATOR_TOKENS,
    ExecKernel,
    KeyedLockMap,
    WorkspaceContext,
    normalise_command_args,
    sanitize_exec_workdir,
    sanitize_filename,
    sanitize_path,
)
from .services.files import (  # noqa: F401,E402
    _FIND_FILES_QUERY_RE,
    _FIND_FILES_SUCCESS_EXIT_CODES,
    FILE_DOWNLOAD_MAX_SIZE,
    FILE_READ_ABSOLUTE_MAX_SIZE,
    FILE_READ_DEFAULT_MAX_SIZE,
    FILE_UPLOAD_MAX_SIZE,
    FIND_FILES_DEFAULT_LIMIT,
    FIND_FILES_PRUNE_NAMES,
    FileManager,
    build_find_files_command,
    build_single_file_tar,
    build_tar_entries,
    convert_archive_to_tar,
    sanitize_find_query,
)
from .services.harness_exec import HarnessExecService  # noqa: F401,E402
from .services.git_service import GitService  # noqa: F401,E402
from .services.sessions.streams import (  # noqa: F401,E402
    _TCP_HOST_RE,
    _validate_stream_host,
    _validate_stream_port,
    STREAM_BLOCKED_ENV_EXACT,
    STREAM_BLOCKED_ENV_PREFIXES,
    STREAM_CHUNK_SIZE,
    STREAM_MAX_PER_WORKSPACE,
    TCP_RELAY_CODE,
)
from .services.sessions.xdotool import (  # noqa: F401,E402
    _XDOTOOL_FUNCTION_KEY_RE,
    _XDOTOOL_KEY_ALIASES,
    _XDOTOOL_KEY_FAILURE_MARKERS,
    _XDOTOOL_MODIFIER_ALIASES,
    _collapse_xdotool_token,
    _normalize_xdotool_key_combo,
    _normalize_xdotool_token,
    _xdotool_key_failed,
    _xdotool_type_command,
)
# -- Step 2 canonical managers ------------------------------------------------
# Leaf clusters (terminals + images) now live canonically in
# ``src.services``. ``TerminalSession`` is re-exported so
# ``src.service.TerminalSession`` keeps working for existing importers.
from .services.images import ImageManager  # noqa: F401,E402
from .services.credentials import (  # noqa: F401,E402
    WORKSPACE_CREDENTIAL_BASHRC,
    WORKSPACE_CREDENTIAL_BASHRC_LINE,
    WORKSPACE_CREDENTIAL_DIR,
    WORKSPACE_CREDENTIAL_ENV_FILE,
    WORKSPACE_CREDENTIAL_ENVIRONMENT,
    WORKSPACE_CREDENTIAL_ENVIRONMENT_END,
    WORKSPACE_CREDENTIAL_ENVIRONMENT_START,
    WORKSPACE_CREDENTIAL_MANIFEST,
    WORKSPACE_CREDENTIAL_PROFILE_D,
    CredentialManager,
)
from .services.sessions.background import (  # noqa: F401,E402
    _BACKGROUND_ENV_KEY_RE,
    _BACKGROUND_PROCESS_ID_RE,
    _BACKGROUND_STOP_GRACE_S,
    _BACKGROUND_STOP_POLL_S,
    _BACKGROUND_VERIFY_MAX_ENTRIES,
    BACKGROUND_PROCESS_DIR,
    BackgroundProcess,
    BackgroundProcessManager,
)
from .services.sessions.streams import (  # noqa: F401,E402
    StreamManager,
    StreamSession,
)
from .services.sessions.terminals import (  # noqa: F401,E402
    TerminalManager,
    TerminalSession,
)
from .services.sessions.desktop import (  # noqa: F401,E402
    _CLICK_BUTTONS,
    _RUN_ID_RE,
    _SCROLL_BUTTONS,
    COMPUTER_USE_RECORD_DIR,
    DEFAULT_DESKTOP_HEIGHT,
    DEFAULT_DESKTOP_WIDTH,
    DESKTOP_DISPLAY,
    DESKTOP_EXECUTE_MAX_CHARS,
    DESKTOP_EXECUTE_TIMEOUT_S,
    DESKTOP_HOLDER_COMPUTERUSE,
    DESKTOP_HOLDER_VIEWER,
    DESKTOP_HOME,
    DESKTOP_XAUTHORITY_PATH,
    MAX_DESKTOP_HEIGHT,
    MAX_DESKTOP_WIDTH,
    MIN_DESKTOP_HEIGHT,
    MIN_DESKTOP_WIDTH,
    DesktopManager,
)
from .services.workspace_lifecycle import WorkspaceLifecycle  # noqa: F401,E402
from .services.workspace_registry import WorkspaceRegistry  # noqa: F401,E402

# ``FILE_READ_*`` / ``FILE_UPLOAD_*`` / ``FILE_DOWNLOAD_*`` size caps are
# canonically defined in ``src.services.files`` (Step 4) and re-exported
# above, so ``src.service.<NAME>`` keeps working with identical objects
# (not copies). Do not re-define them here.

# ``WORKSPACE_CREDENTIAL_*`` path constants are canonically defined in
# ``src.services.credentials`` (Step 4 forward-move) and re-exported
# above, so ``src.service.<NAME>`` keeps working with identical objects.
# The literals below are kept as comments for grep continuity only.
# WORKSPACE_CREDENTIAL_DIR = "/root/.opencuria-credentials"
# WORKSPACE_CREDENTIAL_MANIFEST = "/root/.opencuria-credentials/manifest"
# WORKSPACE_CREDENTIAL_ENV_FILE = "/root/.opencuria-env.sh"
# WORKSPACE_CREDENTIAL_PROFILE_D = "/etc/profile.d/opencuria-env.sh"
# WORKSPACE_CREDENTIAL_BASHRC = "/root/.bashrc"
# WORKSPACE_CREDENTIAL_BASHRC_LINE = (
#     "test -f /root/.opencuria-env.sh && . /root/.opencuria-env.sh"
# )
# WORKSPACE_CREDENTIAL_ENVIRONMENT = "/etc/environment"
# WORKSPACE_CREDENTIAL_ENVIRONMENT_START = "# OPENCURIA_CREDENTIALS_START"
# WORKSPACE_CREDENTIAL_ENVIRONMENT_END = "# OPENCURIA_CREDENTIALS_END"

# ``DESKTOP_*`` / ``DEFAULT_DESKTOP_*`` / ``MIN_DESKTOP_*`` /
# ``MAX_DESKTOP_*`` / ``COMPUTER_USE_RECORD_DIR`` constants plus
# ``_RUN_ID_RE`` / ``DESKTOP_HOLDER_*`` / ``_SCROLL_BUTTONS`` /
# ``_CLICK_BUTTONS`` are canonically defined in
# ``src.services.sessions.desktop`` (Step 6a) and re-exported above, so
# ``src.service.<NAME>`` keeps working with identical objects (not
# copies). Do not re-define them here.
# The literals below are kept as comments for grep continuity only.
# DESKTOP_DISPLAY = ":1"
# DESKTOP_HOME = "/root"
# DESKTOP_XAUTHORITY_PATH = "/root/.Xauthority"
# DEFAULT_DESKTOP_WIDTH = 1920
# DEFAULT_DESKTOP_HEIGHT = 1080
# MIN_DESKTOP_WIDTH = 800
# MAX_DESKTOP_WIDTH = 3840
# MIN_DESKTOP_HEIGHT = 600
# MAX_DESKTOP_HEIGHT = 2160
# COMPUTER_USE_RECORD_DIR = "/workspace/.opencuria/computeruse"
# DESKTOP_EXECUTE_MAX_CHARS = 200_000
# DESKTOP_EXECUTE_TIMEOUT_S = 120.0
# _RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
# DESKTOP_HOLDER_VIEWER = "viewer"
# DESKTOP_HOLDER_COMPUTERUSE = "computeruse"
# _SCROLL_BUTTONS = {"up": 4, "down": 5, "left": 6, "right": 7}
# _CLICK_BUTTONS = {"left": 1, "middle": 2, "right": 3, 1: 1, 2: 2, 3: 3}
import sys as _sys

for _alias_name in (
    "DESKTOP_DISPLAY",
    "DESKTOP_HOME",
    "DESKTOP_XAUTHORITY_PATH",
    "DEFAULT_DESKTOP_WIDTH",
    "DEFAULT_DESKTOP_HEIGHT",
    "MIN_DESKTOP_WIDTH",
    "MAX_DESKTOP_WIDTH",
    "MIN_DESKTOP_HEIGHT",
    "MAX_DESKTOP_HEIGHT",
    "COMPUTER_USE_RECORD_DIR",
    "DESKTOP_EXECUTE_MAX_CHARS",
    "DESKTOP_EXECUTE_TIMEOUT_S",
    "_RUN_ID_RE",
    "DESKTOP_HOLDER_VIEWER",
    "DESKTOP_HOLDER_COMPUTERUSE",
    "_SCROLL_BUTTONS",
    "_CLICK_BUTTONS",
):
    _sys.modules[__name__].__dict__[_alias_name] = getattr(
        _sys.modules["src.services.sessions.desktop"], _alias_name
    )
del _sys, _alias_name
# Ubuntu 22.04 ships xdotool 3.20160805, which has almost no key aliases
# and treats a bare "--" as an invalid option. Canonical definitions live
# in ``src.services.sessions.xdotool`` (Step 1); re-exported above.
# Background constants/dataclasses live canonically in
# ``src.services.sessions.background`` (Step 3); re-exported above.
# Stream dataclass/manager live canonically in
# ``src.services.sessions.streams`` (Step 3); re-exported above.


def _collapse_xdotool_token(token: str) -> str:
    """Return a case- and separator-insensitive lookup key."""
    from .services.sessions.xdotool import _collapse_xdotool_token as _impl

    return _impl(token)


def _normalize_xdotool_token(token: str, *, modifier: bool = False) -> str:
    """Map one key or modifier name to an xdotool 3.20160805 token."""
    from .services.sessions.xdotool import _normalize_xdotool_token as _impl

    return _impl(token, modifier=modifier)


def _normalize_xdotool_key_combo(key: str, modifiers: list[Any]) -> str:
    """Build an xdotool key combo from a key name and optional modifiers."""
    from .services.sessions.xdotool import _normalize_xdotool_key_combo as _impl

    return _impl(key, modifiers)


def _xdotool_type_command(text: str) -> str:
    """Build an xdotool type command compatible with Ubuntu 22.04."""
    from .services.sessions.xdotool import _xdotool_type_command as _impl

    return _impl(text)


def _xdotool_key_failed(exit_code: int, output: str) -> bool:
    """Return True when xdotool did not actually deliver the key."""
    from .services.sessions.xdotool import _xdotool_key_failed as _impl

    return _impl(exit_code, output)


# ``TerminalSession`` is canonically defined in
# ``src.services.sessions.terminals`` (Step 2) and re-exported at module
# top, so ``src.service.TerminalSession`` keeps working for existing
# importers with the identical object (not a copy/subclass).


# ``BackgroundProcess`` + ``BACKGROUND_PROCESS_DIR`` / ``_BACKGROUND_*``
# constants are canonically defined in
# ``src.services.sessions.background`` (Step 3) and re-exported at module
# top, so ``src.service.<NAME>`` keeps working with identical objects.


# ``StreamSession`` is canonically defined in
# ``src.services.sessions.streams`` (Step 3) and re-exported at module
# top (constants/validators since Step 1), so ``src.service.<NAME>``
# keeps working with identical objects.


def _validate_stream_host(host: str) -> str:
    """Validate a TCP relay host (DNS name, IPv4/IPv6, or workspace-localhost)."""
    from .services.sessions.streams import _validate_stream_host as _impl

    return _impl(host)


def _validate_stream_port(port: object) -> int:
    """Validate a TCP relay port (1..65535)."""
    from .services.sessions.streams import _validate_stream_port as _impl

    return _impl(port)


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
        # Step 8: cache ownership lives in ``WorkspaceRegistry``
        # (canonical ``src.services.workspace_registry``); ``_cache``
        # stays as a property alias below so tests poking
        # ``service._cache[...]`` keep working. All managers are wired
        # with bound lookups onto the registry (no service import in
        # the modules). The registry is constructed FIRST so every
        # manager resolves through it; late-bound closures over the
        # service facades keep instance-attribute overrides and
        # monkeypatching working exactly as before.
        #
        # ``_runtimes`` stays the SAME dict object the registry and all
        # managers were constructed with (``WorkspaceService`` never
        # rebinds it), so in-place test reassignment
        # (``service._runtimes = {...}``) would detach the managers.
        # The property setter below therefore updates the shared dict
        # in place AND re-points the registry/managers at the new
        # mapping, preserving the pre-Step-8 rebinding behaviour.
        self._registry = WorkspaceRegistry(runtimes, settings)
        # Step 2: leaf managers own terminal + image state. The managers
        # are wired with bound lookups (no service import in the modules)
        # and write through to the live cache via closures (not captured
        # dict objects) so ``sync_from_runtime`` reassignments stay
        # correct.
        self._terminals_manager = TerminalManager(
            runtimes,
            self._get_cached,
            self._get_runtime,
            lambda workspace_id: self._cache.pop(workspace_id, None),
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self._images = ImageManager(
            runtimes,
            self._get_cached,
            self._get_runtime,
            self._get_runtime_by_type,
            None,
            lambda info: self._cache.__setitem__(info.workspace_id, info),
            # Late-bound closure over the ``inject_workspace_credentials``
            # facade (like ``_lifecycle.inject_hook`` below) so
            # instance-attribute overrides and ``monkeypatch`` on the
            # service keep working exactly like the pre-extraction
            # ``self.inject_workspace_credentials`` call.
            lambda runtime, instance_id, env_vars, files, ssh_keys, log: (
                self.inject_workspace_credentials(
                    runtime, instance_id, env_vars, files, ssh_keys, log
                )
            ),
        )
        # Step 3: stateful leaf clusters own stream + background state.
        # The managers are wired with bound lookups (no service import in
        # the modules). ``sanitize_exec_workdir`` is the canonical
        # ``src.services.exec_kernel`` function (bound facade keeps parity
        # via the same canonical helper).
        self._streams_manager = StreamManager(
            runtimes,
            self._get_cached,
            self._get_runtime,
            self._sanitize_exec_workdir,
        )
        self._background = BackgroundProcessManager(
            runtimes,
            self._get_cached,
            self._get_runtime,
            self._sanitize_exec_workdir,
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        # Step 4: content-plane managers (files + harness exec) sit on top
        # of the exec kernel. Wired with bound lookups (no service import
        # in the modules).
        self._files_manager = FileManager(
            runtimes,
            self._get_cached,
            self._get_runtime,
        )
        self._harness = HarnessExecService(
            runtimes,
            self._get_cached,
            self._get_runtime,
            self._sanitize_exec_workdir,
            WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        # Step 5: exec kernel + credential manager. The kernel owns the
        # stateless exec entry points (wrap + normalise + runtime
        # dispatch); the credential manager owns persistent
        # inject/remove. Both are wired with the canonical env-file
        # constant (no service import in the modules). ``inject`` needs
        # no exec hook (verbatim facade logic uses the runtime
        # directly); the manager keeps an optional ``exec_command``
        # parameter for forward compatibility.
        self._exec_kernel = ExecKernel(
            credential_env_file=WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        self._credentials_manager = CredentialManager(
            credential_env_file=WORKSPACE_CREDENTIAL_ENV_FILE,
        )
        # Git operations: serialised per workspace/repo so concurrent
        # snapshot + mutation requests cannot interleave mid-sequence.
        # Step 7: full git orchestration lives in the ``GitService``
        # manager (canonical ``src.services.git_service``), wired with
        # bound lookups (no service import in the module). The manager
        # owns the ``_git_lock_map`` KeyedLockMap (Step 5 mechanics,
        # canonical ``src.services.exec_kernel``); ``_git_locks`` /
        # ``_git_locks_guard`` stay as property aliases onto the
        # manager's map so tests poking ``service._git_locks[...]``
        # keep working.
        self._git = GitService(
            runtimes,
            self._get_cached,
            self._get_runtime,
        )
        # Step 6a: desktop lifecycle manager owns the shared Xvnc
        # process, leases and recordings. Wired with bound lookups (no
        # service import in the module); the harness hook is a late-bound
        # closure over the ``exec_harness_command`` facade (delegates to
        # the harness manager) used by ``desktop_action("execute")``, so
        # ``monkeypatch.setattr(service, "exec_harness_command", ...)``
        # and instance-attribute overrides
        # (``service._ensure_desktop_process_locked = ...``) keep working
        # through the facade. ``sync_from_runtime`` reassignments stay
        # correct (closures, not captured dicts).
        self._desktop = DesktopManager(
            runtimes,
            self._get_cached,
            self._get_runtime,
            lambda workspace_id, command, workdir="/workspace", env=None: (
                self.exec_harness_command(
                    workspace_id, command, workdir=workdir, env=env
                )
            ),
        )
        # Limit concurrent file-read SSH channels per workspace to avoid
        # exhausting the SSH server's MaxSessions limit (default: 10).
        # Each read_file call opens at most 1 SSH channel, so a limit of 4
        # keeps peak channel usage well below 10.
        # Step 4: state is owned by ``FileManager``; this stays as a
        # property alias below so tests poking
        # ``service._file_read_semaphores[...]`` keep working.
        # Step 8: lifecycle orchestration lives in ``WorkspaceLifecycle``
        # (canonical ``src.services.workspace_lifecycle``). The composer
        # wires it here, after the leaf managers exist, with hook
        # closures that read the *current* attribute values at call time
        # (late-bound ``getattr``) so instance-attribute overrides and
        # ``monkeypatch`` on the service keep working exactly like the
        # pre-Step-8 ``self.<facade>`` calls.
        self._lifecycle = WorkspaceLifecycle(
            self._registry,
            settings,
            runtimes,
            self._credentials_manager,
            self._exec_kernel,
            self._background,
            self._streams_manager,
            self._desktop,
        )
        # Registry cross-cluster hooks: late-bound service facades.
        # ``desktop_sessions`` / ``background_entries`` are passed as
        # live dict *objects* owned by the managers (the managers never
        # rebind them — service property setters mutate in place), so
        # identity holds for the process lifetime.
        self._registry.desktop_sessions = self._desktop._desktop_sessions
        self._registry.background_entries = self._background._background_processes
        self._registry.background_status = (
            lambda runtime, instance_id, entry: self._background_status_locked(
                runtime, instance_id, entry
            )
        )
        self._registry.desktop_live = (
            lambda workspace_id: self._is_desktop_session_live(workspace_id)
        )
        self._registry.desktop_heartbeat_payload = (
            lambda workspace_id, session: self._desktop_heartbeat_payload(
                workspace_id, session
            )
        )
        # Lifecycle cross-cluster hooks: late-bound service facades.
        self._lifecycle.remove_hook = (
            lambda runtime, instance_id, log: self.remove_workspace_credentials(
                runtime, instance_id, log
            )
        )
        self._lifecycle.inject_hook = (
            lambda runtime, instance_id, env_vars, files, ssh_keys, log: (
                self.inject_workspace_credentials(
                    runtime, instance_id, env_vars, files, ssh_keys, log
                )
            )
        )
        self._lifecycle.exec_hook = (
            lambda runtime, instance_id, command: self._exec_command(
                runtime, instance_id, command
            )
        )
        self._lifecycle.close_streams_hook = (
            lambda workspace_id, reason: self.close_workspace_streams(
                workspace_id, reason=reason
            )
        )
        self._lifecycle.kill_all_hook = (
            lambda workspace_id, reason: self._kill_all_background_processes(
                workspace_id, reason=reason
            )
        )
        self._lifecycle.drop_tracking_hook = (
            lambda workspace_id, reason: self._drop_background_tracking(
                workspace_id, reason=reason
            )
        )
        self._lifecycle.release_hook = (
            lambda workspace_id, holder, run_id=None, force=False: (
                self.release_desktop(
                    workspace_id, holder=holder, run_id=run_id, force=force
                )
            )
        )
        self._lifecycle.interrupt_hook = (
            lambda workspace_id: self._interrupt_desktop_recordings(workspace_id)
        )
        self._lifecycle.desktop_lock_hook = (
            lambda workspace_id: self._desktop_lock(workspace_id)
        )
        # Self-healing unreachable timers are owned by the lifecycle;
        # this stays as a property alias below so tests poking
        # ``service._unreachable_since[...]`` keep working.
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
        # Step 6a: mechanics live in the manager-owned
        # ``DesktopManager._desktop_lock_map`` (canonical
        # ``src.services.exec_kernel.KeyedLockMap``);
        # ``_desktop_lock_map`` / ``_desktop_locks`` /
        # ``_desktop_locks_guard`` stay as property aliases onto the
        # manager's map so tests poking
        # ``service._desktop_locks.get(...)`` keep working.

    # -- Step 8 composer accessors ---------------------------------------
    # ``_cache`` stays readable/writable as a dict alias onto the
    # registry-owned store so tests poking ``service._cache[...]``
    # keep working. ``registry`` / ``lifecycle`` are the stable entry
    # points for the extracted clusters.
    #
    # -- Step 2 manager accessors ----------------------------------------
    # ``_terminals`` stays readable/writable as a dict alias onto the
    # manager-owned store so tests poking ``service._terminals[...]``
    # keep working. ``terminal_manager`` / ``images`` are the stable
    # entry points the websocket handlers use.
    #
    # -- Step 3 manager accessors ----------------------------------------
    # ``_streams`` / ``_background_processes`` (+ ``_background_lock`` /
    # ``_background_start_locks`` / ``_background_start_locks_guard`` /
    # ``_streams_guard``) stay readable/writable as aliases onto the
    # manager-owned stores so tests poking ``service._streams[...]`` /
    # ``service._background_processes[...]`` keep working. ``streams`` /
    # ``background`` are the stable entry points the websocket handlers
    # use.
    #
    # -- Step 4 manager accessors ----------------------------------------
    # ``_file_read_semaphores`` stays readable/writable as an alias onto
    # the file-manager-owned dict so tests poking
    # ``service._file_read_semaphores[...]`` keep working. ``files`` /
    # ``harness`` are the stable entry points the websocket handlers use.
    #
    # -- Step 6a manager accessors ---------------------------------------
    # ``_desktop_sessions`` / ``_desktop_recordings`` /
    # ``_desktop_lock_map`` / ``_desktop_locks`` /
    # ``_desktop_locks_guard`` stay readable/writable as aliases onto the
    # desktop-manager-owned stores so tests poking
    # ``service._desktop_sessions[...]`` /
    # ``service._desktop_recordings[...]`` /
    # ``service._desktop_locks.get(...)`` keep working. ``desktop`` is
    # the stable entry point the websocket handlers will use (Step 6b).

    @property
    def _runtimes(self) -> dict[str, RuntimeBackend]:
        """Return the shared runtime-backend mapping.

        Step 8: the SAME dict object handed to the registry, the
        lifecycle and every manager at construction time.
        """
        return self.__dict__["_runtimes"]

    @_runtimes.setter
    def _runtimes(self, value: dict[str, RuntimeBackend]) -> None:
        """Re-point the whole composition at a new runtime mapping.

        Step 8: pre-Step-8 code allowed ``service._runtimes = {...}``
        rebinding (relied upon by tests); the managers and the registry
        hold their own references, so the setter propagates the new
        mapping to all of them to preserve that behaviour.
        """
        self.__dict__["_runtimes"] = value
        _registry = self.__dict__.get("_registry")
        if _registry is not None:
            try:
                _registry._runtimes = value
            except AttributeError:
                pass
        for _manager_attr in (
            "_terminals_manager",
            "_images",
            "_streams_manager",
            "_background",
            "_files_manager",
            "_harness",
            "_git",
            "_desktop",
        ):
            _manager = self.__dict__.get(_manager_attr)
            if _manager is not None:
                try:
                    _manager._runtimes = value
                except AttributeError:
                    pass
        _lifecycle = self.__dict__.get("_lifecycle")
        if _lifecycle is not None:
            try:
                _lifecycle._runtimes = value
            except AttributeError:
                pass

    @property
    def _cache(self) -> dict[uuid.UUID, WorkspaceInfo]:
        """Alias onto the registry-owned workspace cache dict."""
        return self._registry._cache

    @_cache.setter
    def _cache(self, value: dict) -> None:
        self._registry._cache.clear()
        self._registry._cache.update(value)

    @property
    def registry(self) -> WorkspaceRegistry:
        """Return the workspace registry (cache ownership)."""
        return self._registry

    @property
    def lifecycle(self) -> WorkspaceLifecycle:
        """Return the workspace lifecycle orchestrator."""
        return self._lifecycle

    @property
    def _unreachable_since(self) -> dict[uuid.UUID, float]:
        """Alias onto the lifecycle-owned self-healing timer dict."""
        return self._lifecycle._unreachable_since

    @_unreachable_since.setter
    def _unreachable_since(self, value: dict) -> None:
        self._lifecycle._unreachable_since.clear()
        self._lifecycle._unreachable_since.update(value)

    @property
    def _desktop_sessions(self) -> dict[uuid.UUID, DesktopSession]:
        """Alias onto the desktop manager's session dict."""
        return self._desktop._desktop_sessions

    @_desktop_sessions.setter
    def _desktop_sessions(self, value: dict) -> None:
        self._desktop._desktop_sessions.clear()
        self._desktop._desktop_sessions.update(value)

    @property
    def _desktop_recordings(
        self,
    ) -> dict[tuple[uuid.UUID, str], tuple[int, str]]:
        """Alias onto the desktop manager's recording dict."""
        return self._desktop._desktop_recordings

    @_desktop_recordings.setter
    def _desktop_recordings(self, value: dict) -> None:
        self._desktop._desktop_recordings.clear()
        self._desktop._desktop_recordings.update(value)

    @property
    def _desktop_lock_map(self) -> KeyedLockMap:
        """Alias onto the desktop manager's keyed lock map."""
        return self._desktop._desktop_lock_map

    @_desktop_lock_map.setter
    def _desktop_lock_map(self, value: KeyedLockMap) -> None:
        self._desktop._desktop_lock_map = value

    @property
    def _desktop_locks(self) -> dict[uuid.UUID, asyncio.Lock]:
        """Alias onto the desktop manager's keyed lock map dict."""
        return self._desktop._desktop_locks

    @_desktop_locks.setter
    def _desktop_locks(self, value: dict) -> None:
        self._desktop._desktop_locks.clear()
        self._desktop._desktop_locks.update(value)

    @property
    def _desktop_locks_guard(self) -> asyncio.Lock:
        """Alias onto the desktop manager's lock-map guard."""
        return self._desktop._desktop_locks_guard

    @_desktop_locks_guard.setter
    def _desktop_locks_guard(self, value: asyncio.Lock) -> None:
        self._desktop._desktop_locks_guard = value

    @property
    def _file_read_semaphores(self) -> dict[uuid.UUID, asyncio.Semaphore]:
        """Alias onto the file manager's semaphore dict."""
        return self._files_manager._file_read_semaphores

    @_file_read_semaphores.setter
    def _file_read_semaphores(self, value: dict) -> None:
        self._files_manager._file_read_semaphores.clear()
        self._files_manager._file_read_semaphores.update(value)

    @property
    def _streams(self) -> dict[str, StreamSession]:
        """Alias onto the stream manager's session dict."""
        return self._streams_manager._streams

    @_streams.setter
    def _streams(self, value: dict) -> None:
        self._streams_manager._streams.clear()
        self._streams_manager._streams.update(value)

    @property
    def _streams_guard(self) -> asyncio.Lock:
        """Alias onto the stream manager's guard lock."""
        return self._streams_manager._streams_guard

    @_streams_guard.setter
    def _streams_guard(self, value: asyncio.Lock) -> None:
        self._streams_manager._streams_guard = value

    @property
    def _background_processes(
        self,
    ) -> dict[uuid.UUID, dict[str, BackgroundProcess]]:
        """Alias onto the background manager's tracking dict."""
        return self._background._background_processes

    @_background_processes.setter
    def _background_processes(self, value: dict) -> None:
        self._background._background_processes.clear()
        self._background._background_processes.update(value)

    @property
    def _background_lock(self) -> asyncio.Lock:
        """Alias onto the background manager's tracking lock."""
        return self._background._background_lock

    @_background_lock.setter
    def _background_lock(self, value: asyncio.Lock) -> None:
        self._background._background_lock = value

    @property
    def _background_start_locks(
        self,
    ) -> dict[tuple[uuid.UUID, str], asyncio.Lock]:
        """Alias onto the background manager's keyed start-lock map."""
        return self._background._background_start_locks

    @_background_start_locks.setter
    def _background_start_locks(self, value: dict) -> None:
        self._background._background_start_locks.clear()
        self._background._background_start_locks.update(value)

    @property
    def _background_start_locks_guard(self) -> asyncio.Lock:
        """Alias onto the background manager's start-lock guard."""
        return self._background._background_start_locks_guard

    @_background_start_locks_guard.setter
    def _background_start_locks_guard(self, value: asyncio.Lock) -> None:
        self._background._background_start_locks_guard = value

    @property
    def streams(self) -> StreamManager:
        """Return the generic stream session manager."""
        return self._streams_manager

    @property
    def background(self) -> BackgroundProcessManager:
        """Return the background process manager."""
        return self._background

    @property
    def _terminals(self) -> dict[str, TerminalSession]:
        """Alias onto the terminal manager's session dict."""
        return self._terminals_manager._terminals

    @_terminals.setter
    def _terminals(self, value: dict) -> None:
        self._terminals_manager._terminals.clear()
        self._terminals_manager._terminals.update(value)

    @property
    def terminal_manager(self) -> TerminalManager:
        """Return the terminal session manager."""
        return self._terminals_manager

    @property
    def images(self) -> ImageManager:
        """Return the image build/artifact manager."""
        return self._images

    @property
    def files(self) -> FileManager:
        """Return the file operation manager."""
        return self._files_manager

    @property
    def harness(self) -> HarnessExecService:
        """Return the harness command execution manager."""
        return self._harness

    @property
    def credentials(self) -> CredentialManager:
        """Return the credential inject/remove manager."""
        return self._credentials_manager

    @property
    def exec_kernel(self) -> ExecKernel:
        """Return the exec kernel (wrap + normalise + runtime dispatch)."""
        return self._exec_kernel

    @property
    def desktop(self) -> DesktopManager:
        """Return the desktop session manager."""
        return self._desktop

    @property
    def git(self) -> GitService:
        """Return the git orchestration manager."""
        return self._git

    @property
    def _git_lock_map(self) -> KeyedLockMap:
        """Alias onto the git manager's keyed lock map."""
        return self._git._git_lock_map

    @_git_lock_map.setter
    def _git_lock_map(self, value: KeyedLockMap) -> None:
        self._git._git_lock_map = value

    @property
    def _git_locks(self) -> dict[tuple[uuid.UUID, str], asyncio.Lock]:
        """Alias onto the git manager's keyed lock map dict."""
        return self._git._git_locks

    @_git_locks.setter
    def _git_locks(self, value: dict) -> None:
        self._git._git_locks.clear()
        self._git._git_locks.update(value)

    @property
    def _git_locks_guard(self) -> asyncio.Lock:
        """Alias onto the git manager's lock-map guard."""
        return self._git._git_locks_guard

    @_git_locks_guard.setter
    def _git_locks_guard(self, value: asyncio.Lock) -> None:
        self._git._git_locks_guard = value

    # -- background processes --------------------------------------------------
    # Step 3: thin facade over ``BackgroundProcessManager`` (canonical).
    # State (``_background_processes`` / ``_background_lock`` /
    # ``_background_start_locks`` + guard) is owned by the manager and
    # exposed via property aliases above.

    @staticmethod
    def _sanitize_background_file_path(value: str | None, suffix: str) -> str | None:
        """Validate a backend-assigned background log/exit path.

        Step 3: thin facade over
        ``BackgroundProcessManager._sanitize_background_file_path``.
        """
        return BackgroundProcessManager._sanitize_background_file_path(value, suffix)

    @staticmethod
    def _sanitize_process_id(process_id: str) -> str:
        """Validate a backend-assigned background process id.

        Step 3: thin facade over
        ``BackgroundProcessManager._sanitize_process_id``.
        """
        return BackgroundProcessManager._sanitize_process_id(process_id)

    @staticmethod
    def _build_background_start_shell(
        command: str,
        log_path: str,
        exit_path: str,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        """Build a detached start shell for a background process.

        Step 3: thin facade over
        ``BackgroundProcessManager._build_background_start_shell``.
        """
        return BackgroundProcessManager._build_background_start_shell(
            command, log_path, exit_path, extra_env
        )

    async def _probe_background_pid(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        pid: int,
    ) -> bool:
        """Return True when *pid* is still alive inside the workspace.

        Step 3: thin facade over
        ``BackgroundProcessManager._probe_background_pid``.
        """
        return await self._background._probe_background_pid(runtime, instance_id, pid)

    async def _read_background_exit_code(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        exit_path: str,
    ) -> int | None:
        """Read the exit code recorded in *exit_path*, if any.

        Step 3: thin facade over
        ``BackgroundProcessManager._read_background_exit_code``.
        """
        return await self._background._read_background_exit_code(
            runtime, instance_id, exit_path
        )

    async def _kill_background_pid(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        pid: int,
        signal: str,
    ) -> None:
        """Best-effort signal delivery to a background process group.

        Step 3: thin facade over
        ``BackgroundProcessManager._kill_background_pid``.
        """
        await self._background._kill_background_pid(runtime, instance_id, pid, signal)

    async def _background_start_lock(
        self, workspace_id: uuid.UUID, process_id: str
    ) -> asyncio.Lock:
        """Return the serialising lock for one workspace/process_id pair.

        Step 3: thin facade over
        ``BackgroundProcessManager._background_start_lock``.
        """
        return await self._background._background_start_lock(workspace_id, process_id)

    async def _stop_background_pid_graceful(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        pid: int,
    ) -> None:
        """Best-effort stop of one background PID (TERM -> grace -> KILL).

        Step 3: thin facade over
        ``BackgroundProcessManager._stop_background_pid_graceful``.
        """
        await self._background._stop_background_pid_graceful(runtime, instance_id, pid)

    async def verify_and_reattach_background_processes(
        self,
        workspace_id: uuid.UUID,
        expected: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Verify untracked ("vanished") processes and reattach live ones.

        Step 3: thin facade over
        ``BackgroundProcessManager.verify_and_reattach_background_processes``.
        """
        return await self._background.verify_and_reattach_background_processes(
            workspace_id, expected
        )

    async def _drop_background_tracking(
        self, workspace_id: uuid.UUID, *, reason: str
    ) -> int:
        """Drop in-memory tracking after the VM/container was rebooted.

        Step 3: thin facade over
        ``BackgroundProcessManager._drop_background_tracking``.
        """
        return await self._background._drop_background_tracking(
            workspace_id, reason=reason
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

        Step 3: thin facade over
        ``BackgroundProcessManager.start_background_process``.
        """
        return await self._background.start_background_process(
            workspace_id,
            process_id,
            command,
            workdir=workdir,
            env=env,
            name=name,
            log_path=log_path,
            exit_path=exit_path,
        )

    async def _background_status_locked(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        entry: BackgroundProcess,
    ) -> dict[str, Any]:
        """Compute a live status dict for a tracked background process.

        Step 3: thin facade over
        ``BackgroundProcessManager._background_status_locked``.
        """
        return await self._background._background_status_locked(
            runtime, instance_id, entry
        )

    def _get_background_entry(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
    ) -> BackgroundProcess:
        """Return the tracked entry or raise for unknown process ids.

        Step 3: thin facade over
        ``BackgroundProcessManager._get_background_entry``.
        """
        return self._background._get_background_entry(workspace_id, process_id)

    async def get_background_status(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
    ) -> dict[str, Any]:
        """Return the live status of one tracked background process.

        Step 3: thin facade over
        ``BackgroundProcessManager.get_background_status``.
        """
        return await self._background.get_background_status(workspace_id, process_id)

    async def list_background_processes(
        self,
        workspace_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """Return live statuses for all tracked background processes.

        Step 3: thin facade over
        ``BackgroundProcessManager.list_background_processes``.
        """
        return await self._background.list_background_processes(workspace_id)

    async def stop_background_process(
        self,
        workspace_id: uuid.UUID,
        process_id: str,
    ) -> dict[str, Any]:
        """Stop a tracked background process and drop it from tracking.

        Step 3: thin facade over
        ``BackgroundProcessManager.stop_background_process``.
        """
        return await self._background.stop_background_process(workspace_id, process_id)

    async def _kill_all_background_processes(
        self,
        workspace_id: uuid.UUID,
        *,
        reason: str,
    ) -> None:
        """Best-effort kill of every tracked process for a workspace.

        Step 3: thin facade over
        ``BackgroundProcessManager._kill_all_background_processes``.
        """
        await self._background._kill_all_background_processes(
            workspace_id, reason=reason
        )

    # -- cache management --------------------------------------------------

    async def sync_from_runtime(self) -> None:
        """Rebuild the in-memory cache from live runtime state.

        Step 8: thin facade over the canonical ``WorkspaceRegistry``
        (``src.services.workspace_registry``).
        """
        await self._registry.sync_from_runtime()

    def _get_cached(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Look up a workspace in the cache or raise.

        Step 8: thin facade over the canonical ``WorkspaceRegistry``.
        """
        return self._registry.get_cached(workspace_id)

    def _get_runtime(self, workspace_id: uuid.UUID) -> RuntimeBackend:
        """Return the runtime backend for a workspace.

        Step 8: thin facade over the canonical ``WorkspaceRegistry``.
        """
        return self._registry.get_runtime(workspace_id)

    def _get_runtime_by_type(self, runtime_type: str) -> RuntimeBackend:
        """Return the runtime backend by type name.

        Step 8: thin facade over the canonical ``WorkspaceRegistry``.
        """
        return self._registry.get_runtime_by_type(runtime_type)

    @property
    def supported_runtimes(self) -> list[str]:
        """Return the list of enabled runtime type names.

        Step 8: thin facade over the canonical ``WorkspaceRegistry``.
        """
        return self._registry.supported_runtimes

    def workspace_exists(self, workspace_id: uuid.UUID) -> bool:
        """Return True when *workspace_id* is present in the cache.

        Step 8: public read-through to the canonical
        ``WorkspaceRegistry`` (replaces the private
        ``service._cache`` poke in the websocket layer).
        """
        return self._registry.workspace_exists(workspace_id)

    # -- command execution helpers ---------------------------------------------
    # Step 1: canonical logic lives in ``src.services.exec_kernel``.

    def _normalise_command_args(self, raw_args: list[str] | str) -> list[str]:
        """Return command args suitable for runtime execution.

        Step 5: thin facade over the canonical ``ExecKernel``
        (``src.services.exec_kernel``).
        """
        return self._exec_kernel.normalise_command_args(raw_args)

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

        Step 5: thin facade over the canonical ``ExecKernel``
        (``src.services.exec_kernel``).
        """
        return await self._exec_kernel.exec_command(runtime, instance_id, command)

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

        Step 5: thin facade over the canonical ``ExecKernel``
        (``src.services.exec_kernel``).
        """
        async for line in self._exec_kernel.exec_command_stream(
            runtime, instance_id, command
        ):
            yield line

    # -- persistent workspace credentials -------------------------------------

    @staticmethod
    def _build_tar_entries(
        files: list[tuple[str, bytes, int]],
    ) -> bytes:
        """Build a tar archive containing multiple files."""
        from .services.files import build_tar_entries as _impl

        return _impl(files)

    @staticmethod
    def _credential_path_helpers() -> list[str]:
        """Return shell helper functions used by inject and remove scripts.

        Step 5: thin facade over the canonical ``CredentialManager``
        (``src.services.credentials``).
        """
        return CredentialManager._credential_path_helpers()

    def _wrap_command_with_persistent_env(self, command: dict) -> dict:
        """Source persistent workspace credentials before running a command.

        Step 5: thin facade over the canonical ``ExecKernel``
        (``src.services.exec_kernel``).
        """
        return self._exec_kernel.wrap_command_with_persistent_env(command)

    async def remove_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        log,
    ) -> None:
        """Idempotently remove persisted credential material from a workspace.

        Step 5: thin facade over the canonical ``CredentialManager``
        (``src.services.credentials``).
        """
        await self._credentials_manager.remove_workspace_credentials(
            runtime, instance_id, log
        )

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

        Step 5: thin facade over the canonical ``CredentialManager``
        (``src.services.credentials``). The leading remove goes through
        this facade's own (overridable/mockable)
        ``remove_workspace_credentials`` first — exactly like the
        pre-Step-5 logic — then the manager's ``_inject_after_remove``
        materializes the remainder verbatim.
        """
        await self.remove_workspace_credentials(runtime, instance_id, log)
        return await self._credentials_manager._inject_after_remove(
            runtime, instance_id, env_vars, files, ssh_keys, log
        )

    # -- workspace lifecycle ---------------------------------------------------
    # Step 8: canonical orchestration lives in ``WorkspaceLifecycle``
    # (``src.services.workspace_lifecycle``); the composer exposes it
    # via ``lifecycle``. These stay as thin delegates with identical
    # signatures so websocket handlers, main.py and tests keep working.

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

        Step 8: thin facade over ``WorkspaceLifecycle.create_workspace``.
        """
        return await self._lifecycle.create_workspace(
            repos,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars,
            files=files,
            ssh_keys=ssh_keys,
            workspace_id=workspace_id,
            runtime_type=runtime_type,
            image_tag=image_tag,
            base_image_path=base_image_path,
        )

    async def stop_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Remove credentials then stop a running workspace.

        Step 8: thin facade over ``WorkspaceLifecycle.stop_workspace``.
        """
        return await self._lifecycle.stop_workspace(workspace_id)

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
        """Resume a stopped workspace and re-inject persistent credentials.

        Step 8: thin facade over ``WorkspaceLifecycle.resume_workspace``.
        """
        return await self._lifecycle.resume_workspace(
            workspace_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars,
            files=files,
            ssh_keys=ssh_keys,
        )

    async def inject_credentials(
        self,
        workspace_id: uuid.UUID,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Replace persistent credentials on a running workspace.

        Step 8: thin facade over ``WorkspaceLifecycle.inject_credentials``.
        """
        return await self._lifecycle.inject_credentials(
            workspace_id, env_vars=env_vars, files=files, ssh_keys=ssh_keys
        )

    async def update_workspace_resources(
        self,
        workspace_id: uuid.UUID,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> None:
        """Reconfigure resources for an existing QEMU workspace.

        Step 8: thin facade over
        ``WorkspaceLifecycle.update_workspace_resources``.
        """
        await self._lifecycle.update_workspace_resources(
            workspace_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

    async def remove_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace and clean up resources.

        Step 8: thin facade over ``WorkspaceLifecycle.remove_workspace``.
        """
        await self._lifecycle.remove_workspace(workspace_id)

    async def cleanup_unknown_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Best-effort cleanup for a runtime workspace unknown to the backend.

        Step 8: thin facade over
        ``WorkspaceLifecycle.cleanup_unknown_workspace``.
        """
        return await self._lifecycle.cleanup_unknown_workspace(workspace_id)

    # -- self-healing SSH health check -----------------------------------------
    # Step 8: canonical logic lives in ``WorkspaceLifecycle``. Delegates
    # below preserve the public/private names for registry wiring and
    # health-loop callers.

    async def _check_workspace_reachable(
        self, workspace_id: uuid.UUID, info: WorkspaceInfo
    ) -> bool:
        """Return True if the workspace responds to a lightweight exec probe.

        Step 8: thin facade over
        ``WorkspaceLifecycle._check_workspace_reachable``.
        """
        return await self._lifecycle._check_workspace_reachable(workspace_id, info)

    async def run_health_check_loop(self) -> None:
        """Periodically probe running workspaces and restart unreachable ones.

        Runs indefinitely; cancel the task to stop it.

        Step 8: thin facade over
        ``WorkspaceLifecycle.run_health_check_loop``.
        """
        await self._lifecycle.run_health_check_loop()

    # -- workspace registry reads ----------------------------------------------
    # Step 8: canonical cache ownership lives in ``WorkspaceRegistry``
    # (``src.services.workspace_registry``). Delegates preserve names.

    async def list_workspaces(self) -> list[WorkspaceInfo]:
        """Return all known workspaces, refreshing from the runtime.

        Step 8: thin facade over ``WorkspaceRegistry.list_workspaces``.
        """
        return await self._registry.list_workspaces()

    async def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Return a single workspace by ID, checking live status.

        Step 8: thin facade over ``WorkspaceRegistry.get_workspace``.
        """
        return await self._registry.get_workspace(workspace_id)

    def get_workspace_statuses(self) -> list[dict]:
        """Return lightweight status list for heartbeat reporting.

        Step 8: thin facade over ``WorkspaceRegistry.get_workspace_statuses``.
        """
        return self._registry.get_workspace_statuses()

    async def get_workspace_heartbeat_statuses(self) -> list[dict]:
        """Return workspace heartbeat payload including live desktop sessions.

        Step 8: thin facade over
        ``WorkspaceRegistry.get_workspace_heartbeat_statuses``.
        """
        return await self._registry.get_workspace_heartbeat_statuses()

    async def recover_desktop_sessions_from_runtime(self) -> None:
        """Rebuild in-memory desktop sessions from live runtime state.

        Step 8: thin facade over
        ``WorkspaceRegistry.recover_desktop_sessions_from_runtime``.
        """
        await self._registry.recover_desktop_sessions_from_runtime()

    async def get_vm_metrics(self) -> dict[str, dict[str, Any]]:
        """Collect host-observed metrics for QEMU workspaces.

        Step 8: thin facade over ``WorkspaceRegistry.get_vm_metrics``.
        """
        return await self._registry.get_vm_metrics()

    # -- interactive terminal --------------------------------------------------

    # -- generic stream sessions (process / workspace-local TCP) -----------
    # Step 3: thin facade over ``StreamManager`` (canonical). State
    # (``_streams`` / ``_streams_guard``) is owned by the manager and
    # exposed via property aliases above. NOTE: the canonical
    # ``_desktop_lock`` lives in the desktop section below (Step 6a) —
    # the duplicate that used to sit here was removed in Step 8.

    @staticmethod
    def _sanitize_stream_connection_id(connection_id: str) -> str:
        """Validate a stream connection id (opaque, bounded, fail-closed).

        Step 3: thin facade over
        ``StreamManager._sanitize_stream_connection_id``.
        """
        return StreamManager._sanitize_stream_connection_id(connection_id)

    @staticmethod
    def _sanitize_stream_env(
        env: dict[str, str] | None,
    ) -> dict[str, str]:
        """Validate explicit stream env (least privilege: no credential sourcing).

        Step 3: thin facade over ``StreamManager._sanitize_stream_env``.
        """
        return StreamManager._sanitize_stream_env(env)

    @staticmethod
    def _sanitize_stream_command(command: object) -> list[str]:
        """Validate a stream argv list (never shell-joined).

        Step 3: thin facade over ``StreamManager._sanitize_stream_command``.
        """
        return StreamManager._sanitize_stream_command(command)

    def _stream_count_for_workspace(self, workspace_id: uuid.UUID) -> int:
        """Return the number of live streams bound to *workspace_id*.

        Step 3: thin facade over
        ``StreamManager._stream_count_for_workspace``.
        """
        return self._streams_manager._stream_count_for_workspace(workspace_id)

    async def stream_start_process(
        self,
        workspace_id: uuid.UUID,
        connection_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> StreamSession:
        """Spawn a workspace-bound stdio process stream (least-privilege env).

        Step 3: thin facade over ``StreamManager.stream_start_process``.
        """
        return await self._streams_manager.stream_start_process(
            workspace_id, connection_id, command, workdir=workdir, env=env
        )

    async def stream_start_tcp(
        self,
        workspace_id: uuid.UUID,
        connection_id: str,
        host: str,
        port: int,
        tls: bool = False,
        server_hostname: str | None = None,
    ) -> StreamSession:
        """Open a workspace-local TCP stream via the static in-workspace relay.

        Step 3: thin facade over ``StreamManager.stream_start_tcp``.
        """
        return await self._streams_manager.stream_start_tcp(
            workspace_id,
            connection_id,
            host,
            port,
            tls=tls,
            server_hostname=server_hostname,
        )

    def get_stream(self, connection_id: str) -> StreamSession:
        """Return the live session for *connection_id* or raise.

        Step 3: thin facade over ``StreamManager.get_stream``.
        """
        return self._streams_manager.get_stream(connection_id)

    def _get_stream(self, connection_id: str) -> StreamSession:
        """Back-compat alias for :meth:`get_stream`.

        Step 3: thin facade over ``StreamManager._get_stream``.
        """
        return self._streams_manager._get_stream(connection_id)

    async def stream_read(
        self, connection_id: str, stream: str = "stdout"
    ) -> AsyncIterator[tuple[str, bytes]]:
        """Yield ``(stream, data)`` chunks until the stream ends (EOF).

        Step 3: thin facade over ``StreamManager.stream_read``.
        """
        async for chunk in self._streams_manager.stream_read(connection_id, stream):
            yield chunk

    async def stream_read_once(
        self,
        connection_id: str,
        stream: str = "stdout",
        size: int = STREAM_CHUNK_SIZE,
    ) -> bytes:
        """Read one bounded chunk from one stream (``b""`` on EOF).

        Step 3: thin facade over ``StreamManager.stream_read_once``.
        """
        return await self._streams_manager.stream_read_once(connection_id, stream, size)

    async def stream_write(self, connection_id: str, data: bytes) -> None:
        """Write bounded bytes to the stream stdin (per-connection locked).

        Step 3: thin facade over ``StreamManager.stream_write``.
        """
        await self._streams_manager.stream_write(connection_id, data)

    async def stream_write_eof(self, connection_id: str) -> None:
        """Half-close the stream stdin (graceful EOF).

        Step 3: thin facade over ``StreamManager.stream_write_eof``.
        """
        await self._streams_manager.stream_write_eof(connection_id)

    async def stream_wait(self, connection_id: str) -> int | None:
        """Wait for the stream process tree to exit; return exit code.

        Step 3: thin facade over ``StreamManager.stream_wait``.
        """
        return await self._streams_manager.stream_wait(connection_id)

    async def stream_close(self, connection_id: str) -> dict[str, object]:
        """Close one stream and kill its process tree (idempotent-ish).

        Step 3: thin facade over ``StreamManager.stream_close``.
        """
        return await self._streams_manager.stream_close(connection_id)

    async def close_workspace_streams(
        self, workspace_id: uuid.UUID, *, reason: str = ""
    ) -> int:
        """Close every stream bound to *workspace_id*; return closed count.

        Step 3: thin facade over ``StreamManager.close_workspace_streams``.
        """
        return await self._streams_manager.close_workspace_streams(
            workspace_id, reason=reason
        )

    async def close_all_streams(self, *, reason: str = "") -> int:
        """Close every tracked stream (shutdown/disconnect path).

        Step 3: thin facade over ``StreamManager.close_all_streams``.
        """
        return await self._streams_manager.close_all_streams(reason=reason)

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        Persistent workspace credentials are sourced via a login shell.

        Step 2: thin facade over ``TerminalManager.start_terminal``.
        """
        return await self._terminals_manager.start_terminal(
            workspace_id, cols=cols, rows=rows
        )

    async def read_terminal(self, terminal_id: str) -> AsyncIterator[bytes]:
        """Yield raw bytes from the PTY as they arrive.

        Stops when the PTY is closed or returns empty data (EOF).

        Step 2: thin facade over ``TerminalManager.read_terminal``.
        """
        async for chunk in self._terminals_manager.read_terminal(terminal_id):
            yield chunk

    async def write_terminal(self, terminal_id: str, data: bytes) -> None:
        """Write raw bytes (user input) to the PTY stdin.

        Step 2: thin facade over ``TerminalManager.write_terminal``.
        """
        await self._terminals_manager.write_terminal(terminal_id, data)

    async def resize_terminal(self, terminal_id: str, cols: int, rows: int) -> None:
        """Resize the PTY window.

        Step 2: thin facade over ``TerminalManager.resize_terminal``.
        """
        await self._terminals_manager.resize_terminal(terminal_id, cols, rows)

    async def close_terminal(self, terminal_id: str) -> None:
        """Close a PTY session and release resources.

        Step 2: thin facade over ``TerminalManager.close_terminal``.
        """
        await self._terminals_manager.close_terminal(terminal_id)

    # -- desktop session (KasmVNC) -----------------------------------------
    # Step 6a: thin facade over ``DesktopManager`` (canonical). State
    # (``_desktop_sessions`` / ``_desktop_recordings`` /
    # ``_desktop_lock_map`` + guard) is owned by the manager and
    # exposed via property aliases above.

    async def _desktop_lock(self, workspace_id: uuid.UUID) -> asyncio.Lock:
        """Return the serialising lock for one workspace desktop lifecycle.

        Step 6a: thin facade over ``DesktopManager._desktop_lock``.
        """
        return await self._desktop._desktop_lock(workspace_id)

    async def _is_desktop_session_live(self, workspace_id: uuid.UUID) -> bool:
        """Return whether the cached desktop session still accepts connections.

        Step 6a: thin facade over ``DesktopManager._is_desktop_session_live``.
        """
        return await self._desktop._is_desktop_session_live(workspace_id)

    def _desktop_heartbeat_payload(self, workspace_id: uuid.UUID, session: DesktopSession) -> dict[str, Any]:
        """Return heartbeat fields for a live desktop session.

        Step 6a: thin facade over ``DesktopManager._desktop_heartbeat_payload``.
        """
        return self._desktop._desktop_heartbeat_payload(workspace_id, session)

    @staticmethod
    def _parse_desktop_holder(holder: str) -> str:
        """Validate a desktop lease holder kind.

        Step 6a: thin facade over ``DesktopManager._parse_desktop_holder``.
        """
        return DesktopManager._parse_desktop_holder(holder)

    def _empty_desktop_release_result(self) -> DesktopReleaseResult:
        """Return a release result when no desktop process is tracked.

        Step 6a: thin facade over ``DesktopManager._empty_desktop_release_result``.
        """
        return self._desktop._empty_desktop_release_result()

    def _desktop_release_result(self, session: DesktopSession | None, *, stopped: bool) -> DesktopReleaseResult:
        """Build a release result from the current session cache.

        Step 6a: thin facade over ``DesktopManager._desktop_release_result``.
        """
        return self._desktop._desktop_release_result(session, stopped=stopped)

    @staticmethod
    def _resolve_desktop_geometry(width: int | None = None, height: int | None = None) -> tuple[int, int]:
        """Return a sanitized even framebuffer size for Xvnc.

        Step 6a: thin facade over ``DesktopManager._resolve_desktop_geometry``.
        """
        return DesktopManager._resolve_desktop_geometry(width, height)

    @staticmethod
    def _desktop_start_command(width: int, height: int) -> str:
        """Return the shell used to start Xvnc at a fixed geometry.

        Step 6a: thin facade over ``DesktopManager._desktop_start_command``.
        """
        return DesktopManager._desktop_start_command(width, height)

    def get_desktop_state_payload(self, workspace_id: uuid.UUID) -> dict[str, Any] | None:
        """Return cache/network fields for desktop lifecycle announcements.

        Step 6a: thin facade over ``DesktopManager.get_desktop_state_payload``.
        """
        return self._desktop.get_desktop_state_payload(workspace_id)

    async def ensure_desktop_process(self, workspace_id: uuid.UUID, *, width: int | None = None, height: int | None = None) -> DesktopSession:
        """Start the shared KasmVNC process without acquiring a lease.

        Step 6a: thin facade over ``DesktopManager.ensure_desktop_process``.
        """
        return await self._desktop.ensure_desktop_process(workspace_id, width=width, height=height)

    async def _ensure_desktop_process_locked(self, workspace_id: uuid.UUID, *, width: int | None = None, height: int | None = None) -> DesktopSession:
        """Ensure the desktop process while holding the workspace lock.

        Step 6a: thin facade over ``DesktopManager._ensure_desktop_process_locked``.
        """
        return await self._desktop._ensure_desktop_process_locked(workspace_id, width=width, height=height)

    async def acquire_desktop(self, workspace_id: uuid.UUID, *, holder: str, run_id: str | None = None, width: int | None = None, height: int | None = None) -> DesktopSession:
        """Ensure the desktop process and acquire a viewer or computer-use lease.

        Step 6a: thin facade over ``DesktopManager.acquire_desktop``.
        """
        return await self._desktop.acquire_desktop(workspace_id, holder=holder, run_id=run_id, width=width, height=height)

    async def release_desktop(self, workspace_id: uuid.UUID, *, holder: str, run_id: str | None = None, force: bool = False) -> DesktopReleaseResult:
        """Drop a desktop lease and stop Xvnc when no holders remain.

        Step 6a: thin facade over ``DesktopManager.release_desktop``.
        """
        return await self._desktop.release_desktop(workspace_id, holder=holder, run_id=run_id, force=force)

    async def start_desktop(self, workspace_id: uuid.UUID, *, width: int | None = None, height: int | None = None) -> DesktopSession:
        """Acquire the viewer lease and ensure the desktop process is running.

        Step 6a: thin facade over ``DesktopManager.start_desktop``.
        """
        return await self._desktop.start_desktop(workspace_id, width=width, height=height)

    async def stop_desktop(self, workspace_id: uuid.UUID) -> DesktopReleaseResult:
        """Release the viewer lease. Stops Xvnc only when no computer-use hold remains.

        Step 6a: thin facade over ``DesktopManager.stop_desktop``.
        """
        return await self._desktop.stop_desktop(workspace_id)

    async def _interrupt_desktop_recordings(self, workspace_id: uuid.UUID) -> None:
        """Send SIGINT/SIGTERM to ffmpeg recordings for *workspace_id*.

        Step 6a: thin facade over ``DesktopManager._interrupt_desktop_recordings``.
        """
        return await self._desktop._interrupt_desktop_recordings(workspace_id)

    async def _stop_desktop_process(self, workspace_id: uuid.UUID, *, interrupt_recordings: bool, expected_session: DesktopSession | None = None) -> None:
        """Kill Xvnc and drop the cached desktop session.

        Step 6a: thin facade over ``DesktopManager._stop_desktop_process``.
        """
        return await self._desktop._stop_desktop_process(workspace_id, interrupt_recordings=interrupt_recordings, expected_session=expected_session)

    @staticmethod
    def _desktop_env() -> dict[str, str]:
        """Return environment variables for desktop X11 commands.

        Step 6a: thin facade over ``DesktopManager._desktop_env``.
        """
        return DesktopManager._desktop_env()

    @staticmethod
    def _sanitize_run_id(run_id: str) -> str:
        """Validate a computer-use recording run identifier.

        Step 6a: thin facade over ``DesktopManager._sanitize_run_id``.
        """
        return DesktopManager._sanitize_run_id(run_id)

    async def _ensure_desktop_xauthority(self, workspace_id: uuid.UUID) -> None:
        """Ensure the X11 client auth file exists inside the workspace.

        Step 6a: thin facade over ``DesktopManager._ensure_desktop_xauthority``.
        """
        return await self._desktop._ensure_desktop_xauthority(workspace_id)

    async def _require_desktop_live(self, workspace_id: uuid.UUID) -> None:
        """Raise when the workspace desktop session is not accepting input.

        Step 6a: thin facade over ``DesktopManager._require_desktop_live``.
        """
        return await self._desktop._require_desktop_live(workspace_id)

    async def _exec_desktop_shell(self, workspace_id: uuid.UUID, command: str) -> tuple[int, str]:
        """Execute a shell command inside the workspace desktop environment.

        Step 6a: thin facade over ``DesktopManager._exec_desktop_shell``.
        """
        return await self._desktop._exec_desktop_shell(workspace_id, command)

    async def _get_desktop_geometry(self, workspace_id: uuid.UUID, width: int | None = None, height: int | None = None) -> tuple[int, int]:
        """Return desktop width and height, optionally overriding query results.

        Step 6a: thin facade over ``DesktopManager._get_desktop_geometry``.
        """
        return await self._desktop._get_desktop_geometry(workspace_id, width, height)

    async def desktop_action(self, workspace_id: uuid.UUID, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute a desktop I/O action inside the workspace display.

        Step 6a: thin facade over ``DesktopManager.desktop_action``.
        """
        return await self._desktop.desktop_action(workspace_id, action, args)

    async def write_desktop_clipboard(self, workspace_id: uuid.UUID, text: str) -> None:
        """Write plain text into the desktop clipboard inside the workspace VM/container.

        Step 6a: thin facade over ``DesktopManager.write_desktop_clipboard``.
        """
        return await self._desktop.write_desktop_clipboard(workspace_id, text)

    async def read_desktop_clipboard(self, workspace_id: uuid.UUID) -> str:
        """Read plain text from the desktop clipboard inside the workspace VM/container.

        Step 6a: thin facade over ``DesktopManager.read_desktop_clipboard``.
        """
        return await self._desktop.read_desktop_clipboard(workspace_id)

    def get_desktop_session(self, workspace_id: uuid.UUID) -> DesktopSession | None:
        """Return the active desktop session if any.

        Step 6a: thin facade over ``DesktopManager.get_desktop_session``.
        """
        return self._desktop.get_desktop_session(workspace_id)

    def get_desktop_container_ip(self, workspace_id: uuid.UUID) -> str:
        """Get the upstream IP address for the workspace desktop proxy.

        Step 6a: thin facade over ``DesktopManager.get_desktop_container_ip``.
        """
        return self._desktop.get_desktop_container_ip(workspace_id)

    def get_desktop_network_name(self, workspace_id: uuid.UUID) -> str:
        """Get the backend-attachable network for a workspace desktop proxy.

        Step 6a: thin facade over ``DesktopManager.get_desktop_network_name``.
        """
        return self._desktop.get_desktop_network_name(workspace_id)

    # -- file operations -------------------------------------------------------
    # Step 4: thin facade over ``FileManager`` (canonical). State
    # (``_file_read_semaphores``) is owned by the manager and exposed via
    # the property alias above. Pure helpers (sanitizers, find, tar) live
    # in ``src.services.exec_kernel`` / ``src.services.files``.

    @staticmethod
    def _sanitize_path(path: str) -> str:
        """Ensure *path* is under ``/workspace`` and prevent traversal."""
        from .services.exec_kernel import sanitize_path as _impl

        return _impl(path)

    @staticmethod
    def _sanitize_exec_workdir(path: str) -> str:
        """Normalize an exec working directory (not sandboxed to /workspace)."""
        from .services.exec_kernel import sanitize_exec_workdir as _impl

        return _impl(path)

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Validate and return a safe filename for workspace uploads."""
        from .services.exec_kernel import sanitize_filename as _impl

        return _impl(filename)

    async def _realpath_under_workspace(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        path: str,
    ) -> str:
        """Resolve symlinks for *path* and ensure it stays in /workspace.

        Step 4: thin facade over ``FileManager._realpath_under_workspace``.
        """
        return await self._files_manager._realpath_under_workspace(
            runtime, instance_id, path
        )

    @staticmethod
    def _build_single_file_tar(filename: str, content: bytes) -> bytes:
        """Build a tar archive containing exactly one file."""
        from .services.files import build_single_file_tar as _impl

        return _impl(filename, content)

    @staticmethod
    def _convert_archive_to_tar(content: bytes) -> bytes:
        """Convert an uploaded archive payload to a plain tar stream."""
        from .services.files import convert_archive_to_tar as _impl

        return _impl(content)

    async def list_files(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> list[dict]:
        """List files and directories at *path* inside the workspace.

        Returns a list of dicts with ``name``, ``path``, ``type``, ``size``.

        Step 4: thin facade over ``FileManager.list_files``.
        """
        return await self._files_manager.list_files(workspace_id, path)

    @staticmethod
    def sanitize_find_query(query: str) -> str:
        """Return a safe ``find -ipath`` query fragment.

        Only characters that the chat ``@`` mention regex allows are accepted.
        ``..`` is rejected even though ``.`` is otherwise valid.
        """
        from .services.files import sanitize_find_query as _impl

        return _impl(query)

    @classmethod
    def build_find_files_command(cls, query: str, limit: int) -> list[str]:
        """Build ``bash -lc`` argv that finds workspace files up to *limit*.

        Prunes common junk directories. An empty *query* lists shallower paths
        first; a non-empty query uses case-insensitive ``-ipath``.
        """
        from .services.files import build_find_files_command as _impl

        return _impl(query, limit)

    async def find_files(
        self,
        workspace_id: uuid.UUID,
        query: str = "",
        limit: int = FIND_FILES_DEFAULT_LIMIT,
    ) -> dict:
        """Search workspace files for mention autocomplete.

        Returns ``{"paths": [{"path", "name"}], "truncated": bool}``. Results
        are capped at ``FIND_FILES_DEFAULT_LIMIT``.

        Step 4: thin facade over ``FileManager.find_files``.
        """
        return await self._files_manager.find_files(
            workspace_id, query=query, limit=limit
        )

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

        Step 4: thin facade over ``FileManager.read_file``.
        """
        return await self._files_manager.read_file(
            workspace_id, path, max_size=max_size
        )

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

        Step 4: thin facade over ``FileManager.upload_file``. The upload
        cap reads the live ``src.service.FILE_UPLOAD_MAX_SIZE`` value at
        call time so ``monkeypatch`` on the facade stays effective.
        """
        import src.service as _service_module

        return await self._files_manager.upload_file(
            workspace_id,
            path,
            filename,
            content_b64,
            is_directory=is_directory,
            upload_max_size=_service_module.FILE_UPLOAD_MAX_SIZE,
        )

    async def download_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Download a file or directory from the workspace container.

        Returns a dict with ``content`` (base64), ``filename``, ``is_archive``
        and ``size`` (raw byte count). For directories, the content is a
        tar.gz archive. Payloads larger than ``FILE_DOWNLOAD_MAX_SIZE``
        raise ``ValueError`` so callers can return a small structured error
        instead of buffering unbounded memory.

        Step 4: thin facade over ``FileManager.download_file``.
        """
        return await self._files_manager.download_file(workspace_id, path)

    async def stat_path(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Stat a path inside the workspace container.

        Returns a dict with ``path``, ``is_dir``, ``size``, ``mime_type``.

        Step 4: thin facade over ``FileManager.stat_path``.
        """
        return await self._files_manager.stat_path(workspace_id, path)

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

        Step 4: thin facade over ``FileManager.write_file_content``. The
        write cap reads the live ``src.service.FILE_UPLOAD_MAX_SIZE``
        value at call time so ``monkeypatch`` on the facade stays
        effective.
        """
        import src.service as _service_module

        return await self._files_manager.write_file_content(
            workspace_id,
            path,
            content_b64,
            mode=mode,
            upload_max_size=_service_module.FILE_UPLOAD_MAX_SIZE,
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

        Step 4: thin facade over ``HarnessExecService.exec_harness_command``.
        """
        return await self._harness.exec_harness_command(
            workspace_id, command, workdir=workdir, env=env
        )

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

        Step 4: thin facade over
        ``HarnessExecService.exec_harness_command_stream``.
        """
        async for chunk in self._harness.exec_harness_command_stream(
            workspace_id, command, workdir=workdir, env=env
        ):
            yield chunk

    @staticmethod
    def _parse_harness_exec_output(output: str) -> tuple[str, str]:
        """Split tagged exec wrapper output into (stdout, stderr).

        Step 4: thin facade over
        ``HarnessExecService._parse_harness_exec_output``.
        """
        return HarnessExecService._parse_harness_exec_output(output)

    # ── Git operations (dumb-executor git service) ──────────────────────
    # Step 7: thin facade over ``GitService`` (canonical
    # ``src.services.git_service``). State (``_git_lock_map``) is owned
    # by the manager and exposed via the ``git`` / ``_git_lock_map`` /
    # ``_git_locks`` / ``_git_locks_guard`` aliases above; every method
    # here delegates with the same name/signature/messages.

    async def _git_lock(
        self, workspace_id: uuid.UUID, repo_root: str
    ) -> asyncio.Lock:
        """Return the serialising lock for one workspace/repo pair.

        Step 7: thin facade over ``GitService._git_lock``.
        """
        return await self._git._git_lock(workspace_id, repo_root)

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

        Step 7: thin facade over ``GitService._git_exec``.
        """
        return await self._git._git_exec(runtime, instance_id, argv, workdir=workdir, env=env, timeout=timeout, check_git=check_git)

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

        Step 7: thin facade over ``GitService._git_realpath_contained``.
        """
        return await self._git._git_realpath_contained(runtime, instance_id, path, env, context=context, display=display)

    async def _git_verify_metadata_paths(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> str:
        """Verify gitdir + common-dir live under ``/workspace`` (fail-closed).

        Step 7: thin facade over ``GitService._git_verify_metadata_paths``.
        """
        return await self._git._git_verify_metadata_paths(runtime, instance_id, repo_root, env)

    async def _git_verify_repo_root(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        resolved: str,
        env: dict[str, str],
    ) -> str:
        """Verify *resolved* is a repo root with in-workspace metadata.

        Step 7: thin facade over ``GitService._git_verify_repo_root``.
        """
        return await self._git._git_verify_repo_root(runtime, instance_id, resolved, env)

    async def _git_resolve_repo_root(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_arg: str,
        env: dict[str, str],
    ) -> str:
        """Resolve *repo_arg* to a verified repository root (fail-closed).

        Step 7: thin facade over ``GitService._git_resolve_repo_root``.
        """
        return await self._git._git_resolve_repo_root(runtime, instance_id, repo_arg, env)

    async def _git_discover_repos(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env: dict[str, str],
    ) -> list[str]:
        """Discover repository roots under ``/workspace`` (capped).

        Step 7: thin facade over ``GitService._git_discover_repos``.
        """
        return await self._git._git_discover_repos(runtime, instance_id, env)

    async def list_git_repositories(
        self, workspace_id: uuid.UUID
    ) -> list[str]:
        """Return discovered repository roots for *workspace_id* (internal).

        Step 7: thin facade over ``GitService.list_git_repositories``.
        """
        return await self._git.list_git_repositories(workspace_id)

    async def _git_list_entry(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build a lightweight list entry for one repository root.

        Step 7: thin facade over ``GitService._git_list_entry``.
        """
        return await self._git._git_list_entry(runtime, instance_id, repo_root, env)

    async def _git_repo_snapshot_no_log(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Build the snapshot dict for one repository root (no history).

        Step 7: thin facade over ``GitService._git_repo_snapshot_no_log``.
        """
        return await self._git._git_repo_snapshot_no_log(runtime, instance_id, repo_root, env)

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

        Step 7: thin facade over ``GitService._git_repo_history``.
        """
        return await self._git._git_repo_history(runtime, instance_id, repo_root, env, limit=limit, skip=skip, branch=branch)

    async def _git_merge_state(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Detect MERGE_HEAD / rebase / cherry-pick state files.

        Step 7: thin facade over ``GitService._git_merge_state``.
        """
        return await self._git._git_merge_state(runtime, instance_id, repo_root, env)

    async def _git_working_diff(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        changes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build staged/unstaged/untracked diff entries for a repo.

        Step 7: thin facade over ``GitService._git_working_diff``.
        """
        return await self._git._git_working_diff(runtime, instance_id, repo_root, env, changes)

    async def _git_diff_paths(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        argv: list[str],
    ) -> dict[str, str]:
        """Run a patch diff argv and split per-file patches.

        Step 7: thin facade over ``GitService._git_diff_paths``.
        """
        return await self._git._git_diff_paths(runtime, instance_id, repo_root, env, argv)

    async def _git_diff_numstat_raw(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        numstat_argv: list[str],
        raw_argv: list[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Run numstat + raw diff argvs and parse them.

        Step 7: thin facade over ``GitService._git_diff_numstat_raw``.
        """
        return await self._git._git_diff_numstat_raw(runtime, instance_id, repo_root, env, numstat_argv, raw_argv)

    def _git_join_diff_parts(
        self,
        patches: dict[str, str],
        numstat_raw: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Join patch/numstat/raw parts into file change dicts.

        Step 7: thin facade over ``GitService._git_join_diff_parts``.
        """
        return self._git._git_join_diff_parts(patches, numstat_raw)

    async def _git_untracked_entry(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        rel: str,
    ) -> dict[str, Any]:
        """Render an untracked path as an added-file diff entry.

        Step 7: thin facade over ``GitService._git_untracked_entry``.
        """
        return await self._git._git_untracked_entry(runtime, instance_id, repo_root, env, rel)

    async def _git_untracked_fallback(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        rel: str,
    ) -> dict[str, Any]:
        """Render an unreadable/odd untracked path without patch text.

        Step 7: thin facade over ``GitService._git_untracked_fallback``.
        """
        return await self._git._git_untracked_fallback(runtime, instance_id, repo_root, env, rel)

    async def _git_commit_details(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
        commit_hash: str,
    ) -> dict[str, Any]:
        """Build commit details with file changes, numstat and hunks.

        Step 7: thin facade over ``GitService._git_commit_details``.
        """
        return await self._git._git_commit_details(runtime, instance_id, repo_root, env, commit_hash)

    async def execute_git_operation(
        self,
        workspace_id: uuid.UUID,
        operation: str,
        repo_path: str | None = None,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one whitelisted git operation inside a workspace.

        Step 7: thin facade over ``GitService.execute_git_operation``.
        """
        return await self._git.execute_git_operation(workspace_id, operation, repo_path, args)

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

        Step 7: thin facade over ``GitService._execute_git_operation_inner``.
        """
        return await self._git._execute_git_operation_inner(workspace_id, instance_id, runtime, operation, repo_path, params)

    async def _git_fresh_snapshot(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        repo_root: str,
        env: dict[str, str],
    ) -> dict[str, Any]:
        """Return a fresh single-repo snapshot after a mutation.

        Step 7: thin facade over ``GitService._git_fresh_snapshot``.
        """
        return await self._git._git_fresh_snapshot(runtime, instance_id, repo_root, env)

    def _git_mutation_result(
        self, snapshot: dict[str, Any], **extra: Any
    ) -> dict[str, Any]:
        """Wrap a mutation outcome with its fresh snapshot.

        Step 7: thin facade over ``GitService._git_mutation_result``.
        """
        return self._git._git_mutation_result(snapshot, **extra)

    async def _git_op_working_diff(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Return staged vs unstaged working-tree diffs.

        Step 7: thin facade over ``GitService._git_op_working_diff``.
        """
        return await self._git._git_op_working_diff(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_commit_details(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Return full details for one commit.

        Step 7: thin facade over ``GitService._git_op_commit_details``.
        """
        return await self._git._git_op_commit_details(runtime, instance_id, repo_root, env, params, timeout, **_)

    @staticmethod
    def _git_require_paths(params: dict[str, Any], operation: str) -> list[str]:
        """Extract and validate repo-relative paths from *params*.

        Step 7: thin facade over ``GitService._git_require_paths``.
        """
        return GitService._git_require_paths(params, operation)

    async def _git_op_stage(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Stage paths via ``git add -- <paths>``.

        Step 7: thin facade over ``GitService._git_op_stage``.
        """
        return await self._git._git_op_stage(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_unstage(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Unstage paths (unborn-safe via ``rm --cached`` fallback).

        Step 7: thin facade over ``GitService._git_op_unstage``.
        """
        return await self._git._git_op_unstage(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_discard(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Discard worktree changes; untracked paths are cleaned exactly.

        Step 7: thin facade over ``GitService._git_op_discard``.
        """
        return await self._git._git_op_discard(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_commit(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Commit the index only; message passed as a single argv element.

        Step 7: thin facade over ``GitService._git_op_commit``.
        """
        return await self._git._git_op_commit(runtime, instance_id, repo_root, env, params, timeout, **_)

    def _git_remote_arg(self, params: dict[str, Any]) -> str | None:
        """Return the validated remote name, if any.

        Step 7: thin facade over ``GitService._git_remote_arg``.
        """
        return self._git._git_remote_arg(params)

    async def _git_op_fetch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Fetch with prune (non-interactive, PAT via askpass).

        Step 7: thin facade over ``GitService._git_op_fetch``.
        """
        return await self._git._git_op_fetch(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_current_branch(
        self, runtime, instance_id, repo_root, env
    ) -> str | None:
        """Return the current branch name, or None when detached/unborn.

        Step 7: thin facade over ``GitService._git_current_branch``.
        """
        return await self._git._git_current_branch(runtime, instance_id, repo_root, env)

    async def _git_op_pull(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Pull following repo config (ff/merge), never interactive.

        Step 7: thin facade over ``GitService._git_op_pull``.
        """
        return await self._git._git_op_pull(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_push(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Push following the branch upstream when one exists.

        Step 7: thin facade over ``GitService._git_op_push``.
        """
        return await self._git._git_op_push(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_sync(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Sync = pull, then push when ahead of upstream.

        Step 7: thin facade over ``GitService._git_op_sync``.
        """
        return await self._git._git_op_sync(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_verify_branch_exists(
        self, runtime, instance_id, repo_root, env, branch: str
    ) -> bool:
        """Return True when local branch *branch* exists.

        Step 7: thin facade over ``GitService._git_verify_branch_exists``.
        """
        return await self._git._git_verify_branch_exists(runtime, instance_id, repo_root, env, branch)

    async def _git_verify_ref_exists(
        self, runtime, instance_id, repo_root, env, ref: str
    ) -> bool:
        """Return True when *ref* resolves (branch, tag or commit).

        Step 7: thin facade over ``GitService._git_verify_ref_exists``.
        """
        return await self._git._git_verify_ref_exists(runtime, instance_id, repo_root, env, ref)

    async def _git_op_checkout_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Checkout a local branch (dirty-worktree errors surface).

        Step 7: thin facade over ``GitService._git_op_checkout_branch``.
        """
        return await self._git._git_op_checkout_branch(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_checkout_commit(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Detach HEAD at *commit* (dirty-worktree errors surface).

        Step 7: thin facade over ``GitService._git_op_checkout_commit``.
        """
        return await self._git._git_op_checkout_commit(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_checkout_remote_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Track a remote branch locally (fetch + checkout/create).

        Step 7: thin facade over ``GitService._git_op_checkout_remote_branch``.
        """
        return await self._git._git_op_checkout_remote_branch(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_create_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Create a branch; optionally check it out (``checkout=True``).

        Step 7: thin facade over ``GitService._git_op_create_branch``.
        """
        return await self._git._git_op_create_branch(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_rename_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Rename a branch (defaults to the current branch).

        Step 7: thin facade over ``GitService._git_op_rename_branch``.
        """
        return await self._git._git_op_rename_branch(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_delete_branch(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Safe-delete a branch (``-d`` only; current branch protected).

        Step 7: thin facade over ``GitService._git_op_delete_branch``.
        """
        return await self._git._git_op_delete_branch(runtime, instance_id, repo_root, env, params, timeout, **_)

    def _git_merge_msg(self, params: dict[str, Any], default: str) -> list[str]:
        """Return ``-m <message>`` argv for merges (single argv element).

        Step 7: thin facade over ``GitService._git_merge_msg``.
        """
        return self._git._git_merge_msg(params, default)

    async def _git_op_merge_into_current(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Merge *branch* into the current branch (fast-forward allowed).

        Step 7: thin facade over ``GitService._git_op_merge_into_current``.
        """
        return await self._git._git_op_merge_into_current(runtime, instance_id, repo_root, env, params, timeout, **_)

    async def _git_op_merge_current_into(
        self, runtime, instance_id, repo_root, env, params, timeout,
        workspace_id: uuid.UUID | None = None, **_: Any
    ) -> dict[str, Any]:
        """Merge the current branch into *target* and return to the start branch.

        Step 7: thin facade over ``GitService._git_op_merge_current_into``.
        """
        return await self._git._git_op_merge_current_into(runtime, instance_id, repo_root, env, params, timeout, workspace_id, **_)

    async def _git_op_merge_abort(
        self, runtime, instance_id, repo_root, env, params, timeout, **_: Any
    ) -> dict[str, Any]:
        """Abort an in-progress merge (no-op error when none active).

        Step 7: thin facade over ``GitService._git_op_merge_abort``.
        """
        return await self._git._git_op_merge_abort(runtime, instance_id, repo_root, env, params, timeout, **_)

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

        Step 2: thin facade over ``ImageManager.build_image``.
        """
        return await self._images.build_image(
            runtime_type=runtime_type,
            build_job_id=build_job_id,
            dockerfile_content=dockerfile_content,
            image_tag=image_tag,
            base_distro=base_distro,
            init_script=init_script,
            image_path=image_path,
            progress_callback=progress_callback,
        )

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
    ) -> "ImageArtifactInfo":
        """Create an image artifact from a workspace.

        The runtime must support artifact capture.

        Step 2: thin facade over ``ImageManager.create_image_artifact``.
        """
        return await self._images.create_image_artifact(workspace_id, name)

    async def list_image_artifacts(
        self,
        workspace_id: uuid.UUID,
    ) -> list["ImageArtifactInfo"]:
        """List all captured image artifacts for a workspace.

        Step 2: thin facade over ``ImageManager.list_image_artifacts``.
        """
        return await self._images.list_image_artifacts(workspace_id)

    async def delete_image_artifact(
        self,
        workspace_id: uuid.UUID,
        image_artifact_id: str,
    ) -> None:
        """Delete a captured image artifact.

        Step 2: thin facade over ``ImageManager.delete_image_artifact``.
        """
        await self._images.delete_image_artifact(workspace_id, image_artifact_id)

    async def delete_image_reference(
        self,
        *,
        runtime_type: str,
        image_ref: str,
    ) -> str:
        """Delete a concrete runtime image reference without requiring a workspace.

        Returns 'deleted' or 'already_absent' to indicate the result.

        Step 2: thin facade over ``ImageManager.delete_image_reference``.
        """
        return await self._images.delete_image_reference(
            runtime_type=runtime_type,
            image_ref=image_ref,
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

        Step 2: thin facade over
        ``ImageManager.create_workspace_from_image_artifact``.
        """
        return await self._images.create_workspace_from_image_artifact(
            image_artifact_id,
            new_workspace_id,
            runtime_type,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            env_vars=env_vars,
            files=files,
            ssh_keys=ssh_keys,
        )
