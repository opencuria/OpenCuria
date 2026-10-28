"""Serialized, cancellation-drained storage mutations within one runtime."""

import asyncio
import contextvars
import hashlib
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, BinaryIO, ParamSpec, TypeVar

_owner = contextvars.ContextVar("storage_owner", default=None)
_Params = ParamSpec("_Params")
_Result = TypeVar("_Result")


async def _await_drained(task: asyncio.Task[_Result]) -> _Result:
    """Keep ownership until an issued effect settles, even after cancellation."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


def drain_cancellation(
    method: Callable[_Params, Coroutine[Any, Any, _Result]],
) -> Callable[_Params, Coroutine[Any, Any, _Result]]:
    """Drain a long operation without holding the shared storage lock."""

    @wraps(method)
    async def wrapped(*args: _Params.args, **kwargs: _Params.kwargs) -> _Result:
        return await _await_drained(asyncio.create_task(method(*args, **kwargs)))

    return wrapped


def sha256_digest(stream: BinaryIO) -> str:
    """Hash a binary stream with bounded memory on every supported Python."""
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def storage_mutation(method):
    """Hold the runtime lock until all effects finish, including SDK threads."""

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        if _owner.get() is self:
            return await method(self, *args, **kwargs)
        if not hasattr(self, "_storage_lock"):
            self._storage_lock = asyncio.Lock()

        async def run():
            async with self._storage_lock:
                token = _owner.set(self)
                try:
                    return await method(self, *args, **kwargs)
                finally:
                    _owner.reset(token)

        return await _await_drained(asyncio.create_task(run()))

    return wrapped
