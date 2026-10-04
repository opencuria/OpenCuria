"""Runner-local capture admission using the registry's lifecycle boundary."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from .workspace_registry import WorkspaceRegistry

Method = TypeVar("Method", bound=Callable[..., Any])


class CaptureFence:
    """Reject new work during capture and drain only active runtime calls.

    The registry supplies the sole lifecycle lock. Backend persisted ownership
    still fences the wider stop/scrub/capture operation and reconnect gaps.
    """

    def __init__(self, registry: WorkspaceRegistry | None = None) -> None:
        self._registry = registry
        self._capturing: set[uuid.UUID] = set()
        self._users: dict[tuple[uuid.UUID, asyncio.Task[Any] | None], int] = {}
        self._changed = asyncio.Condition()
        self._epochs: dict[tuple[uuid.UUID, asyncio.Task[Any] | None], int] = {}
        self._coordinated: set[tuple[uuid.UUID, asyncio.Task[Any] | None]] = set()

    def check_available(self, workspace_id: uuid.UUID) -> None:
        """Reject unadmitted work while this workspace is being captured."""
        key = (workspace_id, asyncio.current_task())
        if key not in self._users and workspace_id in self._capturing:
            raise RuntimeError("Workspace is capturing image")

    @asynccontextmanager
    async def interaction(
        self, workspace_id: uuid.UUID, *, coordinated: bool = False
    ) -> AsyncIterator[None]:
        """Admit after the lifecycle boundary, rejecting stale queued work.

        Coordinated callers already own the registry lock. Nested calls reuse
        admission, so lifecycle cleanup can close streams without reacquiring it.
        """
        key = (workspace_id, asyncio.current_task())
        self.check_available(workspace_id)
        if key not in self._users and not coordinated and self._registry is not None:
            async with self._registry.live_boundary(workspace_id):
                self.check_available(workspace_id)
                self._users[key] = 1
        else:
            self._users[key] = self._users.get(key, 0) + 1
        if self._registry is not None and key not in self._epochs:
            self._epochs[key] = self._registry._epochs.get(workspace_id, 0)
        if coordinated:
            self._coordinated.add(key)
        try:
            yield
        finally:
            self._users[key] -= 1
            if not self._users[key]:
                del self._users[key]
                self._epochs.pop(key, None)
                self._coordinated.discard(key)
            async with self._changed:
                self._changed.notify_all()

    def check_current(self, workspace_id: uuid.UUID) -> None:
        """Reject admitted work made stale while waiting for a manager lock."""
        key = (workspace_id, asyncio.current_task())
        if self._registry is not None and key not in self._coordinated:
            epoch = self._epochs.get(key)
            if epoch is not None and epoch != self._registry._epochs.get(
                workspace_id, 0
            ):
                self._registry.get_cached(workspace_id)
                raise RuntimeError("Workspace lifecycle changed while waiting")

    @asynccontextmanager
    async def capture(self, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        """Hold capture admission until exit, releasing on errors/cancellation.

        The caller must acquire the existing lifecycle context before draining,
        allowing an already-running stop to finish its stream cleanup first.
        """
        if workspace_id in self._capturing:
            raise RuntimeError("Workspace is capturing image")
        self._capturing.add(workspace_id)
        try:
            yield
        finally:
            self._capturing.remove(workspace_id)

    async def drain(self, workspace_id: uuid.UUID) -> None:
        """Wait for active calls, not idle iterator consumers or session readers."""
        async with self._changed:
            await self._changed.wait_for(
                lambda: not any(ws == workspace_id for ws, _ in self._users)
            )


def live_interaction(method: Method) -> Method:
    """Guard calls and each active iterator read, including session-ID writes."""
    signature = inspect.signature(method)

    def workspace(
        self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> uuid.UUID | None:
        bound = signature.bind(self, *args, **kwargs).arguments
        if "workspace_id" in bound:
            return bound["workspace_id"]
        for argument, attribute in (
            ("terminal_id", "_terminals"),
            ("connection_id", "_streams"),
            ("tunnel_id", "_desktop_proxy_tunnels"),
        ):
            if argument in bound:
                entry = getattr(self, attribute).get(bound[argument])
                return getattr(entry, "workspace_id", None)
        return None

    @asynccontextmanager
    async def admission(
        self: Any, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> AsyncIterator[None]:
        fence = self.capture_fence
        ws = workspace(self, args, kwargs)
        if fence is None or ws is None:
            yield
        else:
            async with fence.interaction(ws):
                yield

    if inspect.isasyncgenfunction(method):

        @wraps(method)
        async def iterator(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
            generator = method(self, *args, **kwargs)
            try:
                while True:
                    async with admission(self, args, kwargs):
                        try:
                            item = await anext(generator)
                        except StopAsyncIteration:
                            return
                    yield item
            finally:
                await generator.aclose()

        return iterator  # type: ignore[return-value]

    @wraps(method)
    async def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with admission(self, args, kwargs):
            return await method(self, *args, **kwargs)

    return wrapped  # type: ignore[return-value]
