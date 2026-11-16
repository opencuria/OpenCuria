"""Per-path asyncio locks for read-modify-write file tools.

``WriteTool`` and ``EditTool`` wrap their read-modify-write section in
``async with get_lock(path)`` so concurrent tool calls in one step (same
event loop) serialize instead of losing updates. Process-local only.
"""

from __future__ import annotations

import asyncio
import os

_locks: dict[str, asyncio.Lock] = {}


def get_lock(path: str, *, case_sensitive: bool = True) -> asyncio.Lock:
    """Return the process-local lock for normalized *path*.

    Paths on case-insensitive guests share one lock per case-folded spelling.
    """
    key = os.path.normpath(path or "")
    if not case_sensitive:
        key = key.casefold()
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock
