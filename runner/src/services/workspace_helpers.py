"""Shared path validation, credential scripts, and keyed locks for the runner."""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import Hashable


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


class KeyedLockMap:
    """Get-or-create never-evict keyed lock map.

    Encapsulates the guard+dict pattern used by the background / git /
    desktop ``_..._lock`` factories: repeated lookups for the same key
    return the identical lock object, and entries are never evicted (a
    drop in the release/wait window would hand a third caller a
    different lock object and silently break serialisation).

    The underlying dict and guard are exposed for manager integrations that
    need to inspect or share the lock state.
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


__all__ = [
    "KeyedLockMap",
    "credential_path_helpers",
    "sanitize_exec_workdir",
    "sanitize_filename",
    "sanitize_path",
]
