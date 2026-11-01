"""Own pending process and Git waiters and resolve replies safely."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


@dataclass
class PendingGitRequest:
    """In-flight git:operation RPC with its expected trust-boundary fields.

    ``request_id`` correlation alone is not fail-closed: a reply on the
    same runner connection with a wrong ``workspace_id``/``operation``
    must never resolve another request's waiter. The pending entry
    therefore pins the expected workspace UUID string and operation name.
    """

    future: asyncio.Future
    workspace_id: str
    operation: str


class RpcRegistryService:
    """Pending-RPC registries with explicitly supplied collaborators."""

    def __init__(
        self,
    ) -> None:
        """Compose this service without inheriting sibling behavior or state."""
        self.process_pending: dict[str, asyncio.Future] = {}
        self.git_pending: dict[str, PendingGitRequest] = {}

    def owns_request(self, kind: Literal["git", "process"], request_id: str) -> bool:
        """Return whether this registry has a live waiter for a reply."""
        if kind == "git":
            pending = self.git_pending.get(request_id)
            return pending is not None and not pending.future.done()
        if kind == "process":
            future = self.process_pending.get(request_id)
            return future is not None and not future.done()
        return False

    def resolve_process_future(self, request_id: str, payload: dict) -> bool:
        """Resolve the pending process RPC future for *request_id* (sync).

        Thread-safe: Socket.IO replies arrive via ``sync_to_async`` worker
        threads while waiters live on the main event loop.
        """
        future = self.process_pending.get(request_id)
        if future is None or future.done():
            logger.warning("process reply for unknown request_id %s", request_id)
            return False

        def _set() -> None:
            if not future.done():
                future.set_result(payload)

        try:
            loop = future.get_loop()
        except RuntimeError:
            loop = None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and loop.is_running() and running is not loop:
            loop.call_soon_threadsafe(_set)
        else:
            _set()
        return True

    def resolve_git_future(self, request_id: str, payload: dict) -> bool:
        """Resolve the pending git RPC future for *request_id* (sync).

        Thread-safe: Socket.IO replies arrive via ``sync_to_async``
        worker threads while waiters live on the main event loop.

        The caller (:meth:`handle_git_reply`) must have validated the
        pending entry's expected ``workspace_id``/``operation`` and the
        sending runner's ownership first; this helper intentionally logs
        the unknown-request case exactly once. Callers must look the
        pending entry up beforehand and drop unknown replies without
        calling this method a second time (avoids duplicate logs).
        """
        pending = self.git_pending.get(request_id)
        if pending is None:
            logger.warning("git reply for unknown request_id %s", request_id)
            return False
        future = pending.future
        if future.done():
            logger.warning("git reply for unknown request_id %s", request_id)
            return False

        def _set() -> None:
            if not future.done():
                future.set_result(payload)

        try:
            loop = future.get_loop()
        except RuntimeError:
            loop = None
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and loop.is_running() and running is not loop:
            loop.call_soon_threadsafe(_set)
        else:
            _set()
        return True
