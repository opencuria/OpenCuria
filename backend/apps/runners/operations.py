"""Durable lifecycle repository. Task.status remains the execution truth."""

import uuid
from collections.abc import Callable
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from common.exceptions import ConflictError

from .image_assignment_repository import ImageAssignmentRepository
from .locking import lock_runner
from .models import LifecycleCommand, Runner, Task, Workspace
from .workspace_operation_repository import (
    OPERATION_BY_REASON,
    WorkspaceOperationRepository,
)

LIFECYCLE_TYPES = {
    "create_workspace",
    "create_workspace_from_image_artifact",
    "stop_workspace",
    "resume_workspace",
    "update_workspace",
    "remove_workspace",
    "build_image",
    "create_image_artifact",
    "delete_image",
    "inject_credentials",
}
OPERATIONS = {
    "create_workspace": "creating",
    "create_workspace_from_image_artifact": "creating",
    "stop_workspace": "stopping",
    "resume_workspace": "starting",
    "update_workspace": "restarting",
    "remove_workspace": "removing",
    "create_image_artifact": "capturing_image",
    "inject_credentials": "synchronizing_credentials",
}
INSPECTION_PHASES = {"intervention", "attention_required"}


class OperationRepository:
    """Lock allocation, sanitized outbox, leases and terminal reconciliation."""

    @staticmethod
    def allocate(
        task: Task,
        *,
        capture_request_id: uuid.UUID | None = None,
        recreate_request_id: uuid.UUID | None = None,
    ) -> None:
        """Allocate inside the task creation transaction."""
        if task.type not in LIFECYCLE_TYPES:
            return
        if task.workspace_id:
            ws = Workspace.objects.select_for_update().get(pk=task.workspace_id)
            if ws.current_task_id and ws.current_task_id != task.id:
                raise ConflictError("Workspace has an unresolved lifecycle operation")
            capture = WorkspaceOperationRepository.live_capture(ws.id)
            if capture and capture.id != capture_request_id:
                raise ConflictError("Workspace is capturing image")
            recreate = WorkspaceOperationRepository.live_recreate(ws.id)
            if recreate and recreate.id != recreate_request_id:
                raise ConflictError("Workspace is being reset")
            ws.current_task_id = task.id
            if capture:
                ws.active_operation = "capturing_image"
            elif recreate:
                ws.active_operation = OPERATION_BY_REASON[recreate.reason]
            else:
                ws.active_operation = OPERATIONS.get(task.type)
            ws.save(update_fields=["current_task", "active_operation"])
        LifecycleCommand.objects.get_or_create(
            task=task,
            defaults={
                "deadline_at": timezone.now() + timedelta(hours=2),
            },
        )

    @staticmethod
    def prepare(task_id: str, event: str, payload: dict) -> LifecycleCommand:
        """Persist reproducible inputs without resolved secret material."""
        with transaction.atomic():
            identity = Task.objects.values("runner_id", "workspace_id").get(pk=task_id)
            lock_runner(identity["runner_id"])
            if identity["workspace_id"]:
                Workspace.objects.select_for_update().get(pk=identity["workspace_id"])
            task = Task.objects.select_for_update().get(pk=task_id)
            if task.status in {"completed", "failed"}:
                raise ConflictError("Operation has already ended")
            if not LifecycleCommand.objects.filter(task=task).exists():
                OperationRepository.allocate(task)
            command = LifecycleCommand.objects.select_for_update().get(task_id=task_id)
            if command.event:
                return command
            clean = {
                k: v
                for k, v in payload.items()
                if k
                not in {
                    "env_vars",
                    "files",
                    "ssh_keys",
                    "init_script",
                }
            }
            # Recipes are rendered from catalog input, not credential material.
            if task.workspace_id and any(
                payload.get(k) for k in ("env_vars", "files", "ssh_keys")
            ):
                attached = task.workspace.credentials.exists()
                if not attached:
                    clean["_unreproducible_credentials"] = True
            if task.type == "build_image":
                from .models import ImageInstance

                image = ImageInstance.objects.get(creating_task=task)
                clean["_revision_id"] = str(image.revision_id)
            command.phase = "delivery"
            command.event = event
            command.payload = clean
            command.target = str(
                payload.get("workspace_id")
                or payload.get("image_instance_id")
                or payload.get("image_artifact_id")
                or task_id
            )
            command.save()
            return command

    @staticmethod
    def delivery_payload(command: LifecycleCommand) -> dict:
        """Rehydrate persisted immutable recipe inputs inside the repository."""
        payload = dict(command.payload)
        payload.pop("_previous_qemu_resources", None)
        payload.pop("_observed_workspace_status", None)
        revision_id = payload.pop("_revision_id", None)
        if revision_id:
            from .models import ImageRevision

            payload.update(
                ImageRevision.objects.get(pk=revision_id).rendered_input or {}
            )
        if payload.pop("_unreproducible_credentials", False):
            OperationRepository.fail_unissued(
                command.task,
                "Credential inputs could not be restored. "
                "Retry with attached credentials.",
            )
            raise ConflictError("Unreproducible credential inputs")
        return payload

    @staticmethod
    def claim_direct_delivery(task_id: uuid.UUID) -> str | None:
        """Lease immediate dispatch before emitting, just like recovery delivery.

        Recording the issue boundary first makes zero deliveries reliable proof
        that an unprepared intent never reached a runner. The lease also prevents
        the worker racing a direct dispatch with another delivery.
        """
        with transaction.atomic():
            runner_id = Task.objects.values_list("runner_id", flat=True).get(pk=task_id)
            runner = lock_runner(runner_id)
            task = Task.objects.select_for_update().get(pk=task_id)
            row = LifecycleCommand.objects.select_for_update().get(task_id=task_id)
            now = timezone.now()
            if (
                task.status not in {"pending", "in_progress"}
                or not row.event
                or not runner.is_online
                or not runner.sid
                or row.lease_until > now
                or row.next_delivery_at > now
            ):
                return None
            row.deliveries += 1
            row.lease_until = now + timedelta(seconds=30)
            row.next_delivery_at = now + timedelta(seconds=min(300, 2**row.deliveries))
            row.save(update_fields=["deliveries", "lease_until", "next_delivery_at"])
            return runner.sid

    @staticmethod
    def envelope(command: LifecycleCommand, payload: dict) -> dict:
        """Bind the immutable execution attempt and exact identity."""
        return {
            **payload,
            "operation_id": str(command.task_id),
            "attempt": command.attempt,
            "target": command.target,
            "runner_id": str(command.task.runner_id),
        }

    @staticmethod
    def list_for_inspection() -> list[LifecycleCommand]:
        """Keep checking unresolved operations, including explicit repair blockers."""
        return list(
            LifecycleCommand.objects.filter(
                phase__in=INSPECTION_PHASES, task__runner__status="online"
            )
            .exclude(task__runner__sid="")
            .select_related("task__runner")
        )

    @staticmethod
    def require_attention(
        runner_id: str, sid: str, identity: dict, diagnostic: str
    ) -> bool:
        """Retain an exact current operation after a proven permanent blocker.

        Return whether the persisted diagnostic changed. Recheck the runner
        session and workspace reservation under the same locks as disposition;
        a delayed observation must never revive a completed or replaced command.
        """
        with transaction.atomic():
            runner = lock_runner(runner_id)
            if runner.sid != sid or not runner.is_online:
                return False
            task = Task.objects.filter(
                pk=identity["operation_id"], runner=runner
            ).first()
            if task is None:
                return False
            if task.workspace_id:
                workspace = Workspace.objects.select_for_update().get(
                    pk=task.workspace_id
                )
                if workspace.current_task_id != task.id:
                    return False
            command = LifecycleCommand.objects.filter(task=task).first()
            if (
                command is None
                or any(
                    identity.get(key) != value
                    for key, value in OperationRepository.envelope(command, {}).items()
                )
                or command.phase in {"disposed", "result"}
                or task.status == "completed"
                or (task.status == "failed" and command.phase not in INSPECTION_PHASES)
                or (command.phase == "attention_required" and task.error == diagnostic)
            ):
                return False
            OperationRepository.intervene(task, diagnostic)
            Task.objects.filter(pk=task.id).update(error=diagnostic)
            LifecycleCommand.objects.filter(task=task).update(
                phase="attention_required"
            )
            return True

    @staticmethod
    def candidates(
        *, delivery_allowed: Callable[[uuid.UUID], bool]
    ) -> list[LifecycleCommand]:
        """Claim due commands with bounded delivery; reclaim expired leases.

        The recovery service supplies domain approval. Evaluate it under the
        runner locks before expiring unissued commands and allocating leases.
        """
        now = timezone.now()
        stale_runners = Runner.objects.filter(status="online").filter(
            Q(last_heartbeat_at__lt=now - timedelta(seconds=90))
            | Q(
                last_heartbeat_at__isnull=True,
                connected_at__lt=now - timedelta(seconds=90),
            )
        )
        due = LifecycleCommand.objects.filter(
            task__status__in=["pending", "in_progress"],
            next_delivery_at__lte=now,
            lease_until__lte=now,
        )
        # Idle polling must not acquire SQLite's single writer reservation.
        # Repeat both predicates under the runner locks when there is work.
        if not due.exists() and not stale_runners.exists():
            return []
        with transaction.atomic():
            for runner_id in Runner.objects.order_by("id").values_list("id", flat=True):
                lock_runner(runner_id)
            stale_runners.update(status="offline")
            # Match allocation/results: runner -> workspace -> image -> task.
            # Graph approval and inventory writes serialize at runner boundary.
            due_ids = LifecycleCommand.objects.filter(
                task__status__in=["pending", "in_progress"],
                next_delivery_at__lte=now,
                lease_until__lte=now,
            ).values_list("task_id", flat=True)
            list(Task.objects.select_for_update().filter(pk__in=due_ids).order_by("id"))
            rows = list(
                LifecycleCommand.objects.select_for_update(of=("self",))
                .select_related("task__runner", "task__workspace")
                .filter(
                    task__status__in=["pending", "in_progress"],
                    next_delivery_at__lte=now,
                    lease_until__lte=now,
                )
                .order_by("next_delivery_at", "task_id")[:100]
            )
            # Rotate every inspected row, including offline, withheld and live
            # work. Otherwise the same first 100 rows can occupy this bounded
            # batch forever and starve unrelated deliverable commands.
            LifecycleCommand.objects.filter(
                task_id__in=[row.task_id for row in rows]
            ).update(next_delivery_at=now)
            claimed = []
            for row in rows:
                if not row.event and row.created_at < now - timedelta(seconds=60):
                    OperationRepository.fail_unissued(
                        row.task,
                        "Operation preparation was interrupted before delivery. "
                        "Retry the action.",
                    )
                    continue
                if not row.deliveries and not delivery_allowed(row.task_id):
                    # An explicitly withheld, never-executed approval is idle,
                    # not an ambiguous runtime deadline failure.
                    row.deadline_at = now + timedelta(hours=2)
                    row.save(update_fields=["deadline_at"])
                    continue
                alive = row.heartbeat_at and row.heartbeat_at > now - timedelta(
                    seconds=90
                )
                if alive:
                    # A queue slot or slow but live execution is not a timeout.
                    row.deadline_at = now + timedelta(hours=2)
                    row.save(update_fields=["deadline_at"])
                    continue
                if not row.deliveries and row.deadline_at <= now:
                    # Offline/withheld work has not executed and remains queued.
                    row.deadline_at = now + timedelta(hours=2)
                    row.save(update_fields=["deadline_at"])
                if row.deadline_at <= now or row.deliveries >= 12:
                    OperationRepository.intervene(
                        row.task,
                        "Connection to the operation was lost. Recovery will "
                        "continue when the runner can verify its state.",
                    )
                    continue
                if not row.event or not row.task.runner.is_online or alive:
                    continue
                if not delivery_allowed(row.task_id):
                    continue
                row.lease_until = now + timedelta(seconds=30)
                row.deliveries += 1
                row.next_delivery_at = now + timedelta(
                    seconds=min(300, 2**row.deliveries)
                )
                row.save(
                    update_fields=["lease_until", "deliveries", "next_delivery_at"]
                )
                claimed.append(row)
            return claimed

    @staticmethod
    def fail_unissued(task: Task, reason: str) -> None:
        """Close local preparation failures only before the durable issue boundary."""
        with transaction.atomic():
            lock_runner(task.runner_id)
            command = LifecycleCommand.objects.select_for_update().get(task=task)
            if command.deliveries:
                OperationRepository.intervene(task, reason)
                return
            fresh_creation = (
                task.type
                in {"create_workspace", "create_workspace_from_image_artifact"}
                and Workspace.objects.filter(
                    pk=task.workspace_id, status="creating"
                ).exists()
            )
            OperationRepository.intervene(task, reason)
            LifecycleCommand.objects.filter(task=task).update(
                phase="disposed",
                payload={
                    **command.payload,
                    "_observed_workspace_status": (
                        "absent" if fresh_creation else "unknown"
                    ),
                },
            )
            if task.type == "remove_workspace":
                Workspace.objects.filter(current_task=task).update(
                    status="delete_failed",
                    delete_last_error=reason,
                    updated_at=timezone.now(),
                )
            WorkspaceOperationRepository.release_workspace(task)

    @staticmethod
    def intervene(task: Task, reason: str) -> None:
        """Fence late results without guessing runtime effects or scrub proof."""
        with transaction.atomic():
            lock_runner(task.runner_id)
            if task.workspace_id:
                Workspace.objects.select_for_update().get(pk=task.workspace_id)
            from .models import ImageBuildJob, ImageInstance

            jobs = list(
                ImageBuildJob.objects.select_for_update().filter(build_task=task)
            )
            list(
                ImageInstance.objects.select_for_update().filter(
                    Q(creating_task=task) | Q(deleting_task=task)
                )
            )
            Task.objects.select_for_update().get(pk=task.id)
            Task.objects.filter(
                pk=task.id, status__in=["pending", "in_progress"]
            ).update(status="failed", error=reason, completed_at=timezone.now())
            LifecycleCommand.objects.filter(task=task).update(phase="intervention")
            # Keep the identity until fresh automatic inspection proves execution
            # has ended. GETs and ordinary heartbeats cannot clear this fence.
            Workspace.objects.filter(current_task=task).update(active_operation=None)
            if task.type in {
                "create_workspace",
                "create_workspace_from_image_artifact",
            }:
                Workspace.objects.filter(current_task=task).update(status="failed")
            ImageInstance.objects.filter(
                creating_task=task, status__in=["creating", "building", "capturing"]
            ).update(status="failed")
            ImageInstance.objects.filter(deleting_task=task, status="deleting").update(
                status="delete_failed", delete_last_error=reason
            )
            for job in jobs:
                ImageAssignmentRepository.reconcile(job)

    @staticmethod
    def validate_result(runner_id: str, data: dict) -> bool:
        """Check operation envelope before calling existing type-specific callbacks."""
        try:
            row = LifecycleCommand.objects.select_related("task__workspace").get(
                task_id=data["operation_id"]
            )
        except (LifecycleCommand.DoesNotExist, KeyError, ValueError):
            return False
        task = row.task
        return (
            str(task.runner_id) == runner_id
            and data.get("task_id") == str(task.id)
            and data.get("attempt") == row.attempt
            and data.get("target") == row.target
            and (not task.workspace_id or task.workspace.current_task_id == task.id)
        )

    @staticmethod
    def record_heartbeat(runner_id: str, data: dict) -> None:
        """Mark the bound, still-open attempt as executing on its runner."""
        if OperationRepository.validate_result(runner_id, data):
            LifecycleCommand.objects.filter(
                task_id=data["task_id"], task__status__in=["pending", "in_progress"]
            ).update(
                heartbeat_at=timezone.now(),
                deadline_at=timezone.now() + timedelta(hours=2),
                phase="executing",
            )
