"""Runner Socket.IO transport and ownership-checked harness reply routing.

Request/response calls use Socket.IO ACKs. Durable lifecycle commands are
delivered from the operation journal, and their heartbeats and results are
accepted only for the bound attempt. Harness reply events are validated
fail-closed and dispatched through the HarnessGateway port.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from ...exceptions import RunnerOfflineError
from ...operations import LIFECYCLE_TYPES, OperationRepository
from ..operation_results import apply_result

if TYPE_CHECKING:
    from ...models import Runner

logger = logging.getLogger(__name__)


class RunnerTransportMixin:
    """Runner Socket.IO transport shared by RunnerService."""

    async def _emit_to_runner(
        self,
        runner: Runner,
        event: str,
        data: dict,
    ) -> None:
        """Send a Socket.IO event to a specific runner by its SID."""
        lifecycle_type = event.removeprefix("task:")
        if lifecycle_type == "delete_image_artifact":
            lifecycle_type = "delete_image"
        if lifecycle_type in LIFECYCLE_TYPES:
            sid, data = await self._prepare_lifecycle_delivery(runner, event, data)
            if not sid:
                return
            try:
                await self.sio.emit(event, data, to=sid)
            except Exception:
                logger.warning("Lifecycle delivery deferred: %s", data["task_id"])
            return
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
        logger.debug("Emitted %s to runner %s", event, runner.id)

    async def _prepare_lifecycle_delivery(
        self, runner: Runner, event: str, data: dict
    ) -> tuple[str | None, dict]:
        """Prepare and claim one durable attempt for either emit or ACK transport."""
        from asgiref.sync import sync_to_async

        command = await sync_to_async(OperationRepository.prepare)(
            data["task_id"], event, data
        )
        durable = await sync_to_async(OperationRepository.delivery_payload)(command)
        durable.update(
            {k: data[k] for k in ("env_vars", "files", "ssh_keys") if k in data}
        )
        envelope = await sync_to_async(OperationRepository.envelope)(command, durable)
        if not runner.sid or self.sio is None:
            return None, envelope
        sid = await sync_to_async(OperationRepository.claim_direct_delivery)(
            command.task_id
        )
        return sid, envelope

    def record_operation_heartbeat(self, runner_id: str, data: dict) -> None:
        """Record that the runner is executing the bound lifecycle attempt."""
        OperationRepository.record_heartbeat(runner_id, data)

    def apply_operation_result(self, runner_id: str, event: str, data: dict) -> bool:
        """Apply a runner's journaled lifecycle result; True when ACK-eligible."""
        return apply_result(self, runner_id, event, data)

    async def _call_runner(
        self,
        runner: Runner,
        event: str,
        data: dict,
        *,
        timeout: int = 15,
    ) -> dict:
        """Call a runner handler and return its Socket.IO acknowledgement."""
        if self.sio is None:
            raise RuntimeError("No Socket.IO server configured")
        if not runner.sid:
            raise RunnerOfflineError(str(runner.id))

        identity = None
        sid = runner.sid
        if event.removeprefix("task:") in LIFECYCLE_TYPES:
            sid, data = await self._prepare_lifecycle_delivery(runner, event, data)
            if not sid:
                return {}
            identity = {
                key: data[key]
                for key in ("operation_id", "attempt", "target", "runner_id")
            }
        try:
            response = await self.sio.call(event, data, to=sid, timeout=timeout)
        except SocketIOTimeoutError as exc:
            raise RuntimeError(f"Runner call timed out for event '{event}'") from exc
        if response is None:
            return {}
        if not isinstance(response, dict):
            return {"result": response}
        if identity is not None:
            from asgiref.sync import sync_to_async

            if not await sync_to_async(self.runners.has_session)(str(runner.id), sid):
                return {}
            response = {**response, "_operation_identity": identity}
        return response

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
            "harness:exec_wait_result",
            "harness:read_file_result",
            "harness:read_file_chunk",
            "harness:write_file_result",
            "harness:list_result",
            "harness:desktop_action_result",
            "workspace:artifact_ensure_result",
        }
        if event not in harness_events:
            return None
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
        harness = self._harness()
        if event == "harness:read_file_chunk":
            harness.route_harness_file_chunk(data)
        else:
            harness.route_harness_result(data)
        return True

    async def emit_harness_event(
        self,
        runner: Runner,
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
