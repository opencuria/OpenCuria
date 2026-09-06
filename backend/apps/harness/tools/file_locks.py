"""Per-path asyncio locks for read-modify-write file tools.

``WriteTool`` and ``EditTool`` wrap their read-modify-write section in
``async with get_lock(path)`` so concurrent tool calls in one step (same
event loop) serialize instead of losing updates. Process-local only.
"""

from __future__ import annotations

import asyncio
import os

_locks: dict[str, asyncio.Lock] = {}


def get_lock(path: str) -> asyncio.Lock:
    """Return the process-local lock for normalized *path*."""
    key = os.path.normpath(path or "")
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock
