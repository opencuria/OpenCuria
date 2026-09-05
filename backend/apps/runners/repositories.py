"""
Repository layer for the runners app.

Encapsulates all database queries. Services never use the ORM directly —
they call repository methods instead. This keeps business logic decoupled
from data access and makes services easy to test with mock repositories.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .enums import (
    RunnerStatus,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from .models import (
    ImageDefinition,
    ImageInstance,
    Runner,
    ImageBuildJob,
    RunnerSystemMetrics,
    Task,
    Workspace,
)

# ---------------------------------------------------------------------------
# Runner Repository
# ---------------------------------------------------------------------------


class RunnerRepository:
    """Data access for Runner records."""

    @staticmethod
    def get_by_id(runner_id: uuid.UUID) -> Runner | None:
        """Fetch a runner by its ID, or None if not found."""
        return Runner.objects.filter(id=runner_id).first()

    @staticmethod
    def get_by_token_hash(token_hash: str) -> Runner | None:
        """Fetch a runner by its hashed API token."""
        return Runner.objects.filter(api_token_hash=token_hash).first()

    @staticmethod
    def list_all() -> QuerySet[Runner]:
        """Return all runners ordered by creation date."""
        return Runner.objects.all()

    @staticmethod
    def list_online() -> QuerySet[Runner]:
        """Return all online runners."""
        return Runner.objects.filter(status=RunnerStatus.ONLINE)

    @staticmethod
    def create(
        *,
        name: str = "",
        api_token_hash: str,
        organization=None,
    ) -> Runner:
        """Create a new runner record."""
        return Runner.objects.create(
            name=name,
            api_token_hash=api_token_hash,
            organization=organization,
        )

    @staticmethod
    def set_online(
        runner: Runner,
        *,
        sid: str,
        available_runtimes: list[str] | None = None,
    ) -> Runner:
        """Mark a runner as online with its Socket.IO session ID."""
        runner.status = RunnerStatus.ONLINE
        runner.sid = sid
        if available_runtimes is not None:
            runner.available_runtimes = available_runtimes
        runner.connected_at = timezone.now()
        runner.disconnected_at = None
        runner.save(
            update_fields=[
                "status", "sid", "available_runtimes",
                "connected_at", "disconnected_at", "updated_at",
            ]
        )
        return runner

    @staticmethod
    def set_offline(runner: Runner) -> Runner:
        """Mark a runner as offline."""
        runner.status = RunnerStatus.OFFLINE
        runner.sid = ""
        runner.disconnected_at = timezone.now()
        runner.save(
            update_fields=["status", "sid", "disconnected_at", "updated_at"]
        )
        return runner

    @staticmethod
    def list_by_organization(organization_id: uuid.UUID) -> QuerySet[Runner]:
        """Return all runners for a specific organization."""
        return Runner.objects.filter(organization_id=organization_id)

    @staticmethod
    def update_heartbeat(runner: Runner) -> Runner:
        """Update the last heartbeat timestamp for a runner."""
        runner.last_heartbeat_at = timezone.now()
        runner.save(update_fields=["last_heartbeat_at", "updated_at"])
        return runner

    @staticmethod
    def update_qemu_settings(runner: Runner, **fields) -> Runner:
        """Update QEMU resource settings on a runner."""
        for key, value in fields.items():
            setattr(runner, key, value)
        runner.save(update_fields=[*fields.keys(), "updated_at"])
        return runner


class RunnerSystemMetricsRepository:
    """Data access for RunnerSystemMetrics records."""

    @staticmethod
    def create(
        *,
        runner: Runner,
        timestamp,
        cpu_usage_percent: float,
        ram_used_bytes: int,
        ram_total_bytes: int,
        disk_used_bytes: int,
        disk_total_bytes: int,
        vm_metrics: dict[str, Any] | None = None,
    ) -> RunnerSystemMetrics:
        """Persist a new system metrics snapshot."""
        return RunnerSystemMetrics.objects.create(
            runner=runner,
            timestamp=timestamp,
            cpu_usage_percent=cpu_usage_percent,
            ram_used_bytes=ram_used_bytes,
            ram_total_bytes=ram_total_bytes,
            disk_used_bytes=disk_used_bytes,
            disk_total_bytes=disk_total_bytes,
            vm_metrics=vm_metrics,
        )

    @staticmethod
    def get_latest(runner_id: uuid.UUID) -> RunnerSystemMetrics | None:
        """Return the most recent metrics snapshot for the given runner."""
        return (
            RunnerSystemMetrics.objects.filter(runner_id=runner_id)
            .order_by("-timestamp")
            .first()
        )

    @staticmethod
    def get_history(runner_id: uuid.UUID, since: datetime) -> QuerySet[RunnerSystemMetrics]:
        """Return all metrics since a given timestamp."""
        return (
            RunnerSystemMetrics.objects.filter(
                runner_id=runner_id,
                timestamp__gte=since,
            )
            .order_by("timestamp")
        )

    @staticmethod
    def purge_old(runner_id: uuid.UUID, keep_hours: int = 24) -> int:
        """Delete metrics older than *keep_hours* hours. Returns count deleted."""
        from django.utils import timezone as tz
        from datetime import timedelta

        cutoff = tz.now() - timedelta(hours=keep_hours)
        deleted, _ = RunnerSystemMetrics.objects.filter(
            runner_id=runner_id, timestamp__lt=cutoff
        ).delete()
        return deleted


# ---------------------------------------------------------------------------
# Workspace Repository
# ---------------------------------------------------------------------------

def _active_harness_exists():
    """Return an Exists() annotation for busy harness sessions."""
    from django.apps import apps as django_apps

    HarnessSession = django_apps.get_model("harness", "HarnessSession")
    return Exists(
        HarnessSession.objects.filter(workspace=OuterRef("pk"), status="busy")
    )


class WorkspaceRepository:
    """Data access for Workspace records."""

    @staticmethod
    def get_by_id(workspace_id: uuid.UUID) -> Workspace | None:
        """Fetch a workspace by its ID."""
        return (
            Workspace.objects.filter(id=workspace_id)
            .select_related(
                "runner",
                "runner__organization",
                "created_by",
                "base_image_instance",
                "base_image_instance__origin_definition",
            )
            .prefetch_related("credentials__service")
            .annotate(has_active_harness_session=_active_harness_exists())
            .first()
        )

    @staticmethod
    def get_runner_id(workspace_id: uuid.UUID) -> uuid.UUID | None:
        """Return the owning runner id, or None when the workspace is missing.

        Lightweight lookup for harness reply authorization — avoids the
        related-object graph loaded by :meth:`get_by_id`.
        """
        return (
            Workspace.objects.filter(id=workspace_id)
            .values_list("runner_id", flat=True)
            .first()
        )

    @staticmethod
    def list_all() -> QuerySet[Workspace]:
        """Return all workspaces."""
        return (
            Workspace.objects.select_related(
                "runner",
                "runner__organization",
                "base_image_instance",
                "base_image_instance__origin_definition",
            )
            .prefetch_related("credentials__service")
            .annotate(has_active_harness_session=_active_harness_exists())
        )

    @staticmethod
    def list_by_runner(runner_id: uuid.UUID) -> QuerySet[Workspace]:
        """Return all workspaces for a specific runner."""
        return (
            Workspace.objects.filter(runner_id=runner_id)
            .select_related(
                "runner",
                "runner__organization",
                "base_image_instance",
                "base_image_instance__origin_definition",
            )
            .prefetch_related("credentials__service")
            .annotate(has_active_harness_session=_active_harness_exists())
        )

    @staticmethod
    def create(
        *,
        workspace_id: uuid.UUID,
        runner: Runner,
        name: str,
        runtime_type: str = "docker",
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        base_image_instance=None,
        created_by=None,
    ) -> Workspace:
        """Create a new workspace record."""
        workspace = Workspace.objects.create(
            id=workspace_id,
            runner=runner,
            name=name,
            runtime_type=runtime_type,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            base_image_instance=base_image_instance,
            status=WorkspaceStatus.CREATING,
            active_operation=WorkspaceOperation.CREATING,
            created_by=created_by,
        )
        workspace.last_activity_at = workspace.created_at
        workspace.save(update_fields=["last_activity_at"])
        return workspace

    @staticmethod
    def set_credentials(workspace: Workspace, credentials: list) -> Workspace:
        """Replace the credentials attached to a workspace."""
        workspace.credentials.set(credentials)
        workspace.save(update_fields=["updated_at"])
        return workspace

    @staticmethod
    def touch_activity(
        workspace: Workspace,
        *,
        at=None,
    ) -> Workspace:
        """Update the workspace activity timestamp without changing its status."""
        activity_at = at or timezone.now()
        workspace.last_activity_at = activity_at
        workspace.updated_at = activity_at
        workspace.save(update_fields=["last_activity_at", "updated_at"])
        return workspace

    @staticmethod
    def list_by_organization(organization_id: uuid.UUID) -> QuerySet[Workspace]:
        """Return all workspaces for runners in a specific organization."""
        return (
            Workspace.objects.filter(runner__organization_id=organization_id)
            .select_related(
                "runner",
                "runner__organization",
                "base_image_instance",
                "base_image_instance__origin_definition",
            )
            .prefetch_related("credentials__service")
            .annotate(has_active_harness_session=_active_harness_exists())
        )

    @staticmethod
    def list_by_user(user_id: int) -> QuerySet[Workspace]:
        """Return all workspaces created by a specific user."""
        return (
            Workspace.objects.filter(created_by_id=user_id)
            .select_related(
                "runner",
                "runner__organization",
                "base_image_instance",
                "base_image_instance__origin_definition",
            )
            .prefetch_related("credentials__service")
            .annotate(has_active_harness_session=_active_harness_exists())
        )

    @staticmethod
    def update_status(
        workspace: Workspace,
        status: WorkspaceStatus,
    ) -> Workspace:
        """Update a workspace's status."""
        workspace.status = status
        workspace.save(update_fields=["status", "updated_at"])
        return workspace

    @staticmethod
    def update_credentials_present(
        workspace: Workspace,
        credentials_present: bool,
    ) -> Workspace:
        """Persist whether credential material is currently on workspace disk."""
        workspace.credentials_present = credentials_present
        workspace.save(update_fields=["credentials_present", "updated_at"])
        return workspace

    @staticmethod
    def update_active_operation(
        workspace: Workspace,
        active_operation: WorkspaceOperation | None,
    ) -> Workspace:
        """Update the currently active blocking operation for a workspace."""
        workspace.active_operation = active_operation
        workspace.save(update_fields=["active_operation", "updated_at"])
        return workspace

    @staticmethod
    def update_name(workspace: Workspace, name: str) -> Workspace:
        """Update a workspace's name."""
        workspace.name = name
        workspace.save(update_fields=["name", "updated_at"])
        return workspace

    @staticmethod
    def update_qemu_resources(
        workspace: Workspace,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> Workspace:
        """Persist QEMU workspace resource settings."""
        workspace.qemu_vcpus = qemu_vcpus
        workspace.qemu_memory_mb = qemu_memory_mb
        workspace.qemu_disk_size_gb = qemu_disk_size_gb
        workspace.save(update_fields=["qemu_vcpus", "qemu_memory_mb", "qemu_disk_size_gb", "updated_at"])
        return workspace

    @staticmethod
    def list_running_qemu_by_runner(runner_id: uuid.UUID) -> QuerySet[Workspace]:
        """Return active QEMU workspaces for a runner."""
        return Workspace.objects.filter(
            runner_id=runner_id,
            runtime_type="qemu",
            status=WorkspaceStatus.RUNNING,
        )

    @staticmethod
    def list_by_base_image_instance(
        image_instance_id: uuid.UUID,
    ) -> QuerySet[Workspace]:
        """Return workspaces that still depend on an image instance."""
        return Workspace.objects.filter(base_image_instance_id=image_instance_id).exclude(
            status__in=[
                WorkspaceStatus.PENDING_DELETION,
                WorkspaceStatus.DELETING,
                WorkspaceStatus.REMOVED,
                WorkspaceStatus.DELETED,
            ]
        )

    @staticmethod
    def mark_pending_deletion(workspace_id: uuid.UUID) -> None:
        """Mark workspace as pending deletion (runner offline)."""
        requested_at = timezone.now()
        Workspace.objects.filter(id=workspace_id).update(
            status=WorkspaceStatus.PENDING_DELETION,
            active_operation=None,
            delete_requested_at=Coalesce("delete_requested_at", Value(requested_at)),
            delete_last_error="",
        )

    @staticmethod
    def mark_deleting(workspace_id: uuid.UUID) -> None:
        """Mark workspace as actively being deleted by runner."""
        now = timezone.now()
        Workspace.objects.filter(id=workspace_id).update(
            status=WorkspaceStatus.DELETING,
            active_operation=None,
            delete_requested_at=Coalesce("delete_requested_at", Value(now)),
            delete_started_at=now,
            delete_last_error="",
            delete_attempt_count=F("delete_attempt_count") + 1,
        )

    @staticmethod
    def mark_deleted(workspace_id: uuid.UUID) -> None:
        """Mark workspace as fully deleted after runner confirmation."""
        Workspace.objects.filter(id=workspace_id).update(
            status=WorkspaceStatus.DELETED,
            active_operation=None,
            delete_confirmed_at=timezone.now(),
        )

    @staticmethod
    def mark_delete_failed(workspace_id: uuid.UUID, *, error: str = "") -> None:
        """Mark workspace deletion as failed."""
        Workspace.objects.filter(id=workspace_id).update(
            status=WorkspaceStatus.DELETE_FAILED,
            active_operation=None,
            delete_last_error=error,
        )


# ---------------------------------------------------------------------------
class TaskRepository:
    """Data access for Task records."""

    @staticmethod
    def get_by_id(task_id: uuid.UUID) -> Task | None:
        """Fetch a task by its ID."""
        return (
            Task.objects.filter(id=task_id)
            .select_related("runner", "workspace")
            .first()
        )

    @staticmethod
    def create(
        *,
        task_id: uuid.UUID,
        runner: Runner,
        task_type: TaskType,
        workspace: Workspace | None = None,
    ) -> Task:
        """Create a new task record."""
        return Task.objects.create(
            id=task_id,
            runner=runner,
            workspace=workspace,
            type=task_type,
            status=TaskStatus.PENDING,
        )

    @staticmethod
    def mark_in_progress(task: Task) -> Task:
        """Mark a task as in progress."""
        task.status = TaskStatus.IN_PROGRESS
        task.save(update_fields=["status"])
        return task

    @staticmethod
    def complete(task: Task) -> Task:
        """Mark a task as completed."""
        task.status = TaskStatus.COMPLETED
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "completed_at"])
        return task

    @staticmethod
    def fail(task: Task, error: str) -> Task:
        """Mark a task as failed with an error message."""
        task.status = TaskStatus.FAILED
        task.error = error
        task.completed_at = timezone.now()
        task.save(update_fields=["status", "error", "completed_at"])
        return task


# ---------------------------------------------------------------------------
class ImageInstanceRepository:
    """Data access for ImageInstance records."""

    @staticmethod
    def create(
        *,
        runner: Runner,
        runtime_type: str,
        origin_type: str,
        runner_ref: str,
        name: str,
        size_bytes: int = 0,
        origin_definition: ImageDefinition | None = None,
        origin_workspace: Workspace | None = None,
        build_job: ImageBuildJob | None = None,
        created_by=None,
        credentials: list | None = None,
    ) -> "ImageInstance":
        """Create a new image instance record (immediately ready)."""
        image = ImageInstance.objects.create(
            runner=runner,
            runtime_type=runtime_type,
            origin_type=origin_type,
            origin_definition=origin_definition,
            origin_workspace=origin_workspace,
            runner_ref=runner_ref,
            name=name,
            size_bytes=size_bytes,
            status=ImageInstance.Status.READY,
            build_job=build_job,
            created_by=created_by,
        )
        if credentials:
            image.credentials.set(credentials)
        return image

    @staticmethod
    def create_pending(
        *,
        runner: Runner,
        runtime_type: str,
        origin_type: str,
        name: str,
        creating_task_id: str,
        origin_definition: ImageDefinition | None = None,
        origin_workspace: Workspace | None = None,
        build_job: ImageBuildJob | None = None,
        created_by=None,
        credentials: list | None = None,
    ) -> "ImageInstance":
        """Create an image instance before capture/build finishes."""
        status = (
            ImageInstance.Status.BUILDING
            if origin_type == ImageInstance.OriginType.DEFINITION_BUILD
            else ImageInstance.Status.CAPTURING
        )
        image = ImageInstance.objects.create(
            runner=runner,
            runtime_type=runtime_type,
            origin_type=origin_type,
            origin_definition=origin_definition,
            origin_workspace=origin_workspace,
            runner_ref="",
            name=name,
            size_bytes=0,
            build_job=build_job,
            created_by=created_by,
            status=status,
            creating_task_id=creating_task_id,
        )
        if credentials:
            image.credentials.set(credentials)
        return image

    @staticmethod
    def get_by_task_id(task_id: str) -> "ImageInstance | None":
        """Find the image instance associated with a create or delete task."""
        return (
            ImageInstance.objects.filter(
                Q(creating_task_id=task_id) | Q(deleting_task_id=task_id)
            )
            .select_related(
                "runner",
                "origin_workspace",
                "origin_workspace__runner",
                "created_by",
                "origin_definition",
                "build_job",
                "build_job__runner",
                "build_job__image_definition",
            )
            .first()
        )

    @staticmethod
    def mark_ready(
        image_id, *, runner_ref: str, size_bytes: int
    ) -> None:
        """Update a creating image instance to ready once the runner reports success."""
        ImageInstance.objects.filter(id=image_id).update(
            status=ImageInstance.Status.READY,
            runner_ref=runner_ref,
            size_bytes=size_bytes,
            creating_task_id=None,
        )

    @staticmethod
    def mark_failed(image_id) -> None:
        """Mark an image instance as failed."""
        ImageInstance.objects.filter(id=image_id).update(
            status=ImageInstance.Status.FAILED,
        )

    @staticmethod
    def mark_failed_by_task_id(task_id: str) -> None:
        """Mark any creating image instance associated with task_id as failed."""
        ImageInstance.objects.filter(
            creating_task_id=task_id,
            status__in=[ImageInstance.Status.BUILDING, ImageInstance.Status.CAPTURING],
        ).update(status=ImageInstance.Status.FAILED)

    @staticmethod
    def timeout_stale(*, timeout_hours: int = 1) -> int:
        """Mark stale creating image instances as failed.

        Returns the number of image instances that were timed out.
        """
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(hours=timeout_hours)
        count = ImageInstance.objects.filter(
            status__in=[ImageInstance.Status.BUILDING, ImageInstance.Status.CAPTURING],
            created_at__lt=cutoff,
        ).update(status=ImageInstance.Status.FAILED)
        return count

    @staticmethod
    def update_name(image_id: uuid.UUID, name: str) -> bool:
        """Rename an image instance. Returns True if updated."""
        count = ImageInstance.objects.filter(id=image_id).update(name=name)
        return count > 0

    @staticmethod
    def get_by_id(image_id: uuid.UUID) -> "ImageInstance | None":
        """Fetch an image instance by ID, including source and runner info."""
        return (
            ImageInstance.objects.filter(id=image_id)
            .select_related(
                "runner",
                "origin_workspace",
                "origin_workspace__runner",
                "created_by",
                "origin_definition",
                "build_job",
                "build_job__runner",
                "build_job__image_definition",
            )
            .first()
        )

    @staticmethod
    def get_by_build_job_id(
        build_job_id: uuid.UUID,
    ) -> "ImageInstance | None":
        """Fetch a built image instance by its runner build relation."""
        return (
            ImageInstance.objects.filter(build_job_id=build_job_id)
            .select_related(
                "runner",
                "origin_workspace",
                "origin_workspace__runner",
                "created_by",
                "origin_definition",
                "build_job",
                "build_job__runner",
                "build_job__image_definition",
            )
            .first()
        )

    @staticmethod
    def list_by_workspace(workspace_id: uuid.UUID) -> "QuerySet[ImageInstance]":
        """Return all image instances captured from a workspace."""
        return ImageInstance.objects.filter(
            origin_workspace_id=workspace_id
        ).exclude(
            status=ImageInstance.Status.DELETED
        ).select_related(
            "runner",
            "origin_workspace",
            "origin_workspace__runner",
            "created_by",
            "origin_definition",
            "build_job",
            "build_job__runner",
            "build_job__image_definition",
        )

    @staticmethod
    def list_by_user(user) -> "QuerySet[ImageInstance]":
        """Return all visible image instances created by a specific user."""
        return ImageInstance.objects.filter(
            created_by=user
        ).exclude(
            status=ImageInstance.Status.DELETED
        ).select_related(
            "runner",
            "origin_workspace",
            "origin_workspace__runner",
            "created_by",
            "origin_definition",
            "build_job",
            "build_job__runner",
            "build_job__image_definition",
        )

    @staticmethod
    def mark_retired(image_id: uuid.UUID) -> None:
        """Mark an image instance retired so it cannot be used for new workspaces."""
        ImageInstance.objects.filter(id=image_id).exclude(
            status=ImageInstance.Status.DELETED
        ).update(status=ImageInstance.Status.RETIRED)

    @staticmethod
    def mark_ready_from_retired(image_id: uuid.UUID) -> None:
        """Mark a retired image instance ready again without changing its ref."""
        ImageInstance.objects.filter(
            id=image_id,
            status=ImageInstance.Status.RETIRED,
        ).update(status=ImageInstance.Status.READY)

    @staticmethod
    def mark_deleting(image_id: uuid.UUID, *, deleting_task_id: str | None) -> None:
        """Mark an image instance as pending deletion."""
        now = timezone.now()
        ImageInstance.objects.filter(id=image_id).update(
            status=ImageInstance.Status.DELETING,
            deleting_task_id=deleting_task_id,
            delete_requested_at=Coalesce("delete_requested_at", Value(now)),
            delete_started_at=now,
            delete_last_error="",
            delete_attempt_count=F("delete_attempt_count") + 1,
        )

    @staticmethod
    def mark_pending_deletion(image_id: uuid.UUID) -> None:
        """Mark an image instance as pending deletion (runner offline)."""
        requested_at = timezone.now()
        ImageInstance.objects.filter(id=image_id).update(
            status=ImageInstance.Status.PENDING_DELETION,
            delete_requested_at=Coalesce("delete_requested_at", Value(requested_at)),
            delete_last_error="",
        )

    @staticmethod
    def mark_deleted(image_id: uuid.UUID) -> None:
        """Mark an image instance as fully deleted."""
        ImageInstance.objects.filter(id=image_id).update(
            status=ImageInstance.Status.DELETED,
            deleting_task_id=None,
            deleted_at=timezone.now(),
            delete_confirmed_at=timezone.now(),
        )

    @staticmethod
    def mark_delete_failed(image_id: uuid.UUID, *, error: str = "") -> None:
        """Mark an image instance deletion as failed."""
        ImageInstance.objects.filter(id=image_id).update(
            status=ImageInstance.Status.DELETE_FAILED,
            deleting_task_id=None,
            delete_last_error=error,
        )

    @staticmethod
    def list_pending_delete_for_runner(runner_id: uuid.UUID) -> QuerySet[ImageInstance]:
        """Return image instances that still need runner-side deletion."""
        return ImageInstance.objects.filter(
            runner_id=runner_id,
            status__in=[ImageInstance.Status.DELETING, ImageInstance.Status.PENDING_DELETION],
        ).exclude(runner_ref="").select_related(
            "runner",
            "origin_definition",
            "origin_workspace",
            "build_job",
        )


class ImageDefinitionRepository:
    """Data access for image definition records."""

    @staticmethod
    def list_by_org(organization_id: uuid.UUID) -> QuerySet[ImageDefinition]:
        return ImageDefinitionRepository.annotate_build_summaries(
            ImageDefinition.objects.filter(
                Q(organization__isnull=True) | Q(organization_id=organization_id)
            ).exclude(
                status=ImageDefinition.Status.DELETED
            )
        ).order_by(
            "name", "-updated_at", "-created_at"
        )

    @staticmethod
    def annotate_build_summaries(
        queryset: QuerySet[ImageDefinition],
    ) -> QuerySet[ImageDefinition]:
        """Attach per-runner build counts used by the org-settings summary."""
        return queryset.annotate(
            summary_active=Count(
                "runner_builds",
                filter=Q(runner_builds__status=ImageBuildJob.Status.ACTIVE),
            ),
            summary_building=Count(
                "runner_builds",
                filter=Q(
                    runner_builds__status__in=[
                        ImageBuildJob.Status.PENDING,
                        ImageBuildJob.Status.BUILDING,
                    ]
                ),
            ),
            summary_failed=Count(
                "runner_builds",
                filter=Q(runner_builds__status=ImageBuildJob.Status.FAILED),
            ),
            summary_inactive=Count(
                "runner_builds",
                filter=Q(runner_builds__status=ImageBuildJob.Status.DEACTIVATED),
            ),
            summary_removing=Count(
                "runner_builds",
                filter=Q(
                    runner_builds__status__in=[
                        ImageBuildJob.Status.PENDING_DELETION,
                        ImageBuildJob.Status.DELETING,
                    ]
                ),
            ),
        )

    @staticmethod
    def build_summary(definition: ImageDefinition) -> dict[str, int]:
        """Return runner-build counts for API/MCP list payloads."""
        if hasattr(definition, "summary_active"):
            return {
                "active": int(getattr(definition, "summary_active", 0) or 0),
                "building": int(getattr(definition, "summary_building", 0) or 0),
                "failed": int(getattr(definition, "summary_failed", 0) or 0),
                "inactive": int(getattr(definition, "summary_inactive", 0) or 0),
                "removing": int(getattr(definition, "summary_removing", 0) or 0),
            }

        statuses = ImageBuildJob.objects.filter(
            image_definition_id=definition.id,
        ).exclude(
            status=ImageBuildJob.Status.DELETED,
        ).values_list("status", flat=True)
        counts = {
            "active": 0,
            "building": 0,
            "failed": 0,
            "inactive": 0,
            "removing": 0,
        }
        for status in statuses:
            if status == ImageBuildJob.Status.ACTIVE:
                counts["active"] += 1
            elif status in {
                ImageBuildJob.Status.PENDING,
                ImageBuildJob.Status.BUILDING,
            }:
                counts["building"] += 1
            elif status == ImageBuildJob.Status.FAILED:
                counts["failed"] += 1
            elif status == ImageBuildJob.Status.DEACTIVATED:
                counts["inactive"] += 1
            elif status in {
                ImageBuildJob.Status.PENDING_DELETION,
                ImageBuildJob.Status.DELETING,
            }:
                counts["removing"] += 1
        return counts

    @staticmethod
    def get_by_id(image_definition_id: uuid.UUID) -> ImageDefinition | None:
        return ImageDefinition.objects.filter(id=image_definition_id).first()

    @staticmethod
    def get_by_id_and_org(
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ImageDefinition | None:
        """Fetch a visible image definition scoped to an organization."""
        return ImageDefinition.objects.filter(
            id=image_definition_id,
        ).filter(
            Q(organization__isnull=True) | Q(organization_id=organization_id)
        ).first()

    @staticmethod
    def deactivate(definition_id: uuid.UUID) -> None:
        """Deactivate definition: immediately no longer selectable for new workspaces."""
        ImageDefinition.objects.filter(id=definition_id).update(
            is_active=False,
            status=ImageDefinition.Status.DEACTIVATED,
            deactivated_at=timezone.now(),
        )

    @staticmethod
    def activate(definition_id: uuid.UUID) -> None:
        """Re-activate a deactivated or restore a failed-delete definition."""
        ImageDefinition.objects.filter(id=definition_id).update(
            is_active=True,
            status=ImageDefinition.Status.ACTIVE,
            deactivated_at=None,
            delete_last_error="",
        )

    @staticmethod
    def mark_pending_deletion(definition_id: uuid.UUID) -> None:
        """Mark definition pending deletion (waiting for build deletes)."""
        requested_at = timezone.now()
        ImageDefinition.objects.filter(id=definition_id).update(
            is_active=False,
            status=ImageDefinition.Status.PENDING_DELETION,
            delete_requested_at=Coalesce("delete_requested_at", Value(requested_at)),
            delete_last_error="",
        )

    @staticmethod
    def mark_deleting(definition_id: uuid.UUID) -> None:
        """Mark definition as actively deleting its builds."""
        now = timezone.now()
        ImageDefinition.objects.filter(id=definition_id).update(
            status=ImageDefinition.Status.DELETING,
            delete_requested_at=Coalesce("delete_requested_at", Value(now)),
            delete_started_at=now,
            delete_last_error="",
            delete_attempt_count=F("delete_attempt_count") + 1,
        )

    @staticmethod
    def mark_deleted(definition_id: uuid.UUID) -> None:
        """Mark definition as fully deleted after all builds are confirmed deleted."""
        ImageDefinition.objects.filter(id=definition_id).update(
            status=ImageDefinition.Status.DELETED,
            delete_confirmed_at=timezone.now(),
            deleted_at=timezone.now(),
        )

    @staticmethod
    def mark_delete_failed(definition_id: uuid.UUID, *, error: str = "") -> None:
        """Mark definition deletion as failed."""
        ImageDefinition.objects.filter(id=definition_id).update(
            is_active=False,
            status=ImageDefinition.Status.DELETE_FAILED,
            delete_last_error=error,
        )


class ImageBuildJobRepository:
    """Data access for runner image build records."""

    @staticmethod
    def list_for_definition(
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
    ) -> QuerySet[ImageBuildJob]:
        """List runner image builds, optionally scoped to an organization."""
        queryset = ImageBuildJob.objects.filter(
            image_definition_id=image_definition_id
        ).exclude(status=ImageBuildJob.Status.DELETED)
        if organization_id is not None:
            queryset = queryset.filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
        return queryset.select_related(
            "runner",
            "image_definition",
            "build_task",
            "image_instance",
        )

    @staticmethod
    def get(
        image_definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
    ) -> ImageBuildJob | None:
        """Fetch one runner image build, optionally scoped to an organization."""
        queryset = ImageBuildJob.objects.filter(
            image_definition_id=image_definition_id,
            runner_id=runner_id,
        )
        if organization_id is not None:
            queryset = queryset.filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
        return queryset.select_related(
            "runner",
            "image_definition",
            "build_task",
            "image_instance",
        ).first()

    @staticmethod
    def get_by_id(build_job_id: uuid.UUID) -> ImageBuildJob | None:
        """Fetch one runner image build by primary key."""
        return ImageBuildJob.objects.filter(
            id=build_job_id
        ).select_related(
            "runner",
            "image_definition",
            "build_task",
            "image_instance",
        ).first()

    @staticmethod
    def get_for_org(
        image_definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ImageBuildJob | None:
        """Fetch one runner image build scoped to an organization."""
        return ImageBuildJobRepository.get(
            image_definition_id,
            runner_id,
            organization_id=organization_id,
        )

    @staticmethod
    def delete_for_org(
        image_definition_id: uuid.UUID,
        runner_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> int:
        """Delete a runner image build scoped to an organization."""
        deleted, _ = ImageBuildJob.objects.filter(
            image_definition_id=image_definition_id,
            runner_id=runner_id,
        ).filter(
            Q(image_definition__organization_id=organization_id)
            | Q(image_definition__organization__isnull=True)
        ).delete()
        return deleted

    @staticmethod
    def mark_pending_deletion(build_job_id: uuid.UUID) -> None:
        """Mark build job as pending deletion (runner offline)."""
        requested_at = timezone.now()
        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.PENDING_DELETION,
            delete_requested_at=Coalesce("delete_requested_at", Value(requested_at)),
            delete_last_error="",
        )

    @staticmethod
    def mark_deleting(build_job_id: uuid.UUID, *, deleting_task_id: str) -> None:
        """Mark build job as actively deleting on runner."""
        now = timezone.now()
        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.DELETING,
            deleting_task_id=deleting_task_id,
            delete_requested_at=Coalesce("delete_requested_at", Value(now)),
            delete_started_at=now,
            delete_last_error="",
            delete_attempt_count=F("delete_attempt_count") + 1,
        )

    @staticmethod
    def mark_deleted(build_job_id: uuid.UUID) -> None:
        """Mark build job as fully deleted after runner confirmation."""
        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.DELETED,
            deleting_task_id=None,
            delete_confirmed_at=timezone.now(),
        )

    @staticmethod
    def mark_delete_failed(build_job_id: uuid.UUID, *, error: str = "") -> None:
        """Mark build job deletion as failed."""
        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.DELETE_FAILED,
            deleting_task_id=None,
            delete_last_error=error,
        )

    @staticmethod
    def mark_failed(build_job_id: uuid.UUID, *, error: str = "") -> None:
        """Mark a hung or failed build job as failed."""
        ImageBuildJob.objects.filter(id=build_job_id).update(
            status=ImageBuildJob.Status.FAILED,
            delete_last_error=error or "",
        )

    @staticmethod
    def list_stale_builds(*, cutoff: datetime) -> QuerySet[ImageBuildJob]:
        """Return build jobs stuck in pending/building past the cutoff."""
        return ImageBuildJob.objects.filter(
            status__in=[
                ImageBuildJob.Status.PENDING,
                ImageBuildJob.Status.BUILDING,
            ],
            updated_at__lt=cutoff,
        ).select_related("image_instance", "image_definition")

    @staticmethod
    def list_stale_deletes(*, cutoff: datetime) -> QuerySet[ImageBuildJob]:
        """Return build jobs stuck in deletion past the cutoff."""
        return ImageBuildJob.objects.filter(
            status__in=[
                ImageBuildJob.Status.PENDING_DELETION,
                ImageBuildJob.Status.DELETING,
            ],
        ).filter(
            Q(delete_requested_at__lt=cutoff)
            | Q(delete_requested_at__isnull=True, updated_at__lt=cutoff)
        ).select_related("image_instance", "image_definition")

    @staticmethod
    def list_in_progress_deletes_for_definition(
        definition_id: uuid.UUID,
    ) -> QuerySet[ImageBuildJob]:
        """Return child jobs still waiting on runner-side deletion."""
        return ImageBuildJob.objects.filter(
            image_definition_id=definition_id,
            status__in=[
                ImageBuildJob.Status.PENDING_DELETION,
                ImageBuildJob.Status.DELETING,
            ],
        )

    @staticmethod
    def list_pending_delete_for_runner(runner_id: uuid.UUID) -> QuerySet[ImageBuildJob]:
        """Return build jobs that need runner-side deletion."""
        return ImageBuildJob.objects.filter(
            runner_id=runner_id,
            status__in=[
                ImageBuildJob.Status.PENDING_DELETION,
                ImageBuildJob.Status.DELETING,
            ],
        ).select_related("runner", "image_definition", "image_instance")

    @staticmethod
    def list_non_deleted_for_definition(
        definition_id: uuid.UUID,
    ) -> QuerySet[ImageBuildJob]:
        """Return all build jobs for a definition that aren't deleted."""
        return ImageBuildJob.objects.filter(
            image_definition_id=definition_id,
        ).exclude(status=ImageBuildJob.Status.DELETED)

    @staticmethod
    def has_dependent_workspaces(build_job_id: uuid.UUID) -> tuple[bool, int]:
        """Check if any non-deleted workspaces depend on this build's image instance."""
        from .models import ImageInstance as II
        instances = II.objects.filter(
            build_job_id=build_job_id,
        ).exclude(status__in=[II.Status.DELETED])
        count = 0
        for instance in instances:
            ws_count = Workspace.objects.filter(
                base_image_instance=instance,
            ).exclude(
                status__in=[
                    WorkspaceStatus.PENDING_DELETION,
                    WorkspaceStatus.DELETING,
                    WorkspaceStatus.REMOVED,
                    WorkspaceStatus.DELETED,
                ],
            ).count()
            count += ws_count
        return count > 0, count
