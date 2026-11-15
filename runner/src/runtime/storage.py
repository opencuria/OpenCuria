"""Serialized, cancellation-drained storage mutations within one runtime."""

import asyncio
import contextvars
import hashlib
import inspect
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any, BinaryIO, ParamSpec, TypeVar

_owner = contextvars.ContextVar("storage_owner", default=None)
_workspace_owners = contextvars.ContextVar("workspace_owners", default=None)
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


def storage_mutation(
    method: Callable[..., Coroutine[Any, Any, _Result]],
) -> Callable[..., Coroutine[Any, Any, _Result]]:
    """Hold the runtime lock until all effects finish, including SDK threads."""

    @wraps(method)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> _Result:
        if _owner.get() == (self, asyncio.current_task()):
            return await method(self, *args, **kwargs)
        if not hasattr(self, "_storage_lock"):
            self._storage_lock = asyncio.Lock()

        async def run() -> _Result:
            async with self._storage_lock:
                token = _owner.set((self, asyncio.current_task()))
                try:
                    return await method(self, *args, **kwargs)
                finally:
                    _owner.reset(token)

        return await _await_drained(asyncio.create_task(run()))

    return wrapped


def workspace_mutation(
    method: Callable[..., Coroutine[Any, Any, _Result]],
) -> Callable[..., Coroutine[Any, Any, _Result]]:
    """Serialize one guest's lifecycle while allowing unrelated guests to run.

    Keep the task identity in the context: a spawned child must acquire its
    own lock, rather than inheriting permission to mutate its parent's guest.
    """
    signature = inspect.signature(method)

    @wraps(method)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> _Result:
        arguments = signature.bind(self, *args, **kwargs).arguments
        instance_id = arguments.get("instance_id", arguments.get("new_instance_id"))
        if instance_id is None:
            instance_id = arguments["config"].workspace_id
        key = (self, instance_id)
        if (_workspace_owners.get() or {}).get(key) is asyncio.current_task():
            return await method(self, *args, **kwargs)
        if not hasattr(self, "_workspace_locks"):
            self._workspace_locks = {}
        lock = self._workspace_locks.setdefault(instance_id, asyncio.Lock())

        async def run() -> _Result:
            async with lock:
                owners = {
                    **(_workspace_owners.get() or {}),
                    key: asyncio.current_task(),
                }
                token = _workspace_owners.set(owners)
                try:
                    return await method(self, *args, **kwargs)
                finally:
                    _workspace_owners.reset(token)

        return await _await_drained(asyncio.create_task(run()))

    return wrapped
