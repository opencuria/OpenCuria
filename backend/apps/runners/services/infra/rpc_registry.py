"""Reply-event correlation for process and git RPCs.

RunnerService owns the pending registries. Socket.IO ACK-based control calls
use the transport directly and need no additional reply registry.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class RpcRegistryMixin:
    """Pending-RPC registries shared by RunnerService."""

    def _resolve_process_future(self, request_id: str, payload: dict) -> bool:
        """Resolve the pending process RPC future for *request_id* (sync).

        Thread-safe: Socket.IO replies arrive via ``sync_to_async`` worker
        threads while waiters live on the main event loop.
        """
        future = self._process_pending.get(request_id)
        if future is None or future.done():
            logger.warning(
                "process reply for unknown request_id %s", request_id
            )
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

    def _resolve_git_future(self, request_id: str, payload: dict) -> bool:
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
        pending = self._git_pending.get(request_id)
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
