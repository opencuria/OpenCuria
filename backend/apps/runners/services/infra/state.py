"""In-memory state stores for the runners service (Phase 4).

Ownership documentation + dict-compatible containers. No Django
dependency; no behaviour change.

The stores deliberately subclass :class:`dict` so every existing
access pattern keeps working unchanged — tests write entries with
direct dict syntax (``service._active_desktops[ws_id] = {...}``,
``service._git_pending[req] = _PendingGitRequest(...)``,
``service._pending_process_verify[...] = ...``) and helpers like
``isinstance(service._git_pending, dict)`` stay True. The classes only
add a documented owner plus small conveniences (``discard``/``contains``
aliases, optional expiry sweep); they are NOT a new Future system.

Deliberately NOT moved here (Phase-5 remainder, documented):

- ``_active_terminals`` / ``_terminal_workspace_runner`` /
  ``_active_desktops`` / ``_desktop_workspace_runner`` stay class
  attributes on the ``RunnerService`` facade: tests read and write them
  directly, so rebinding them per instance would break the shared-state
  contract. The facade ``__init__`` must NOT shadow them.
- ``_call_pending`` (nested ``event -> id -> Future``) and
  ``_pending_unknown_workspace_cleanup`` /
  ``_pending_credential_inject`` (sets) keep their plain builtin types.
"""

from __future__ import annotations

import time
from collections.abc import Hashable, Iterable
from typing import Any


class PendingMap(dict):
    """Dict subclass for pending-RPC registries (request_id -> entry).

    Owner: :class:`RpcRegistryMixin` (``_process_pending``,
    ``_git_pending``) and the heartbeat reconciler
    (``_pending_process_verify``). Entries are resolved by reply events
    (``_resolve_process_future`` / ``_resolve_git_future``) or drained by
    the reconciler; stale entries are dropped by the callers on
    timeout/disconnect (no automatic expiry — see :meth:`sweep_expired`
    for the opted-in timestamped variant).
    """

    #: Advisory TTL (seconds) for timestamped entries swept by
    #: :meth:`sweep_expired`. Not enforced automatically.
    DEFAULT_TTL_SECONDS = 300

    def discard(self, key: Hashable) -> None:
        """Remove *key* if present (set-like convenience, never raises)."""
        self.pop(key, None)

    def contains(self, key: Hashable) -> bool:
        """Return True when *key* has a pending entry."""
        return key in self

    def sweep_expired(
        self,
        now: float | None = None,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> list:
        """Drop ``(key, timestamp)``-shaped entries older than the TTL.

        Only touches values stored as ``(payload, timestamp)`` tuples or
        objects with a ``created_at`` epoch attribute; all other values
        are left alone. Returns the dropped keys.
        """
        current = time.time() if now is None else now
        dropped = []
        for key in list(self.keys()):
            value = self.get(key)
            timestamp: float | None = None
            if isinstance(value, tuple) and len(value) == 2:
                candidate = value[1]
                if isinstance(candidate, (int, float)):
                    timestamp = float(candidate)
            else:
                candidate = getattr(value, "created_at", None)
                if isinstance(candidate, (int, float)):
                    timestamp = float(candidate)
            if timestamp is not None and current - timestamp > ttl_seconds:
                self.pop(key, None)
                dropped.append(key)
        return dropped


class SessionMap(dict):
    """Dict subclass for terminal/desktop session caches.

    Owner: :class:`SessionStoreMixin` (desktop) and the interactive
    session flows (terminals). Keys are workspace-id strings; desktop
    values are normalized state dicts (see
    ``SessionStoreMixin._normalize_desktop_state``).

    Currently documentation-only: the live maps
    (``_active_terminals`` / ``_terminal_workspace_runner`` /
    ``_active_desktops`` / ``_desktop_workspace_runner``) stay class
    attributes on the ``RunnerService`` facade for test compatibility
    and are NOT rebound to ``SessionMap`` instances. Use this class when
    a future phase moves them to instance state.
    """

    def get_session(self, workspace_id: str, default: Any = None) -> Any:
        """Return the cached session for *workspace_id* (dict.get alias)."""
        return self.get(workspace_id, default)

    def put_session(self, workspace_id: str, state: Any) -> None:
        """Cache *state* for *workspace_id* (dict-set alias)."""
        self[workspace_id] = state

    def drop_session(self, workspace_id: str) -> Any:
        """Remove and return the cached session (dict.pop alias)."""
        return self.pop(workspace_id, None)

    def contains(self, workspace_id: str) -> bool:
        """Return True when a session is cached for *workspace_id*."""
        return workspace_id in self

    def asdict(self) -> dict:
        """Return a plain-dict snapshot (convenience for tests/debugging)."""
        return dict(self)

    def workspace_ids(self) -> Iterable[str]:
        """Return the cached workspace-id keys."""
        return list(self.keys())
