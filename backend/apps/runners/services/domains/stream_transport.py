"""Workspace byte streams for RunnerService.

Control calls use Socket.IO ACKs. Output chunks and EOF notices are validated
against workspace ownership and routed through the HarnessGateway, never to
frontend clients.
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
        """Validate one ``workspace:stream_output`` chunk (bounded base64)."""
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
        harness = self._harness()
        if event == "workspace:stream_output":
            return harness.route_stream_output(data)
        return harness.route_stream_closed(data)

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
        """Fail all byte streams owned by a disconnecting runner (sync)."""
        try:
            claimed = uuid.UUID(str(runner_id))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "stream fail rejected: invalid runner_id %s", runner_id
            )
            return

        def owned_by_runner(workspace_id: str) -> bool:
            try:
                owner_id = self.workspaces.get_runner_id(uuid.UUID(workspace_id))
            except (ValueError, TypeError, AttributeError):
                return False
            return owner_id == claimed

        self._harness().fail_streams(
            owned_by_runner, f"runner {runner_id} disconnected"
        )
