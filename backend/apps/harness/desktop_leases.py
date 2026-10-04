"""Run-scoped ownership of runner-managed desktop dependencies."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import anyio
import structlog

log = structlog.get_logger(__name__)


async def cleanup_rpc(awaitable: Any) -> Any:
    """Finish a single RPC despite cancellation; never move SDK teardown tasks."""
    cancelled = False
    with anyio.CancelScope(shield=True):
        task = asyncio.ensure_future(awaitable)
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.done():
                    raise
                cancelled = True
    if cancelled:
        raise asyncio.CancelledError()
    return result


class DesktopLease:
    """Fresh, non-resurrectable lease for one connection or computer-use attempt."""

    def __init__(
        self,
        accessor: Any,
        *,
        kind: str,
        owner_id: str | None = None,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self.accessor = accessor
        self.kind = kind
        self.owner_id = owner_id or str(uuid4())
        self.lease_id = str(uuid4())
        self.epoch = ""
        self.binding: dict[str, Any] = {}
        self.attempted = False
        self.held = False
        self.hold_attempted = False
        self.healthy = True
        self.closed = False
        self._renew_task: asyncio.Task | None = None
        self._on_failure = on_failure
        self._lifecycle_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def owner(self) -> dict[str, str]:
        """Runner stream association, established before process spawn."""
        return {"lease_id": self.lease_id, "epoch": self.epoch}

    def _args(self) -> dict[str, Any]:
        return {
            **self.owner,
            "kind": self.kind,
            "owner_id": self.owner_id,
            "revision": 1,
        }

    async def _rpc(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self.accessor.desktop_action(action, args)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"Desktop lease {action} rejected")
        return result

    async def reserve(self) -> dict[str, Any]:
        """Bind and reserve without starting the desktop, then renew while idle."""
        try:
            async with self._lifecycle_lock:
                if self.closed or self.attempted:
                    raise RuntimeError("Desktop lease cannot be recreated")
                self.binding = await self._rpc("binding", {})
                self.epoch = str(self.binding["epoch"])
                if self.closed:
                    raise RuntimeError("Desktop lease is closed")
                self.attempted = True
                result = await self._rpc("reserve", self._args())
                if (
                    result.get("epoch") != self.epoch
                    or result.get("lease_state") != "reserved"
                ):
                    raise RuntimeError("Desktop lease reservation rejected")
                if self.closed:
                    raise RuntimeError("Desktop lease is closed")
                self._renew_task = asyncio.create_task(self._renew())
                return self.binding
        except BaseException:
            # Preserve the operation's original cancellation/error even when
            # the runner cannot yet confirm closure. A later close retries.
            try:
                await self.aclose()
            except BaseException:
                log.warning("desktop_lease_compensation_failed", lease_id=self.lease_id)
            raise

    async def hold(self) -> None:
        """Activate a reservation; failure interrupts owner, not SDK teardown."""
        try:
            async with self._lifecycle_lock:
                self.check_health()
                if self.held:
                    return
                self.hold_attempted = True
                result = await self._rpc("hold", self._args())
                if (
                    result.get("epoch") != self.epoch
                    or result.get("lease_state") != "held"
                ):
                    raise RuntimeError("Desktop lease activation rejected")
                self.check_health()
                self.held = True
        except BaseException as exc:
            self.healthy = False
            if not self.closed and not isinstance(exc, asyncio.CancelledError):
                if self._on_failure is not None:
                    self._on_failure()
            raise

    def check_health(self) -> None:
        """Fail closed after renewal rejection or closure."""
        if self.closed or not self.healthy or not self.attempted:
            raise RuntimeError("Desktop lease is not healthy")

    async def _renew(self) -> None:
        try:
            while True:
                await asyncio.sleep(45)
                result = await self._rpc("renew", {**self.owner, "revision": 1})
                if result.get("epoch") != self.epoch or result.get(
                    "lease_state"
                ) not in ("reserved", "held"):
                    raise RuntimeError("Desktop lease renewal rejected")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.healthy = False
            log.warning("desktop_lease_renew_failed", lease_id=self.lease_id)
            if self._on_failure is not None:
                self._on_failure()

    async def aclose(self) -> None:
        """Join serialized RPC-only closure; retry an unconfirmed close later.

        Mark closed before waiting on an in-flight lifecycle RPC so a late ACK
        cannot publish a renewal task. Runner release tombstones fence remote
        operations whose effects outlive cancellation of the local RPC.
        """
        self.closed = True
        if self._close_task is None or (self._close_task.done() and self.attempted):
            self._close_task = asyncio.create_task(self._close())
        await cleanup_rpc(self._close_task)

    async def _close(self) -> None:
        task, self._renew_task = self._renew_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lifecycle_lock:
            if self.attempted:
                result = await self._rpc("release", self._args())
                if result.get("lease_state") not in ("released", "expired"):
                    raise RuntimeError("Desktop lease closure unverified")
                self.attempted = False
