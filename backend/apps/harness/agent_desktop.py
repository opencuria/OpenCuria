"""One desktop claim per agent run, shared by every managed desktop consumer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import anyio

from .computer_use.recordings import is_valid_recording_path
from .desktop_leases import DesktopLease, cleanup_rpc


class DesktopBusyError(RuntimeError):
    """A different agent currently owns desktop input."""


class AgentDesktop:
    """Serialize activation, input and stop without owning the agent's processes.

    One durable lease owns all desktop MCP streams throughout the run. Stop
    releases only activation, with monotonic revisions fencing delayed actions.
    Final release/expiry closes those streams before dropping protective ownership.
    """

    def __init__(
        self,
        accessor: Any,
        owner_id: str,
        on_failure: Callable[[], None] | None = None,
        *,
        kind: str = "computeruse",
    ) -> None:
        self.accessor = accessor
        self.owner_id = owner_id
        self.kind = kind
        self._on_failure = on_failure
        self.lease: DesktopLease | None = None
        self.binding: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._closed = False
        self.before_stop: list[Callable[[], Awaitable[None]]] = []
        self.recording_paths: list[str] = []
        self._recording_path = ""
        self._recording_confirmed = False

    @property
    def environment(self) -> dict[str, str]:
        """Address the runner binding even while its display is inactive."""
        return {
            "DISPLAY": self.binding["display"],
            "XAUTHORITY": self.binding["xauthority"],
        }

    async def prepare(self) -> None:
        """Reserve a binding without starting the desktop."""
        async with self._lock:
            await self._reserve()

    async def _reserve(self) -> None:
        if self._closed:
            raise RuntimeError("Agent desktop is closed")
        if self.lease is not None:
            self.lease.check_health()
            return
        lease = DesktopLease(
            self.accessor,
            kind=self.kind,
            owner_id=self.owner_id,
            on_failure=self._on_failure,
        )
        self.lease = lease
        binding = await lease.reserve()
        if binding.get("protocol_version", 0) < 2:
            await lease.aclose()
            raise RuntimeError("Agent desktop requires an updated runner (protocol 2)")
        self.binding = dict(binding)

    async def _start(self) -> None:
        await self._reserve()
        assert self.lease is not None
        # Revalidate readiness even if this run already holds the display.
        await self.lease.hold(refresh=True)
        self.binding.update(self.lease.binding)

    async def _status(self) -> dict[str, Any]:
        if self._closed or self.lease is None:
            raise RuntimeError("Agent desktop is closed")
        result = await self.accessor.desktop_action(
            "lease_status", {**self.lease._args(), "probe": True}
        )
        if not result.get("ok") or result.get("epoch") != self.binding["epoch"]:
            raise RuntimeError("Agent desktop status is unavailable or stale")
        protective = bool(result.get("lease_activated"))
        own = protective and bool(result.get("lease_live"))
        count = int(result.get("holder_count", 0))
        active = bool(result.get("active"))
        return {
            "active": active,
            "own_claim": own,
            "own_claim_state": result["lease_state"],
            "other_claim_count": max(0, count - int(protective)),
            "held_by_others": count > int(protective),
            "needs_start": not (own and result["lease_state"] == "held" and active),
            **self.environment,
        }

    async def action(self, action: str) -> dict[str, Any]:
        """Start, stop or observe this run's claim under the shared operation lock."""
        async with self._lock:
            if self._closed:
                raise RuntimeError("Agent desktop is closed")
            if action == "start":
                await self._start()
            elif action == "stop":
                await self._stop()
            elif action != "status":
                raise ValueError("Unknown desktop action")
            return await self._status()

    @asynccontextmanager
    async def operation(self, *, exclusive_input: bool) -> AsyncIterator[None]:
        """Keep stop and same-agent desktop calls out of an in-flight operation."""
        async with self._lock:
            await self._start()
            assert self.lease is not None
            lease = self.lease
            owns_input = False
            try:
                if exclusive_input:
                    try:
                        result = await self.accessor.desktop_action(
                            "input_acquire", lease._args()
                        )
                    except BaseException:
                        # An uncertain acquire may arrive after cancellation.
                        # Ending the owner fences it, unlike a best-effort unlock.
                        await cleanup_rpc(lease.aclose())
                        raise
                    if not result.get("ok"):
                        if not result.get("busy"):
                            await cleanup_rpc(lease.aclose())
                            raise RuntimeError("Desktop input acquisition was rejected")
                        raise DesktopBusyError("Workspace desktop is busy; retry later")
                    owns_input = True
                    self.binding.update(result)
                yield
            finally:
                if owns_input and not lease.closed:
                    try:
                        released = await cleanup_rpc(
                            self.accessor.desktop_action("input_release", lease._args())
                        )
                        if not released.get("ok"):
                            raise RuntimeError("Desktop input release is unconfirmed")
                    except BaseException:
                        await cleanup_rpc(lease.aclose())
                        raise

    async def record(self) -> None:
        """Start one recording segment while the caller holds the operation lock."""
        if self._recording_path:
            if not self._recording_confirmed:
                raise RuntimeError(
                    "Recording start is unconfirmed; stop before retrying"
                )
            return
        assert self.lease is not None
        lease = self.lease
        self._recording_path = (
            f"/workspace/.opencuria/computeruse/{self.owner_id}/{uuid4()}.mp4"
        )
        result = await self.accessor.desktop_action(
            "record_start",
            {
                **lease.owner,
                "run_id": self.owner_id,
                "path": self._recording_path,
            },
        )
        path = result.get("path", "")
        if (
            not result.get("ok")
            or not is_valid_recording_path(path)
            or path != self._recording_path
        ):
            raise RuntimeError("Invalid recording result returned by runner")
        self._recording_confirmed = True

    async def _finish_recording(self) -> None:
        if not self._recording_path:
            return
        assert self.lease is not None
        result = await self.accessor.desktop_action(
            "record_stop", {**self.lease.owner, "run_id": self.owner_id}
        )
        if not result.get("ok"):
            raise RuntimeError("Desktop recording finalization is unconfirmed")
        if self._recording_confirmed:
            self.recording_paths.append(self._recording_path)
        self._recording_path = ""
        self._recording_confirmed = False

    async def _stop(self) -> None:
        # RPC-only cleanup is shielded; an interrupted stop must still fence
        # delayed activation. Do not move SDK transport teardown to another task.
        with anyio.CancelScope(shield=True):
            for callback in self.before_stop:
                await callback()
            await self._finish_recording()
            if self.lease is not None:
                await self.lease.unhold()

    async def finish(self) -> list[str]:
        """Finalize recording without releasing the display before MCP teardown."""
        async with self._lock:
            await self._finish_recording()
            return list(self.recording_paths)

    async def aclose(self) -> None:
        """Fence queued calls, then release only this run's claim."""
        self._closed = True
        with anyio.CancelScope(shield=True):
            async with self._lock:
                try:
                    await self._finish_recording()
                finally:
                    if self.lease is not None:
                        await self.lease.aclose()
