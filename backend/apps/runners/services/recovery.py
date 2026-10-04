"""Independent durable delivery loop shared by deployment and tests."""

from asgiref.sync import sync_to_async
import structlog

from ..operations import OperationRepository

logger = structlog.get_logger(__name__)


class RecoveryService:
    """One bad command must not stop delivery of other durable commands."""

    async def tick(self, transport) -> None:
        """Advance durable coordinators and deliver due commands independently."""
        from ..deletion_repository import DeletionRepository

        await sync_to_async(DeletionRepository.tick)()
        from ..capture_repository import CaptureRepository

        finished = await sync_to_async(CaptureRepository.tick)()
        if finished:
            from . import RunnerService

            service = RunnerService(transport)
            for result in finished:
                workspace_id = result["workspace_id"]
                await sync_to_async(service._forward_workspace_operation)(
                    workspace_id, None
                )
                if result["diagnostic"]:
                    await sync_to_async(service._forward_to_frontend)(
                        "workspace:error",
                        {"workspace_id": workspace_id, "error": result["diagnostic"]},
                        workspace_id,
                    )
        from ..inventory_repository import InventoryRepository

        refreshes = await sync_to_async(InventoryRepository.refresh_candidates)()
        for refresh in refreshes:
            try:
                await transport.emit("inventory:refresh", {}, room=refresh.runner.sid)
            except Exception:
                logger.exception(
                    "inventory_refresh_deferred", runner_id=str(refresh.runner_id)
                )
        from ..models import LifecycleCommand

        unresolved = await sync_to_async(list)(
            LifecycleCommand.objects.filter(
                phase="intervention", task__runner__status="online"
            )
            .exclude(task__runner__sid="")
            .select_related("task__runner")
        )
        for row in unresolved:
            try:
                await transport.emit(
                    "operation:inspect_request",
                    OperationRepository.envelope(row, {}),
                    room=row.task.runner.sid,
                )
            except Exception:
                logger.exception(
                    "operation_inspection_deferred", task_id=str(row.task_id)
                )
        commands = await sync_to_async(OperationRepository.candidates)()
        for command in commands:
            try:
                if not await sync_to_async(DeletionRepository.delivery_allowed)(
                    command.task_id
                ):
                    continue
                payload = await sync_to_async(OperationRepository.delivery_payload)(
                    command
                )
                if command.task.workspace_id and command.event in {
                    "task:create_workspace",
                    "task:create_workspace_from_image_artifact",
                    "task:resume_workspace",
                }:
                    from apps.credentials.services import CredentialSvc
                    from . import RunnerService

                    resolved = await sync_to_async(
                        CredentialSvc().resolve_workspace_credentials
                    )(command.task.workspace)
                    payload.update(
                        RunnerService._resolved_credentials_payload(resolved)
                    )
                data = OperationRepository.envelope(command, payload)
                await transport.emit(command.event, data, room=command.task.runner.sid)
            except Exception:
                # Retain lease/backoff: transient credential/DB/transport failures
                # must not irreversibly fail an execution that has never started.
                logger.exception(
                    "lifecycle_delivery_deferred", task_id=str(command.task_id)
                )
