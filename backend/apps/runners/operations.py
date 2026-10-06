"""Durable lifecycle repository. Task.status remains the execution truth."""

import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from common.exceptions import ConflictError

from .locking import lock_runner
from .models import LifecycleCommand, Runner, Task, Workspace

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
}
OPERATIONS = {
    "create_workspace": "creating",
    "create_workspace_from_image_artifact": "creating",
    "stop_workspace": "stopping",
    "resume_workspace": "starting",
    "update_workspace": "restarting",
    "remove_workspace": "removing",
    "create_image_artifact": "capturing_image",
}


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
            from .models import CaptureRequest
            from .recreate_repository import OPERATION_BY_REASON, RecreateRepository

            capture = (
                CaptureRequest.objects.filter(workspace=ws)
                .exclude(phase__in=["completed", "failed"])
                .first()
            )
            if capture and capture.id != capture_request_id:
                raise ConflictError("Workspace is capturing image")
            recreate = RecreateRepository.live(ws.id)
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
                    "dockerfile_content",
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
        revision_id = payload.pop("_revision_id", None)
        if revision_id:
            from .models import ImageRevision

            payload.update(
                ImageRevision.objects.get(pk=revision_id).rendered_input or {}
            )
        if payload.pop("_unreproducible_credentials", False):
            OperationRepository.intervene(
                command.task,
                "Credential inputs not reproducible; intervention required",
            )
            raise ConflictError("Unreproducible credential inputs")
        return payload

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
    def candidates() -> list:
        """Claim due commands with bounded delivery; expired leases may be reclaimed."""
        now = timezone.now()
        with transaction.atomic():
            from django.db.models import Q

            for runner_id in Runner.objects.order_by("id").values_list("id", flat=True):
                lock_runner(runner_id)
            Runner.objects.filter(status="online").filter(
                Q(last_heartbeat_at__lt=now - timedelta(seconds=90))
                | Q(
                    last_heartbeat_at__isnull=True,
                    connected_at__lt=now - timedelta(seconds=90),
                )
            ).update(status="offline")
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
                )[:100]
            )
            claimed = []
            for row in rows:
                from .deletion_repository import DeletionRepository

                if not row.deliveries and not DeletionRepository.delivery_allowed(
                    row.task_id
                ):
                    # An explicitly withheld, never-executed approval is idle,
                    # not an ambiguous runtime deadline failure.
                    row.deadline_at = now + timedelta(hours=2)
                    row.save(update_fields=["deadline_at"])
                    continue
                alive = row.heartbeat_at and row.heartbeat_at > now - timedelta(
                    seconds=90
                )
                if (
                    row.deadline_at <= now
                    or (row.deliveries >= 12 and not alive)
                    or (not row.event and row.created_at < now - timedelta(seconds=60))
                ):
                    OperationRepository.intervene(
                        row.task,
                        "Lifecycle outcome unknown; manual intervention required",
                    )
                    continue
                if not row.event or not row.task.runner.is_online or alive:
                    continue
                from .deletion_repository import DeletionRepository

                if not DeletionRepository.delivery_allowed(row.task_id):
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
    def intervene(task: Task, reason: str) -> None:
        """Fence late results without guessing runtime effects or scrub proof."""
        with transaction.atomic():
            lock_runner(task.runner_id)
            if task.workspace_id:
                Workspace.objects.select_for_update().get(pk=task.workspace_id)
            Task.objects.select_for_update().get(pk=task.id)
            Task.objects.filter(
                pk=task.id, status__in=["pending", "in_progress"]
            ).update(status="failed", error=reason, completed_at=timezone.now())
            LifecycleCommand.objects.filter(task=task).update(phase="intervention")
            # Keep current_task as an intervention fence. Explicit deletion is allowed
            # only after operator resolves this identity, not on GET or heartbeat.
            Workspace.objects.filter(current_task=task).update(active_operation=None)
            if task.type in {
                "create_workspace",
                "create_workspace_from_image_artifact",
            }:
                Workspace.objects.filter(current_task=task).update(status="failed")
            from .models import ImageInstance

            ImageInstance.objects.filter(
                creating_task=task, status__in=["creating", "building", "capturing"]
            ).update(status="failed")
            ImageInstance.objects.filter(deleting_task=task, status="deleting").update(
                status="delete_failed", delete_last_error=reason
            )

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


RESULT_METHODS = {
    "workspace:created": (
        "handle_workspace_created",
        {"create_workspace", "create_workspace_from_image_artifact"},
    ),
    "workspace:stopped": ("handle_workspace_stopped", {"stop_workspace"}),
    "workspace:resumed": ("handle_workspace_resumed", {"resume_workspace"}),
    "workspace:updated": ("handle_workspace_updated", {"update_workspace"}),
    "workspace:removed": ("handle_workspace_removed", {"remove_workspace"}),
    "workspace:error": (
        "handle_workspace_error",
        LIFECYCLE_TYPES - {"build_image", "delete_image", "create_image_artifact"},
    ),
    "image:built": ("handle_image_built", {"build_image"}),
    "image:build_failed": ("handle_image_build_failed", {"build_image"}),
    "image_artifact:created": (
        "handle_image_artifact_created",
        {"create_image_artifact"},
    ),
    "image_artifact:failed": (
        "handle_image_artifact_failed",
        {"create_image_artifact"},
    ),
    "image_artifact:deleted": ("handle_image_artifact_deleted", {"delete_image"}),
    "image_artifact:delete_failed": (
        "handle_image_artifact_delete_failed",
        {"delete_image"},
    ),
}


def apply_result(service, runner_id: str, event: str, data: dict) -> bool:
    """Single transaction around callback + terminal ACK eligibility."""
    import inspect

    if event not in RESULT_METHODS:
        return False
    import uuid

    try:
        uuid.UUID(str(runner_id))
        uuid.UUID(str(data.get("operation_id")))
    except (TypeError, ValueError, AttributeError):
        return False
    with transaction.atomic():
        lock_runner(runner_id)
        identity = (
            Task.objects.filter(id=data.get("operation_id"))
            .values("workspace_id")
            .first()
        )
        if identity and identity["workspace_id"]:
            Workspace.objects.select_for_update().get(pk=identity["workspace_id"])
        from .models import ImageBuildJob, ImageInstance

        list(
            ImageBuildJob.objects.select_for_update().filter(
                build_task_id=data.get("operation_id")
            )
        )
        list(
            ImageInstance.objects.select_for_update().filter(
                Q(creating_task_id=data.get("operation_id"))
                | Q(deleting_task_id=data.get("operation_id"))
            )
        )
        Task.objects.select_for_update().filter(pk=data.get("operation_id")).first()
        try:
            row = (
                LifecycleCommand.objects.select_for_update(of=("self",))
                .select_related("task__workspace")
                .get(task_id=data.get("operation_id"))
            )
        except (LifecycleCommand.DoesNotExist, ValueError):
            return False
        task = Task.objects.select_for_update().get(pk=row.task_id)
        method_name, types = RESULT_METHODS[event]
        if (
            str(task.runner_id) != runner_id
            or task.type not in types
            or data.get("task_id") != str(task.id)
            or data.get("attempt") != row.attempt
            or data.get("target") != row.target
            or data.get("runner_id") != runner_id
        ):
            return False
        if task.workspace_id and data.get(
            "workspace_id", str(task.workspace_id)
        ) != str(task.workspace_id):
            return False
        if (
            event
            in {
                "workspace:error",
                "image:build_failed",
                "image_artifact:failed",
                "image_artifact:delete_failed",
            }
            and data.get("outcome_known") is False
            and (
                task.status not in {"completed", "failed"}
                or row.phase == "intervention"
            )
        ):
            OperationRepository.intervene(
                task, "Unknown outcome; intervention required"
            )
            if task.workspace_id:
                service._forward_workspace_operation(str(task.workspace_id), None)
                service._forward_to_frontend(
                    "workspace:error",
                    {
                        "workspace_id": str(task.workspace_id),
                        "task_id": str(task.id),
                        "error": "Unknown outcome; intervention required",
                    },
                    str(task.workspace_id),
                )
            return False
        if task.status in {"completed", "failed"}:
            if task.status == "failed" and row.phase == "intervention":
                # Backend delivery deadlines are not runner terminal outcomes.
                # Exact authenticated journal completion can still resolve the fence.
                if event in {
                    "workspace:error",
                    "image:build_failed",
                    "image_artifact:failed",
                    "image_artifact:delete_failed",
                }:
                    if not data.get("execution_finished") or not data.get(
                        "outcome_known"
                    ):
                        return False
                if task.workspace_id and task.workspace.current_task_id != task.id:
                    return False
                task.status = "in_progress"
                task.error = ""
                task.completed_at = None
                task.save(update_fields=["status", "error", "completed_at"])
                from .models import ImageInstance

                ImageInstance.objects.filter(
                    creating_task=task, status="failed"
                ).update(
                    status="building" if task.type == "build_image" else "capturing"
                )
                ImageInstance.objects.filter(
                    deleting_task=task, status="delete_failed"
                ).update(status="deleting")
            else:
                return True
        if task.workspace_id:
            ws = Workspace.objects.select_for_update().get(pk=task.workspace_id)
            if ws.current_task_id != task.id:
                return False
        data = {
            **data,
            "artifact_id": data.get("image_artifact_id", ""),
            "runner_ref": data.get("image_artifact_id", ""),
        }
        row.heartbeat_at = timezone.now()
        row.phase = "result"
        row.save(update_fields=["heartbeat_at", "phase"])
        method = getattr(service, method_name)
        kwargs = {
            k: v
            for k, v in {**data, "runner_id": runner_id}.items()
            if k in inspect.signature(method).parameters
        }
        method(**kwargs)
        task.refresh_from_db()
        if task.status not in {"completed", "failed"}:
            return False
        if task.status == "failed" and "intervention" in task.error.lower():
            row.phase = "intervention"
            row.save(update_fields=["phase"])
            if task.workspace_id:
                Workspace.objects.filter(pk=task.workspace_id).update(
                    current_task=task, active_operation=None
                )
            return False
        from .repositories import TaskRepository

        TaskRepository.release_workspace(task)
        return True
