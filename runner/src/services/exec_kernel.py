"""Execution kernel pure helpers (Steps 1 + 5).

Canonical home for stateless command/path sanitizers previously living on
``WorkspaceService`` in :mod:`src.service`:

- shell operator / redirection sentinels (``_SHELL_OPERATOR_TOKENS``,
  ``_REDIRECTION_RE``),
- :func:`normalise_command_args` (was
  ``WorkspaceService._normalise_command_args``),
- :func:`sanitize_path`, :func:`sanitize_exec_workdir`,
  :func:`sanitize_filename` (were the ``_sanitize_*`` static methods).

Step 5 additions (behavior-preserving cutover of the exec kernel):

- :func:`credential_path_helpers` (was
  ``WorkspaceService._credential_path_helpers``; verbatim shell lines).
  Pure — takes no state.
- :func:`wrap_command_with_persistent_env` (was
  ``WorkspaceService._wrap_command_with_persistent_env``; verbatim
  logic). Pure — the guest credential env-file path is an explicit
  parameter (default is the literal guest path, matching the canonical
  ``src.services.credentials.WORKSPACE_CREDENTIAL_ENV_FILE`` value) so
  this module never imports ``src.services.credentials`` (avoids an
  import cycle: ``credentials`` imports the wrap helpers from here).
- :func:`exec_command` / :func:`exec_command_stream` (were
  ``WorkspaceService._exec_command`` / ``_exec_command_stream``;
  verbatim logic) taking ``(runtime, instance_id, command)`` plus an
  optional ``credential_env_file`` path.
- :class:`KeyedLockMap`: tiny get-or-create never-evict keyed lock map
  encapsulating the guard+dict pattern used by the background / git /
  desktop lock factories. ``WorkspaceService`` keeps its ``_..._locks``
  dict attributes and factory methods as thin aliases/delegates, so
  existing callers and tests keep working.
- :class:`WorkspaceContext`: dataclass with ``workspace_id``, ``info``,
  ``runtime``; classmethod :meth:`WorkspaceContext.resolve` performs
  ``_get_cached`` + ``_get_runtime`` + ``instance_id`` check with
  messages IDENTICAL to the ``WorkspaceService`` preamble. Used in the
  NEW code paths only (credentials manager + exec kernel's
  ``exec_command`` helpers); other clusters' preambles are left for
  their own move steps (Steps 6-8; lifecycle owns the final cutover) to
  avoid churn.

Only stdlib + typing (+ ``src.models`` / ``src.runtime.base`` for type
hints); no dependency on ``WorkspaceService``. The module never imports
``src.service`` or ``src.services.credentials`` (no cycle).
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import uuid
from collections.abc import AsyncIterator, Callable, Hashable
from dataclasses import dataclass

from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend

#: Guest path of the persistent credential env file sourced by exec
#: wrappers. Literal matches the canonical
#: ``src.services.credentials.WORKSPACE_CREDENTIAL_ENV_FILE`` value;
#: kept as a literal (not an import) so this module has no dependency
#: on ``src.services.credentials`` (``credentials`` imports the wrap
#: helpers from here — importing back would be a cycle).
#: Callers that need the canonical object should import it from
#: ``src.services.credentials``; managers receive it as a constructor
#: parameter defaulting to that constant.
DEFAULT_CREDENTIAL_ENV_FILE = "/root/.opencuria-env.sh"

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


def normalise_command_args(raw_args: list[str] | str) -> list[str]:
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


def sanitize_path(path: str) -> str:
    """Ensure *path* is under ``/workspace`` and prevent traversal."""
    normalized = os.path.normpath(path)
    if normalized != "/workspace" and not normalized.startswith("/workspace/"):
        raise ValueError(f"Path must be under /workspace: {path}")
    return normalized


def sanitize_exec_workdir(path: str) -> str:
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


def sanitize_filename(filename: str) -> str:
    """Validate and return a safe filename for workspace uploads."""
    if not filename:
        raise ValueError("Filename must not be empty")

    if filename != os.path.basename(filename):
        raise ValueError("Filename must not contain path separators")

    if filename in {".", ".."}:
        raise ValueError("Invalid filename")

    return filename


# -- credential path helpers + persistent-env wrap (Step 5) -----------------
# Verbatim logic moved from ``WorkspaceService._credential_path_helpers``
# / ``WorkspaceService._wrap_command_with_persistent_env`` (src/service.py
# ~902-969). Pure functions: the guest env-file path and the environment
# paths/blocks are explicit parameters (defaults are literals matching
# the canonical ``src.services.credentials`` values) so this module
# never imports ``src.services.credentials``.


def credential_path_helpers(
    *,
    credential_environment: str = "/etc/environment",
    credential_environment_start: str = "# OPENCURIA_CREDENTIALS_START",
    credential_environment_end: str = "# OPENCURIA_CREDENTIALS_END",
) -> list[str]:
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
        f"  env_file={shlex.quote(credential_environment)}",
        '  if [ ! -f "$env_file" ]; then',
        "    return",
        "  fi",
        "  tmp_env=$(mktemp)",
        f"  awk '/{credential_environment_start}/{{skip=1}} "
        f"/{credential_environment_end}/{{skip=0; next}} !skip' "
        '"$env_file" > "$tmp_env" || true',
        '  cat "$tmp_env" > "$env_file"',
        '  rm -f "$tmp_env"',
        "}",
    ]


def wrap_command_with_persistent_env(
    command: dict,
    credential_env_file: str = DEFAULT_CREDENTIAL_ENV_FILE,
) -> dict:
    """Source persistent workspace credentials before running a command."""
    normalised_args = normalise_command_args(command["args"])
    extra_env = command.get("env") or {}
    extra_exports = "; ".join(
        f"export {key}={shlex.quote(str(value))}" for key, value in extra_env.items()
    )
    source = (
        f"if [ -f {shlex.quote(credential_env_file)} ]; then "
        f". {shlex.quote(credential_env_file)}; fi"
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


# -- stateful exec entry points (Step 5) -------------------------------------
# Verbatim logic moved from ``WorkspaceService._exec_command`` /
# ``WorkspaceService._exec_command_stream`` (src/service.py ~837-888):
# wrap with the persistent-env wrapper, normalise, then call the
# runtime. The only difference is that (runtime, instance_id) arrive as
# explicit parameters (plus an optional credential-env-file override)
# instead of via a workspace preamble.


async def exec_command(
    runtime: RuntimeBackend,
    instance_id: str,
    command: dict,
    credential_env_file: str = DEFAULT_CREDENTIAL_ENV_FILE,
) -> tuple[int, str]:
    """Execute a structured command dict inside a workspace.

    Args:
        runtime: The runtime backend to use.
        instance_id: Runtime-specific instance ID.
        command: Dict with keys ``args``, ``workdir``, ``env``,
            ``description``.
        credential_env_file: Guest path of the persistent credential env
            file sourced by the wrapper.

    Returns:
        Tuple of (exit_code, output).
    """
    wrapped_command = wrap_command_with_persistent_env(
        command, credential_env_file
    )
    command_args = normalise_command_args(wrapped_command["args"])
    return await runtime.exec_command_wait(
        instance_id,
        command=command_args,
        workdir=wrapped_command.get("workdir"),
        env=wrapped_command.get("env"),
    )


async def exec_command_stream(
    runtime: RuntimeBackend,
    instance_id: str,
    command: dict,
    credential_env_file: str = DEFAULT_CREDENTIAL_ENV_FILE,
) -> AsyncIterator[str]:
    """Execute a structured command dict and stream output lines.

    Args:
        runtime: The runtime backend to use.
        instance_id: Runtime-specific instance ID.
        command: Dict with keys ``args``, ``workdir``, ``env``,
            ``description``.
        credential_env_file: Guest path of the persistent credential env
            file sourced by the wrapper.

    Yields:
        Raw output lines from the command.
    """
    wrapped_command = wrap_command_with_persistent_env(
        command, credential_env_file
    )
    command_args = normalise_command_args(wrapped_command["args"])
    async for line in runtime.exec_command(
        instance_id,
        command=command_args,
        workdir=wrapped_command.get("workdir"),
        env=wrapped_command.get("env"),
    ):
        yield line


# -- KeyedLockMap + WorkspaceContext (Step 5) --------------------------------


class KeyedLockMap:
    """Get-or-create never-evict keyed lock map.

    Encapsulates the guard+dict pattern used by the background / git /
    desktop ``_..._lock`` factories: repeated lookups for the same key
    return the identical lock object, and entries are never evicted (a
    drop in the release/wait window would hand a third caller a
    different lock object and silently break serialisation).

    The underlying dict stays accessible via :attr:`locks` (and
    :meth:`as_dict`) so ``WorkspaceService`` property aliases onto
    manager-owned ``_..._locks`` dicts keep working for tests poking
    them (e.g. ``service._desktop_locks.get(workspace_id)``), and the
    guard via :attr:`guard` so ``..._guard`` aliases keep working.
    """

    def __init__(
        self,
        locks: dict[Hashable, asyncio.Lock] | None = None,
        guard: asyncio.Lock | None = None,
    ) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = (
            locks if locks is not None else {}
        )
        self._guard: asyncio.Lock = guard if guard is not None else asyncio.Lock()

    @property
    def locks(self) -> dict[Hashable, asyncio.Lock]:
        """Return the underlying key -> lock dict (live alias)."""
        return self._locks

    @property
    def guard(self) -> asyncio.Lock:
        """Return the guard lock serialising get-or-create."""
        return self._guard

    @guard.setter
    def guard(self, value: asyncio.Lock) -> None:
        self._guard = value

    def as_dict(self) -> dict[Hashable, asyncio.Lock]:
        """Return the underlying key -> lock dict (live alias)."""
        return self._locks

    async def get(self, key: Hashable) -> asyncio.Lock:
        """Return the lock for *key*, creating it on first use."""
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock


@dataclass
class WorkspaceContext:
    """Resolved workspace preamble: cached info + runtime backend.

    Canonical replacement for the ``_get_cached`` + ``_get_runtime`` +
    ``instance_id`` preamble repeated at ~30 ``WorkspaceService`` entry
    points. :meth:`resolve` raises with messages IDENTICAL to the facade
    preamble:

    - unknown workspace -> ``ValueError("Workspace {id} not found")``
      (raised by the ``get_cached`` lookup),
    - unknown runtime -> ``RuntimeError("Runtime ... not available ...")``
      (raised by the ``get_runtime`` lookup),
    - missing instance -> ``RuntimeError("Workspace has no instance
      assigned")``.
    """

    workspace_id: uuid.UUID
    info: WorkspaceInfo
    runtime: RuntimeBackend

    @classmethod
    async def resolve(
        cls,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo],
        get_runtime: Callable[[uuid.UUID], RuntimeBackend],
        workspace_id: uuid.UUID,
    ) -> WorkspaceContext:
        """Resolve *workspace_id* to (info, runtime), checking instance."""
        info = get_cached(workspace_id)
        runtime = get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        return cls(workspace_id=workspace_id, info=info, runtime=runtime)


class ExecKernel:
    """Stateful exec kernel: wrap + normalise + runtime dispatch.

    Thin stateful shell over the module-level :func:`exec_command` /
    :func:`exec_command_stream` / :func:`wrap_command_with_persistent_env`
    helpers. Wired with an optional ``credential_env_file`` (defaults to
    the guest literal matching the canonical
    ``src.services.credentials.WORKSPACE_CREDENTIAL_ENV_FILE`` value;
    ``WorkspaceService`` passes the canonical constant explicitly).

    ``WorkspaceService`` keeps ``_exec_command`` / ``_exec_command_stream``
    / ``_normalise_command_args`` / ``_wrap_command_with_persistent_env``
    as thin delegates onto a kernel instance, so existing callers keep
    working.
    """

    def __init__(
        self,
        credential_env_file: str = DEFAULT_CREDENTIAL_ENV_FILE,
    ) -> None:
        self._credential_env_file = credential_env_file

    @property
    def credential_env_file(self) -> str:
        """Return the guest credential env-file path used by wrappers."""
        return self._credential_env_file

    def normalise_command_args(
        self, raw_args: list[str] | str
    ) -> list[str]:
        """Return command args suitable for runtime execution."""
        return normalise_command_args(raw_args)

    def wrap_command_with_persistent_env(self, command: dict) -> dict:
        """Source persistent workspace credentials before running a command."""
        return wrap_command_with_persistent_env(
            command, self._credential_env_file
        )

    @staticmethod
    def credential_path_helpers() -> list[str]:
        """Return shell helper functions used by inject and remove scripts."""
        return credential_path_helpers()

    async def exec_command(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> tuple[int, str]:
        """Execute a structured command dict inside a workspace."""
        return await exec_command(
            runtime, instance_id, command, self._credential_env_file
        )

    async def exec_command_stream(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> AsyncIterator[str]:
        """Execute a structured command dict and stream output lines."""
        async for line in exec_command_stream(
            runtime, instance_id, command, self._credential_env_file
        ):
            yield line

    async def resolve(
        self,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo],
        get_runtime: Callable[[uuid.UUID], RuntimeBackend],
        workspace_id: uuid.UUID,
    ) -> WorkspaceContext:
        """Resolve *workspace_id* via :meth:`WorkspaceContext.resolve`."""
        return await WorkspaceContext.resolve(
            get_cached, get_runtime, workspace_id
        )


__all__ = [
    "DEFAULT_CREDENTIAL_ENV_FILE",
    "ExecKernel",
    "KeyedLockMap",
    "WorkspaceContext",
    "_REDIRECTION_RE",
    "_SHELL_OPERATOR_TOKENS",
    "credential_path_helpers",
    "exec_command",
    "exec_command_stream",
    "normalise_command_args",
    "sanitize_exec_workdir",
    "sanitize_filename",
    "sanitize_path",
    "wrap_command_with_persistent_env",
]
