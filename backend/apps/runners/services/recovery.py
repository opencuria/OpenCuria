"""Independent durable delivery loop shared by deployment and tests."""

from collections.abc import Callable
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from ..credential_sync_repository import LegacyCredentialSyncRepository
from ..deletion_repository import DeletionRepository
from ..operations import OperationRepository
from .capture import CaptureService
from .image_retention import ImageRetentionService
from .recreate import RecreateService

logger = structlog.get_logger(__name__)


class RecoveryService:
    """One bad command must not stop delivery of other durable commands."""

    async def tick(self, transport) -> None:
        """Advance durable coordinators and deliver due commands independently."""
        failures: list[tuple[str, Exception]] = []

        async def stage(name: str, operation: Callable[..., Any], **kwargs: Any) -> Any:
            try:
                return await sync_to_async(operation)(**kwargs)
            except Exception as error:
                # A broken coordinator must not starve unrelated durable commands.
                # Still fail the tick after making progress so the supervisor
                # never treats a persistent database/coordinator fault as healthy.
                failures.append((name, error))
                logger.exception("lifecycle_recovery_stage_failed", stage=name)
                return []

        await stage("legacy_credentials", LegacyCredentialSyncRepository.fence)
        await stage("deletion", DeletionRepository.tick)
        finished = await stage("capture", CaptureService.tick)
        finished += await stage("recreate", RecreateService.tick)
        if finished:
            from . import RunnerService

            service = RunnerService(transport)
            for result in finished:
                try:
                    workspace_id = result["workspace_id"]
                    if result.get("status"):
                        await sync_to_async(service._forward_to_frontend)(
                            "workspace:status_changed",
                            {"workspace_id": workspace_id, "status": result["status"]},
                            workspace_id,
                        )
                    await sync_to_async(service._forward_workspace_operation)(
                        workspace_id, None
                    )
                    if result["diagnostic"]:
                        await sync_to_async(service._forward_to_frontend)(
                            "workspace:error",
                            {
                                "workspace_id": workspace_id,
                                "error": result["diagnostic"],
                            },
                            workspace_id,
                        )
                except Exception as error:
                    failures.append(("notification", error))
                    logger.exception("lifecycle_recovery_notification_failed")
        await stage("retention", ImageRetentionService.tick)
        from ..inventory_repository import InventoryRepository

        refreshes = await stage("inventory", InventoryRepository.refresh_candidates)
        for refresh in refreshes:
            try:
                await transport.emit("inventory:refresh", {}, room=refresh.runner.sid)
            except Exception:
                logger.exception(
                    "inventory_refresh_deferred", runner_id=str(refresh.runner_id)
                )
        unresolved = await stage("inspection", OperationRepository.list_for_inspection)
        for row in unresolved:
            try:
                await transport.emit(
                    "operation:inspect_request",
                    OperationRepository.envelope(
                        row,
                        {"legacy_credential_sync": True}
                        if row.payload.get("_legacy_credential_sync") is True
                        else {},
                    ),
                    room=row.task.runner.sid,
                )
            except Exception:
                logger.exception(
                    "operation_inspection_deferred", task_id=str(row.task_id)
                )
        commands = await stage(
            "commands",
            OperationRepository.candidates,
            delivery_allowed=DeletionRepository.delivery_allowed,
        )
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
                    "task:inject_credentials",
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
        if failures:
            names = ", ".join(name for name, _ in failures)
            raise RuntimeError(
                f"Lifecycle recovery stages failed: {names}"
            ) from failures[0][1]
