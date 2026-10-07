"""Serialized, cancellation-drained storage mutations within one runtime."""

import asyncio
import contextvars
import hashlib
from functools import wraps
from typing import BinaryIO

_owner = contextvars.ContextVar("storage_owner", default=None)


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

        task = asyncio.create_task(run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    return wrapped
