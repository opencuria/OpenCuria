"""Runner transport and harness-reply routing (Phase 2 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.sio``,
``self._call_reply_waiter`` / ``self._discard_call_reply_waiter`` (via
``RpcRegistryMixin``), ``self._validate_harness_workspace_runner`` (via
``OwnershipMixin``) and ``self._validate_runner_file_chunk`` (still on
the facade -- Phase 3 candidate). Phase 4: when the facade was
constructed with ``harness_reply_router``, ``_route_harness_reply``
keeps its fail-closed validation but dispatches through the injected
router; otherwise the
``apps.harness.access.runner_accessor`` import stays lazy inside
``_route_harness_reply``. This module has no top-level harness imports.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from ...exceptions import RunnerOfflineError

logger = logging.getLogger(__name__)


class RunnerTransportMixin:
    """Runner Socket.IO transport shared by RunnerService."""

    async def _emit_to_runner(
        self,
        runner: "Runner",
        event: str,
        data: dict,
    ) -> None:
        """Send a Socket.IO event to a specific runner by its SID."""
        if self.sio is None:
            logger.error("No Socket.IO server configured — cannot emit events")
            raise RuntimeError("Socket.IO server is not configured")

        if not runner.sid:
            logger.error(
                "Runner %s has no SID — cannot send event %s",
                runner.id,
                event,
            )
            raise RunnerOfflineError(str(runner.id))

        await self.sio.emit(event, data, to=runner.sid)
        logger.debug("Emitted %s to runner %s: %s", event, runner.id, data)

    async def _call_runner(
        self,
        runner: "Runner",
        event: str,
        data: dict,
        *,
        timeout: int = 15,
    ) -> dict:
        """Send request/response call to a specific runner.

        Runner handlers answer two ways: newer handlers return a
        Socket.IO ACK payload (``sio.call``), older fire-and-forget
        handlers emit ``*_result`` events. Prefer ``sio.call`` and fall
        back to reply-event correlation only when no ACK arrives — but
        only for events with a registered reply waiter (stream control).
        """
        if self.sio is None:
            raise RuntimeError("No Socket.IO server configured")
        if not runner.sid:
            raise RunnerOfflineError(str(runner.id))

        try:
            response = await self.sio.call(
                event, data, to=runner.sid, timeout=timeout
            )
        except SocketIOTimeoutError:
            # No ACK from the runner handler: try reply-event
            # correlation for stream control events, otherwise surface
            # the timeout.
            waiter = self._call_reply_waiter(event, data)
            if waiter is None:
                raise RuntimeError(
                    f"Runner call timed out for event '{event}'"
                )
            request_id, future = waiter
            try:
                try:
                    response = await asyncio.wait_for(future, timeout)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"Runner call timed out for event '{event}'"
                    ) from exc
            finally:
                self._discard_call_reply_waiter(event, request_id)
        if response is None:
            return {}
        if isinstance(response, dict):
            return response
        return {"result": response}

    def _route_harness_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> bool | None:
        """Route harness reply events to the owning accessor.

        Returns None when *event* is not a harness reply, True when the
        payload was routed to an accessor (or dropped after validation),
        in which case callers must not forward it to the frontend.
        """
        harness_events = {
            "harness:exec_chunk",
            "harness:exec_done",
            "harness:exec_wait_result",
            "harness:read_file_result",
            "harness:read_file_chunk",
            "harness:write_file_result",
            "harness:list_result",
            "harness:stat_result",
            "harness:desktop_action_result",
        }
        if event not in harness_events:
            return None
        # Phase-4 port: prefer the injected harness_reply_router.
        reply_router = getattr(self, "_harness_reply_router", None)
        if reply_router is not None:
            return self._route_harness_reply_via_router(
                reply_router, event, data, runner_id=runner_id
            )
        from apps.harness.access.runner_accessor import (
            route_harness_chunk,
            route_harness_done,
            route_harness_file_chunk,
            route_harness_result,
        )

        workspace_id = data.get("workspace_id", "") if isinstance(data, dict) else ""
        # Ownership stays fail-closed for harness chunk replies as well: a
        # runner claim requires a valid, owned workspace_id; missing or
        # malformed ids drop instead of reaching the accessor.
        if runner_id:
            try:
                workspace_id_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s",
                    event,
                    workspace_id,
                )
                return True
            if not self._validate_harness_workspace_runner(
                workspace_id_uuid, runner_id
            ):
                return True
        if event == "harness:read_file_chunk" and not self._validate_runner_file_chunk(
            event, data if isinstance(data, dict) else {}
        ):
            logger.warning(
                "%s rejected: invalid chunk payload for workspace %s",
                event,
                workspace_id,
            )
            return True
        if event == "harness:exec_chunk":
            route_harness_chunk(data)
        elif event == "harness:exec_done":
            route_harness_done(data)
        elif event == "harness:read_file_chunk":
            route_harness_file_chunk(data)
        else:
            route_harness_result(data)
        return True

    def _route_harness_reply_via_router(
        self,
        reply_router,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> bool | None:
        """Route a harness reply through an injected reply router.

        Same fail-closed validation as the lazy ``apps.harness`` path
        (ownership + file-chunk shape); only the final dispatch targets
        the injected router's ``route_harness_chunk`` /
        ``route_harness_done`` / ``route_harness_result`` /
        ``route_harness_file_chunk`` methods.
        """
        workspace_id = data.get("workspace_id", "") if isinstance(data, dict) else ""
        if runner_id:
            try:
                workspace_id_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s",
                    event,
                    workspace_id,
                )
                return True
            if not self._validate_harness_workspace_runner(
                workspace_id_uuid, runner_id
            ):
                return True
        if event == "harness:read_file_chunk" and not self._validate_runner_file_chunk(
            event, data if isinstance(data, dict) else {}
        ):
            logger.warning(
                "%s rejected: invalid chunk payload for workspace %s",
                event,
                workspace_id,
            )
            return True
        if event == "harness:exec_chunk":
            reply_router.route_harness_chunk(data)
        elif event == "harness:exec_done":
            reply_router.route_harness_done(data)
        elif event == "harness:read_file_chunk":
            reply_router.route_harness_file_chunk(data)
        else:
            reply_router.route_harness_result(data)
        return True

    async def emit_harness_event(
        self,
        runner: "Runner",
        event: str,
        payload: dict,
    ) -> None:
        """Emit a harness RPC event to the runner owning a workspace."""
        await self._emit_to_runner(runner, event, payload)

    def handle_harness_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Route a harness reply from a runner to its accessor (sync).

        Called from Socket.IO handlers via ``sync_to_async``. Harness
        operations never touch the frontend event bus.
        """
        self._route_harness_reply(event, data, runner_id=runner_id)
