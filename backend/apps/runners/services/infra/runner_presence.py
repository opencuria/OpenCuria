"""Bounded database lane for runner liveness and active-session checks."""

from __future__ import annotations

import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from asgiref.sync import sync_to_async
from django.db import close_old_connections

T = TypeVar("T")

# Presence checks must not queue behind thread-sensitive tasks that perform a
# full workspace reconcile (which can synchronously wait for runner RPCs).
# A single process-wide worker bounds the lane and reuses its DB connection.
_PRESENCE_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="runner-presence-db",
)


def _run_with_connection_hygiene(function: Callable[..., T], *args, **kwargs) -> T:
    """Run a repository call with Django connection cleanup on its DB thread."""
    close_old_connections()
    try:
        return function(*args, **kwargs)
    finally:
        close_old_connections()


async def run_presence_query(function: Callable[..., T], *args, **kwargs) -> T:
    """Run a short runner-presence repository call on the bounded DB lane."""
    call = functools.partial(_run_with_connection_hygiene, function, *args, **kwargs)
    return await sync_to_async(
        call,
        thread_sensitive=False,
        executor=_PRESENCE_EXECUTOR,
    )()
