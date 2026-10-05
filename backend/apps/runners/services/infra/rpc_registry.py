"""Reply correlation registries for runner RPCs (Phase 2 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self._call_pending``,
``self._process_pending`` and ``self._git_pending`` (created in the
facade ``__init__``). ``_PendingGitRequest`` stays defined in the facade
module and is only touched via ``self._git_pending`` entries, so this
module imports nothing from the parent package (no import cycles).
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class RpcRegistryMixin:
    """Pending-RPC registries shared by RunnerService."""

    #: Start markers retain legacy correlation; close requires a dedicated,
    #: per-request result and never infers termination from silence or EOF.
    _CALL_REPLY_EVENTS: dict[str, tuple[str, ...]] = {
        "workspace:stream_close": ("workspace:stream_close_result",),
        "workspace:stream_start": (
            "workspace:stream_output",
            "workspace:stream_closed",
        ),
    }

    def _call_reply_key(self, event: str, payload: dict) -> str | None:
        """Return the correlation id for a reply-awaited call event."""
        if event == "workspace:stream_close":
            connection_id = payload.get("connection_id")
            request_id = payload.get("close_request_id")
            if isinstance(connection_id, str) and isinstance(request_id, str):
                if connection_id and request_id:
                    return f"{connection_id}:{request_id}"
            return None
        if event == "workspace:stream_start":
            candidate = payload.get("connection_id", "")
            key = str(candidate or "").strip()
            return key or None
        return None

    def _call_reply_waiter(
        self, event: str, payload: dict
    ) -> tuple[str, asyncio.Future] | None:
        """Register a reply future for *event* (sync-safe creation).

        Register before sending so a fast explicit reply cannot be lost.
        Registration needs the caller's running loop.
        """
        key = self._call_reply_key(event, payload)
        if key is None:
            return None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        future: asyncio.Future = loop.create_future()
        future.expected_workspace_id = payload.get("workspace_id")
        self._call_pending.setdefault(event, {})[key] = future
        return key, future

    def _discard_call_reply_waiter(self, event: str, key: str) -> None:
        """Drop a reply waiter (timeout/cancel/answer path)."""
        pending = self._call_pending.get(event)
        if pending is not None:
            future = pending.pop(key, None)
            if future is not None and not future.done():
                future.cancel()
            if not pending:
                self._call_pending.pop(event, None)

    def _resolve_call_reply(self, event: str, data: dict) -> bool:
        """Resolve a pending call waiter from a reply event (sync-safe).

        Called from Socket.IO reply handlers (``sync_to_async`` worker
        threads) — resolves thread-safely like
        :meth:`_resolve_process_future`.
        """
        if not isinstance(data, dict):
            return False
        for call_event, reply_events in self._CALL_REPLY_EVENTS.items():
            if event not in reply_events:
                continue
            pending = self._call_pending.get(call_event)
            if not pending:
                continue
            raw_key = data.get("connection_id", "")
            key = str(raw_key or "").strip()
            if not key:
                continue
            if call_event == "workspace:stream_close":
                key = self._call_reply_key(call_event, data)
            future = pending.get(key)
            if future is None or future.done():
                continue
            if data.get("workspace_id") != future.expected_workspace_id:
                continue
            if event == "workspace:stream_output" and not bool(
                data.get("started", False)
            ):
                # Payload chunks are data plane traffic, not the
                # open-ACK — only the explicit ``started`` marker (or a
                # failed ``stream_closed``) resolves the start waiter.
                continue
            result: dict = {"ok": True, "connection_id": key}
            if event == "workspace:stream_close_result":
                # Preserve false/missing close evidence verbatim.
                result = dict(data)
            if event == "workspace:stream_closed":
                if data.get("error"):
                    result = {
                        "ok": False,
                        "connection_id": key,
                        "error": str(data.get("error")),
                    }
                else:
                    # Plain EOF/close notice without output first is not
                    # a start-ACK (late close of an older stream); only
                    # resolve when it carries an explicit error.
                    continue

            def _set() -> None:
                if not future.done():
                    future.set_result(result)

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
        return False

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
