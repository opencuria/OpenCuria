"""Independent durable delivery loop shared by deployment and tests."""

from __future__ import annotations

import os
import uuid

import structlog
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.deployments.models import LifecycleRecoveryLease

from ..operations import OperationRepository

logger = structlog.get_logger(__name__)
RECOVERY_LEASE_KEY = 0x4F43555052454356  # "OCPRECV"


class RecoveryService:
    """Single-leased recovery dispatcher; Redis carries transport only."""

    def __init__(self, owner_id: str = "local") -> None:
        self.owner_id = owner_id
        self._lease_token = None
        self._lease_connection = None

    async def start(self) -> None:
        """Hold one PostgreSQL advisory lock and publish generation ownership."""
        from asgiref.sync import sync_to_async
        from django.conf import settings
        from django.db import connection, connections

        if connection.vendor != "postgresql":
            if os.environ.get("OPENCURIA_MANAGED_DEPLOYMENT") == "1":
                raise RuntimeError("Managed lifecycle recovery requires PostgreSQL")
            self._lease_token = uuid.uuid4()
            return
        self._lease_connection = connections["default"].copy()
        # Explicitly delegate this dedicated connection to the pinned
        # thread-sensitive executor for its entire lifetime.
        self._lease_connection.inc_thread_sharing()
        self._lease_connection.close_at = None
        await sync_to_async(
            self._lease_connection.ensure_connection, thread_sensitive=True
        )()
        acquired = await sync_to_async(self._try_lock, thread_sensitive=True)()
        if not acquired:
            await sync_to_async(self._lease_connection.close, thread_sensitive=True)()
            self._lease_connection = None
            raise RuntimeError("Another process owns lifecycle recovery")
        token = uuid.uuid4()
        try:
            await sync_to_async(
                LifecycleRecoveryLease.objects.update_or_create,
                thread_sensitive=True,
            )(
                id=1,
                defaults={
                    "owner_id": self.owner_id,
                    "deployment_version": os.environ.get(
                        "OPENCURIA_DEPLOYMENT_VERSION", "local"
                    ),
                    "owner_token": token,
                    "updated_at": timezone.now(),
                },
            )
            self._lease_token = token
        except BaseException:
            await sync_to_async(self._unlock, thread_sensitive=True)()
            await sync_to_async(self._lease_connection.close, thread_sensitive=True)()
            self._lease_connection = None
            raise

    def _try_lock(self) -> bool:
        """Acquire the PG lock on the dedicated Django connection."""
        with self._lease_connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [RECOVERY_LEASE_KEY])
            return bool(cursor.fetchone()[0])

    def _unlock(self) -> None:
        """Release on the same thread that acquired the session advisory lock."""
        with self._lease_connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [RECOVERY_LEASE_KEY])

    async def stop(self) -> None:
        """Clear readiness metadata and release the exclusive dispatcher lock."""
        if self._lease_connection is None:
            return
        from asgiref.sync import sync_to_async

        try:
            if self._lease_token is not None:
                updated = await sync_to_async(
                    LifecycleRecoveryLease.objects.filter(
                        id=1,
                        owner_id=self.owner_id,
                        owner_token=self._lease_token,
                    ).update,
                    thread_sensitive=True,
                )(
                    owner_id="",
                    deployment_version="",
                    owner_token=None,
                    updated_at=timezone.now(),
                )
                if not updated:
                    raise RuntimeError("Lifecycle recovery readiness lease was lost")
            await sync_to_async(self._unlock, thread_sensitive=True)()
        finally:
            await sync_to_async(self._lease_connection.close, thread_sensitive=True)()
            self._lease_connection = None
            self._lease_token = None

    async def tick(self, transport, *, service=None) -> None:
        """Advance coordinators and deliver due commands independently."""
        if self._lease_token is None and os.environ.get("OPENCURIA_MANAGED_DEPLOYMENT") == "1":
            raise RuntimeError("Lifecycle recovery lease is not held")
        from ..deletion_repository import DeletionRepository

        await sync_to_async(DeletionRepository.tick)()
        from ..capture_repository import CaptureRepository
        from ..recreate_repository import RecreateRepository

        finished = await sync_to_async(CaptureRepository.tick)()
        finished += await sync_to_async(RecreateRepository.tick)()
        if finished:
            if service is None:
                from . import RunnerService

                service = RunnerService(transport)
            for result in finished:
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
                        {"workspace_id": workspace_id, "error": result["diagnostic"]},
                        workspace_id,
                    )
        from ..retention_repository import RetentionRepository

        await sync_to_async(RetentionRepository.tick)()
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
                logger.exception(
                    "lifecycle_delivery_deferred", task_id=str(command.task_id)
                )
