"""Generic byte-stream transport (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces``,
``self._call_pending`` (via :class:`RpcRegistryMixin`), ``_call_runner`` /
``_emit_to_runner`` (via ``RunnerTransportMixin``) and the ownership guard
``_validate_harness_workspace_runner`` (via ``OwnershipMixin``). The single
``RunnerService._STREAM_RESULT_EVENTS`` lookup in ``_is_stream_event``
became ``cls._STREAM_RESULT_EVENTS`` (same frozenset, class name instead
of facade name — no circular import, no behaviour change). The
``apps.harness`` imports stay lazy inside the method bodies (no new
top-level harness coupling). Phase 4: ``handle_stream_reply`` prefers an
injected ``harness_stream_router`` port (``route_stream_output(data)`` /
``route_stream_closed(data)``) when the facade was constructed with one;
otherwise the lazy harness path applies. ``fail_streams_for_runner`` and
``ProcessManagerMixin._fail_workspace_processes`` keep the lazy
``_ACCESSORS_BY_STREAM`` access as documented remainder.
"""

from __future__ import annotations

import logging
import uuid

logger = logging.getLogger(__name__)


class StreamTransportMixin:
    """Generic workspace:stream_* byte-stream transport for RunnerService."""

    # ------------------------------------------------------------------
    # Generic byte streams (workspace:stream_* transport, no plugin/MCP
    # domain knowledge — generic bidirectional process/TCP streams only)
    # ------------------------------------------------------------------

    #: ACK timeout for stream control events (start/input/close).
    _STREAM_CALL_TIMEOUT_SECONDS = 15.0

    #: Max raw bytes per stream chunk in either direction.
    _STREAM_CHUNK_SIZE = 64 * 1024

    _STREAM_RESULT_EVENTS = frozenset(
        {
            "workspace:stream_output",
            "workspace:stream_closed",
        }
    )

    @classmethod
    def _is_stream_event(cls, event: str) -> bool:
        """Return True for runner->backend stream events."""
        return event in cls._STREAM_RESULT_EVENTS

    def _validate_stream_payload(
        self, event: str, data: dict
    ) -> tuple[str, str] | None:
        """Fail-closed validation for stream events (ids + ownership).

        Returns ``(workspace_id, connection_id)`` when the payload is
        well-formed and owned by the sending runner; otherwise logs and
        returns ``None``.  Never raises, never touches the frontend bus.
        """
        if not isinstance(data, dict):
            logger.warning("%s rejected: malformed payload", event)
            return None
        workspace_id = data.get("workspace_id", "")
        connection_id = data.get("connection_id", "")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            logger.warning("%s rejected: missing workspace_id", event)
            return None
        if not isinstance(connection_id, str) or not connection_id.strip():
            logger.warning("%s rejected: missing connection_id", event)
            return None
        try:
            uuid.UUID(workspace_id)
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "%s rejected: invalid workspace_id %s",
                event,
                workspace_id,
            )
            return None
        return workspace_id, connection_id

    def _validate_stream_chunk_payload(self, data: dict) -> bool:
        """Validate one ``workspace:stream_output`` chunk (bounded base64).

        A ``started`` marker (empty data, sent by the runner right after
        ``stream_start`` as the open-ACK) is valid control traffic: it
        carries no bytes and must pass validation so the start waiter
        resolves and the live byte stream still accepts it.
        """
        if bool(data.get("started", False)):
            return True
        stream = data.get("stream", "")
        if stream not in ("stdout", "stderr"):
            return False
        content = data.get("data", "")
        if not isinstance(content, str) or not content:
            return False
        import base64 as _b64

        try:
            clean = "".join(content.split())
            decoded = _b64.b64decode(clean, validate=True)
        except Exception:
            return False
        return 0 < len(decoded) <= self._STREAM_CHUNK_SIZE

    def handle_stream_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> bool:
        """Route a workspace:stream_* reply to its byte stream (sync).

        Called from Socket.IO handlers via ``sync_to_async``.  Validates
        workspace ownership like harness replies, then correlates by
        ``connection_id``.  Stream events never reach the frontend bus.

        Returns ``True`` (accepted) when the payload reached a live byte
        stream; ``False`` for unknown events, malformed/foreign payloads,
        or unknown/mismatched/invalid/closed streams.  The runner treats
        a negative result as a signal to close its side.
        """
        if event not in self._STREAM_RESULT_EVENTS:
            return False
        validated = self._validate_stream_payload(event, data)
        if validated is None:
            return False
        workspace_id, connection_id = validated
        if runner_id:
            try:
                workspace_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError):
                return False
            if not self._validate_harness_workspace_runner(
                workspace_uuid, runner_id
            ):
                logger.warning(
                    "%s rejected: workspace %s does not belong to runner %s",
                    event,
                    workspace_id,
                    runner_id,
                )
                return False
        else:
            try:
                workspace_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError):
                return False
            if self.workspaces.get_runner_id(workspace_uuid) is None:
                logger.warning(
                    "%s rejected: workspace %s not found",
                    event,
                    workspace_id,
                )
                return False
        if event == "workspace:stream_output":
            if not self._validate_stream_chunk_payload(
                data if isinstance(data, dict) else {}
            ):
                logger.warning(
                    "%s rejected: invalid chunk payload for connection %s",
                    event,
                    connection_id,
                )
                return False
        # Reply-awaited ``sio.call`` replacement: resolve the pending
        # control-call future first (stream_start/input/close carry no
        # other reply channel).
        self._resolve_call_reply(event, data)
        # Phase-4 port: prefer the injected harness_stream_router.
        stream_router = getattr(self, "_harness_stream_router", None)
        if stream_router is not None:
            if event == "workspace:stream_output":
                return stream_router.route_stream_output(data)
            return stream_router.route_stream_closed(data)
        from apps.harness.access.runner_accessor import (
            route_stream_closed,
            route_stream_output,
        )

        if event == "workspace:stream_output":
            return route_stream_output(data)
        return route_stream_closed(data)

    async def call_stream_event(
        self,
        runner: "Runner",
        event: str,
        payload: dict,
        timeout: float | None = None,
    ) -> dict:
        """ACKed call for stream control events (start/input/close)."""
        return await self._call_runner(
            runner,
            event,
            payload,
            timeout=int(timeout or self._STREAM_CALL_TIMEOUT_SECONDS),
        )

    def fail_streams_for_runner(self, runner_id: str) -> None:
        """Fail all byte streams owned by a disconnecting runner (sync).

        Phase-4 note: ``_ACCESSORS_BY_STREAM`` is harness-internal state;
        the lazy import stays as fallback (documented remainder — no port
        method, no behaviour change).
        """
        from apps.harness.access import runner_accessor as _accessor_mod

        try:
            claimed = uuid.UUID(str(runner_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "stream fail rejected: invalid runner_id %s", runner_id
            )
            return
        for accessor in list(_accessor_mod._ACCESSORS_BY_STREAM.values()):
            try:
                owner_id = self.workspaces.get_runner_id(
                    uuid.UUID(str(accessor.workspace_id))
                )
            except (ValueError, TypeError, AttributeError):
                continue
            if owner_id == claimed:
                accessor.fail_all_streams(
                    f"runner {runner_id} disconnected"
                )
