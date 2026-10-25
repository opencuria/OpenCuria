"""Process-local provider account revocation notifications.

Account mutations publish after commit; answer monitors subscribe without
coupling account ownership to turn orchestration or provider configuration.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Iterable

_listener_lock = threading.Lock()
_listeners: dict[uuid.UUID, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}


def register_revocation_listener(
    account_ids: Iterable[str],
    *,
    loop: asyncio.AbstractEventLoop,
    event: asyncio.Event,
) -> set[uuid.UUID]:
    """Watch account IDs on the current loop; return normalized identifiers."""
    normalized: set[uuid.UUID] = set()
    for value in account_ids:
        try:
            normalized.add(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            continue
    with _listener_lock:
        for account_id in normalized:
            _listeners.setdefault(account_id, set()).add((loop, event))
    return normalized


def unregister_revocation_listener(
    account_ids: Iterable[uuid.UUID], *, event: asyncio.Event
) -> None:
    """Remove one event from the local post-commit wakeup registry."""
    with _listener_lock:
        for account_id in account_ids:
            listeners = _listeners.get(account_id)
            if listeners is None:
                continue
            remaining = {pair for pair in listeners if pair[1] is not event}
            if remaining:
                _listeners[account_id] = remaining
            else:
                _listeners.pop(account_id, None)


def notify_account_revoked(account_id: uuid.UUID) -> None:
    """Wake local turn monitors after a committed account revocation."""
    with _listener_lock:
        listeners = tuple(_listeners.get(uuid.UUID(str(account_id)), ()))
    for loop, event in listeners:
        if not loop.is_closed():
            loop.call_soon_threadsafe(event.set)
