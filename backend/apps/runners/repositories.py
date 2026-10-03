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

from django.db import connection, transaction
from django.db.models import Count, Exists, F, OuterRef, Q, QuerySet, Value
from django.db.models.functions import Coalesce, Length
from django.utils import timezone

from .enums import (
    RunnerStatus,
    TaskStatus,
    TaskType,
    WorkspaceOperation,
    WorkspaceStatus,
)
from .locking import lock_runner
from .models import (
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
    Runner,
    RunnerSystemMetrics,
    Task,
    Workspace,
    WorkspaceProcess,
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
        runner.last_heartbeat_at = timezone.now()
        runner.connected_at = timezone.now()
        runner.disconnected_at = None
        runner.save(
            update_fields=[
                "status",
                "sid",
                "available_runtimes",
                "last_heartbeat_at",
                "connected_at",
                "disconnected_at",
                "updated_at",
            ]
        )
        return runner

    @staticmethod
    def set_offline(runner: Runner) -> Runner:
        """Mark a runner as offline."""
        runner.status = RunnerStatus.OFFLINE
        runner.sid = ""
        runner.disconnected_at = timezone.now()
        runner.save(update_fields=["status", "sid", "disconnected_at", "updated_at"])
        return runner

    @staticmethod
    def list_by_organization(organization_id: uuid.UUID) -> QuerySet[Runner]:
        """Return all runners for a specific organization."""
        return Runner.objects.filter(organization_id=organization_id)

    @staticmethod
    def update_heartbeat(runner: Runner) -> Runner:
        """Update the last heartbeat timestamp for a runner."""
        runner.last_heartbeat_at = timezone.now()
        # Fence against reconnect/disconnect racing the authenticated handler.
        Runner.objects.filter(pk=runner.pk, sid=runner.sid).exclude(sid="").update(
            last_heartbeat_at=runner.last_heartbeat_at,
            status=RunnerStatus.ONLINE,
            disconnected_at=None,
            updated_at=timezone.now(),
        )
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
    def get_history(
        runner_id: uuid.UUID, since: datetime
    ) -> QuerySet[RunnerSystemMetrics]:
        """Return all metrics since a given timestamp."""
        return RunnerSystemMetrics.objects.filter(
            runner_id=runner_id,
            timestamp__gte=since,
        ).order_by("timestamp")

    @staticmethod
    def purge_old(runner_id: uuid.UUID, keep_hours: int = 24) -> int:
        """Delete metrics older than *keep_hours* hours. Returns count deleted."""
        from datetime import timedelta

        from django.utils import timezone as tz

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
    def get_by_id(workspace_id: uuid.UUID, *, lock: bool = False) -> Workspace | None:
        """Fetch a workspace by ID, optionally acquiring a row lock."""
        queryset = Workspace.objects.filter(id=workspace_id)
        if lock:
            # Keep the workspace row locked without locking nullable related
            # rows pulled in by select_related (e.g. created_by).
            if connection.features.has_select_for_update_of:
                queryset = queryset.select_for_update(of=("self",))
            else:
                queryset = queryset.select_for_update()
        return (
            queryset.select_related(
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
        desktop_width: int | None = None,
        desktop_height: int | None = None,
        base_image_instance=None,
        created_by=None,
    ) -> Workspace:
        """Create a new workspace record."""
        from .desktop import DEFAULT_DESKTOP_HEIGHT, DEFAULT_DESKTOP_WIDTH

        workspace = Workspace.objects.create(
            id=workspace_id,
            runner=runner,
            name=name,
            runtime_type=runtime_type,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            desktop_width=(
                DEFAULT_DESKTOP_WIDTH if desktop_width is None else desktop_width
            ),
            desktop_height=(
                DEFAULT_DESKTOP_HEIGHT if desktop_height is None else desktop_height
            ),
            base_image_instance=base_image_instance,
            status=WorkspaceStatus.CREATING,
            active_operation=WorkspaceOperation.CREATING,
            created_by=created_by,
        )
        workspace.last_activity_at = workspace.created_at
        workspace.save(update_fields=["last_activity_at"])
        return workspace

    @staticmethod
    def list_attached_credentials(workspace_id: uuid.UUID) -> list[Any]:
        """Return workspace credential rows with their service metadata."""
        workspace = Workspace.objects.filter(id=workspace_id).first()
        if workspace is None:
            return []
        return list(workspace.credentials.all().select_related("service"))

    @staticmethod
    def list_ordinary_credential_ids(workspace_id: uuid.UUID) -> set[uuid.UUID]:
        """Return attached non-OAuth credential IDs for runner sync comparison."""
        from django.apps import apps

        credential_model = apps.get_model("credentials", "Credential")
        return set(
            credential_model.objects.filter(workspaces__id=workspace_id)
            .exclude(service__credential_type="mcp_oauth")
            .values_list("id", flat=True)
        )

    @staticmethod
    def set_credentials(workspace: Workspace, credentials: list) -> Workspace:
        """Replace the attachment relation without claiming on-disk state."""
        workspace.credentials.set(credentials)
        workspace.save(update_fields=["updated_at"])
        cache = getattr(workspace, "_prefetched_objects_cache", None)
        if cache is not None:
            cache.pop("credentials", None)
        return workspace

    @staticmethod
    def replace_configuration(
        workspace: Workspace,
        *,
        name: str | None,
        credentials: list | None,
        plugin_ids: list[uuid.UUID] | None,
        enabled_by,
        qemu_values: tuple | None = None,
        desktop_values: tuple | None = None,
    ) -> Workspace:
        """Replace the selected workspace associations after service validation."""
        if name is not None:
            workspace.name = name
            workspace.save(update_fields=["name", "updated_at"])
        if credentials is not None:
            workspace.credentials.set(credentials)
        if qemu_values is not None:
            (
                workspace.qemu_vcpus,
                workspace.qemu_memory_mb,
                workspace.qemu_disk_size_gb,
            ) = qemu_values
            workspace.save(
                update_fields=[
                    "qemu_vcpus",
                    "qemu_memory_mb",
                    "qemu_disk_size_gb",
                    "updated_at",
                ]
            )
        if desktop_values is not None:
            workspace.desktop_width, workspace.desktop_height = desktop_values
            workspace.save(
                update_fields=["desktop_width", "desktop_height", "updated_at"]
            )
        if plugin_ids is not None:
            from apps.plugins.repositories import WorkspacePluginActivationRepository

            WorkspacePluginActivationRepository.replace_for_workspace(
                workspace, list(dict.fromkeys(plugin_ids)), enabled_by=enabled_by
            )
        if credentials is not None or plugin_ids is not None:
            workspace.save(update_fields=["updated_at"])
            cache = getattr(workspace, "_prefetched_objects_cache", None)
            if cache is not None:
                cache.pop("credentials", None)
        return WorkspaceRepository.get_by_id(workspace.id)

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
    def list_ids_for_credential(
        credential_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """Return workspace IDs with *credential_id* attached (org-agnostic)."""
        return list(
            Workspace.objects.filter(credentials__id=credential_id).values_list(
                "id", flat=True
            )
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
        from .capture_repository import CaptureRepository

        workspace.active_operation = CaptureRepository.operation(
            workspace.id, active_operation
        )
        workspace.save(update_fields=["active_operation", "updated_at"])
        return workspace

    @staticmethod
    def claim_auto_stop(
        workspace_id: uuid.UUID,
        *,
        idle_before: datetime,
        expected_timeout_minutes: int,
    ) -> Workspace | None:
        """Atomically claim an idle workspace for an inactivity stop.

        The service supplies the policy-derived cutoff. Under the same row
        lock used by scheduled admission, verify the workspace is still
        running/idle and the configured timeout has not changed, then claim
        STOPPING. Returning None means state changed before the claim.
        """
        with transaction.atomic():
            identity = (
                Workspace.objects.filter(pk=workspace_id).values("runner_id").first()
            )
            if identity is None:
                return None
            lock_runner(identity["runner_id"])
            workspace = (
                Workspace.objects.select_for_update().filter(id=workspace_id).first()
            )
            if workspace is None:
                return None
            timeout = (
                Workspace.objects.filter(id=workspace_id)
                .values_list(
                    "runner__organization__workspace_auto_stop_timeout_minutes",
                    flat=True,
                )
                .first()
            )
            runner_status = (
                Workspace.objects.filter(id=workspace_id)
                .values_list("runner__status", flat=True)
                .first()
            )
            from apps.harness.models import HarnessSession

            has_busy_session = HarnessSession.objects.filter(
                workspace_id=workspace_id, status="busy"
            ).exists()
            if (
                workspace.status != WorkspaceStatus.RUNNING
                or workspace.active_operation is not None
                or workspace.current_task_id is not None
                or has_busy_session
                or runner_status != RunnerStatus.ONLINE
                or workspace.last_activity_at is None
                or workspace.last_activity_at > idle_before
                or timeout != expected_timeout_minutes
                or timeout is None
                or timeout <= 0
            ):
                return None
            workspace.active_operation = WorkspaceOperation.STOPPING
            workspace.save(update_fields=["active_operation", "updated_at"])
            return WorkspaceRepository.get_by_id(workspace_id)

    @staticmethod
    def clear_auto_stop_claim(workspace_id: uuid.UUID) -> None:
        """Clear an auto-stop claim after task creation/dispatch failure."""
        Workspace.objects.filter(
            id=workspace_id,
            active_operation=WorkspaceOperation.STOPPING,
        ).update(active_operation=None, updated_at=timezone.now())

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
        workspace.save(
            update_fields=[
                "qemu_vcpus",
                "qemu_memory_mb",
                "qemu_disk_size_gb",
                "updated_at",
            ]
        )
        return workspace

    @staticmethod
    def update_desktop_geometry(
        workspace: Workspace,
        *,
        desktop_width: int,
        desktop_height: int,
    ) -> Workspace:
        """Persist the fixed desktop framebuffer size."""
        workspace.desktop_width = desktop_width
        workspace.desktop_height = desktop_height
        workspace.save(update_fields=["desktop_width", "desktop_height", "updated_at"])
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
        return Workspace.objects.filter(
            base_image_instance_id=image_instance_id
        ).exclude(
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
            active_operation=WorkspaceOperation.REMOVING,
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
class WorkspaceProcessRepository:
    """Data access for WorkspaceProcess records."""

    @staticmethod
    def create(
        *,
        process_id: uuid.UUID,
        workspace: Workspace,
        command: str,
        workdir: str = "/workspace",
        name: str = "",
        created_by=None,
        session_id: uuid.UUID | None = None,
        run_count: int = 1,
        kind: str = "persistent",
    ) -> WorkspaceProcess:
        """Create a new process record in running state."""
        from .enums import ProcessStatus

        return WorkspaceProcess.objects.create(
            id=process_id,
            workspace=workspace,
            command=command,
            workdir=workdir or "/workspace",
            name=name or "",
            status=ProcessStatus.RUNNING,
            created_by=created_by,
            session_id=session_id,
            run_count=run_count,
            kind=kind or "persistent",
        )

    @staticmethod
    def get_by_id(process_id: uuid.UUID) -> WorkspaceProcess | None:
        """Fetch a process by its ID."""
        return (
            WorkspaceProcess.objects.filter(id=process_id)
            .select_related("workspace", "workspace__runner", "created_by")
            .first()
        )

    @staticmethod
    def get_for_workspace(
        process_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> WorkspaceProcess | None:
        """Fetch a process scoped to a workspace (None when foreign)."""
        return (
            WorkspaceProcess.objects.filter(id=process_id, workspace_id=workspace_id)
            .select_related("workspace", "workspace__runner", "created_by")
            .first()
        )

    @staticmethod
    def get_by_name(
        workspace_id: uuid.UUID,
        name: str,
        *,
        kind: str | None = None,
        session_id: uuid.UUID | None = None,
    ) -> WorkspaceProcess | None:
        """Fetch a process by exact (case-sensitive) name in a workspace.

        Without filters this prefers the persistent row; pass ``kind``
        (plus ``session_id`` for temp rows) to scope the lookup.
        """
        queryset = WorkspaceProcess.objects.filter(
            workspace_id=workspace_id,
            name=name,
        )
        if kind is not None:
            queryset = queryset.filter(kind=kind)
            if kind == "temp":
                queryset = queryset.filter(session_id=session_id)
        else:
            queryset = queryset.filter(kind="persistent")
        return queryset.select_related(
            "workspace", "workspace__runner", "created_by"
        ).first()

    @staticmethod
    def get_temp_by_name(
        workspace_id: uuid.UUID,
        name: str,
        session_id: uuid.UUID,
    ) -> WorkspaceProcess | None:
        """Fetch a temp process by name scoped to one session."""
        return (
            WorkspaceProcess.objects.filter(
                workspace_id=workspace_id,
                name=name,
                kind="temp",
                session_id=session_id,
            )
            .select_related("workspace", "workspace__runner", "created_by")
            .first()
        )

    @staticmethod
    def resolve_for_workspace(
        workspace_id: uuid.UUID,
        id_or_name: str | uuid.UUID,
        *,
        kind: str | None = None,
        session_id: uuid.UUID | None = None,
    ) -> WorkspaceProcess | None:
        """Resolve a process by UUID first, then by exact name.

        UUID matches win regardless of kind, but a temp row only
        resolves when its ``session_id`` matches (temp rows are
        session-scoped). Name fallback honors the same scoping.

        Never raises on unparsable UUIDs — falls back to name lookup.
        """
        candidate: WorkspaceProcess | None = None
        try:
            parsed = (
                id_or_name
                if isinstance(id_or_name, uuid.UUID)
                else uuid.UUID(str(id_or_name))
            )
        except (ValueError, TypeError, AttributeError):
            parsed = None
        if parsed is not None:
            candidate = WorkspaceProcessRepository.get_for_workspace(
                parsed, workspace_id
            )
            if candidate is not None:
                candidate_kind = str(getattr(candidate, "kind", "") or "")
                if candidate_kind == "temp":
                    if session_id is not None and candidate.session_id != session_id:
                        candidate = None
                    elif kind == "persistent":
                        candidate = None
                elif kind == "temp":
                    candidate = None
                if candidate is not None:
                    return candidate
        if kind == "temp":
            if session_id is None:
                return None
            return WorkspaceProcessRepository.get_temp_by_name(
                workspace_id, str(id_or_name), session_id
            )
        if kind == "persistent":
            return WorkspaceProcessRepository.get_by_name(
                workspace_id, str(id_or_name), kind="persistent"
            )
        if session_id is not None:
            temp = WorkspaceProcessRepository.get_temp_by_name(
                workspace_id, str(id_or_name), session_id
            )
            if temp is not None:
                return temp
        return WorkspaceProcessRepository.get_by_name(workspace_id, str(id_or_name))

    @staticmethod
    def update_for_restart(
        process_id: uuid.UUID,
        *,
        command: str,
        workdir: str,
        log_path: str = "",
        run_count: int = 1,
        created_by=None,
        session_id: uuid.UUID | None = None,
    ) -> int:
        """Reset a row for a new run on the same process id.

        Keeps ``name`` unchanged; overwrites command/workdir, resets
        lifecycle to RUNNING, clears pid/exit/ended_at, sets log path,
        run count and a fresh started_at (auto_now_add needs explicit set).
        Returns rows updated.
        """
        from .enums import ProcessStatus

        fields: dict[str, Any] = {
            "command": command,
            "workdir": workdir or "/workspace",
            "status": ProcessStatus.RUNNING,
            "pid": None,
            "exit_code": None,
            "ended_at": None,
            "log_path": log_path or "",
            "run_count": run_count,
            "started_at": timezone.now(),
        }
        if created_by is not None:
            fields["created_by"] = created_by
        if session_id is not None:
            fields["session_id"] = session_id
        return WorkspaceProcess.objects.filter(id=process_id).update(**fields)

    @staticmethod
    def list_by_workspace(
        workspace_id: uuid.UUID,
        *,
        kinds: tuple[str, ...] | None = None,
        running_only: bool = False,
    ) -> QuerySet[WorkspaceProcess]:
        """Return processes for a workspace, newest first.

        ``kinds`` restricts to ``persistent``/``temp`` rows; ``running_only``
        keeps only RUNNING rows (the user-visible temp subset).
        """
        from .enums import ProcessStatus

        queryset = WorkspaceProcess.objects.filter(workspace_id=workspace_id)
        if kinds is not None:
            queryset = queryset.filter(kind__in=list(kinds))
        if running_only:
            queryset = queryset.filter(status=ProcessStatus.RUNNING)
        return queryset.select_related("created_by")

    @staticmethod
    def list_running_by_workspace(
        workspace_id: uuid.UUID,
    ) -> QuerySet[WorkspaceProcess]:
        """Return processes still marked running for a workspace."""
        from .enums import ProcessStatus

        return WorkspaceProcess.objects.filter(
            workspace_id=workspace_id,
            status=ProcessStatus.RUNNING,
        )

    @staticmethod
    def list_running_session_processes(
        workspace_id: uuid.UUID,
        session_id: uuid.UUID,
        *,
        kind: str | None = None,
    ) -> QuerySet[WorkspaceProcess]:
        """Return RUNNING rows of one agent session (cleanup hook source).

        ``kind="temp"`` restricts to session-scoped temp rows; ``None``
        returns all running rows of the session.
        """
        from .enums import ProcessStatus

        queryset = WorkspaceProcess.objects.filter(
            workspace_id=workspace_id,
            session_id=session_id,
            status=ProcessStatus.RUNNING,
        )
        if kind is not None:
            queryset = queryset.filter(kind=kind)
        return queryset.select_related("workspace", "workspace__runner")

    @staticmethod
    def update_status(
        process_id: uuid.UUID,
        *,
        status: str,
        exit_code: int | None = None,
        pid: int | None = None,
        log_path: str | None = None,
        ended_at=None,
    ) -> int:
        """Update lifecycle fields of a process. Returns rows updated."""
        fields: dict[str, Any] = {"status": status}
        if exit_code is not None:
            fields["exit_code"] = exit_code
        if pid is not None:
            fields["pid"] = pid
        if log_path is not None:
            fields["log_path"] = log_path
        if ended_at is not None:
            fields["ended_at"] = ended_at
        return WorkspaceProcess.objects.filter(id=process_id).update(**fields)

    @staticmethod
    def mark_finished(
        process_id: uuid.UUID,
        *,
        status: str,
        exit_code: int | None = None,
    ) -> int:
        """Mark a process finished with an end timestamp."""
        return WorkspaceProcess.objects.filter(id=process_id).update(
            status=status,
            exit_code=exit_code,
            ended_at=timezone.now(),
        )

    @staticmethod
    def mark_processes_killed(
        workspace_id: uuid.UUID,
        *,
        status: str,
    ) -> int:
        """Mark all running processes of a workspace killed/finished."""
        from .enums import ProcessStatus

        return WorkspaceProcess.objects.filter(
            workspace_id=workspace_id,
            status=ProcessStatus.RUNNING,
        ).update(status=status, ended_at=timezone.now())

    @staticmethod
    def delete(process_id: uuid.UUID) -> int:
        """Delete a process record. Returns rows deleted."""
        deleted, _ = WorkspaceProcess.objects.filter(id=process_id).delete()
        return deleted

    @staticmethod
    def delete_for_workspace(
        process_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> int:
        """Delete a process scoped to a workspace. Returns rows deleted."""
        deleted, _ = WorkspaceProcess.objects.filter(
            id=process_id, workspace_id=workspace_id
        ).delete()
        return deleted


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
        operation_payload: dict | None = None,
        capture_request_id: uuid.UUID | None = None,
    ) -> Task:
        """Create a new task record."""
        from .operations import OperationRepository

        with transaction.atomic():
            lock_runner(runner.id)
            if workspace is not None:
                workspace = Workspace.objects.select_for_update().get(pk=workspace.id)
                from .capture_repository import CaptureRepository

                if capture_request_id is None and CaptureRepository.active(
                    workspace.id
                ):
                    from common.exceptions import ConflictError

                    raise ConflictError("Workspace is capturing image")
            task = Task.objects.create(
                id=task_id,
                runner=runner,
                workspace=workspace,
                type=task_type,
                status=TaskStatus.PENDING,
            )
            OperationRepository.allocate(task, capture_request_id=capture_request_id)
            if operation_payload is not None or task_type in {
                TaskType.STOP_WORKSPACE,
                TaskType.RESUME_WORKSPACE,
                TaskType.UPDATE_WORKSPACE,
                TaskType.REMOVE_WORKSPACE,
            }:
                payload = {"task_id": str(task.id)}
                if workspace is not None:
                    payload.update(
                        workspace_id=str(workspace.id),
                        qemu_vcpus=workspace.qemu_vcpus,
                        qemu_memory_mb=workspace.qemu_memory_mb,
                        qemu_disk_size_gb=workspace.qemu_disk_size_gb,
                    )
                payload.update(operation_payload or {})
                payload["task_id"] = str(task.id)
                OperationRepository.prepare(
                    str(task.id), "task:" + str(task_type), payload
                )
                task.refresh_from_db()
            return task

    @staticmethod
    def mark_in_progress(task: Task) -> Task:
        """Mark a task as in progress."""
        Task.objects.filter(id=task.id, status=TaskStatus.PENDING).update(
            status=TaskStatus.IN_PROGRESS
        )
        task.refresh_from_db()
        return task

    @staticmethod
    def release_workspace(task: Task) -> None:
        """Release a child identity, never its enclosing capture reservation."""
        from .capture_repository import CaptureRepository

        operation = (
            CaptureRepository.operation(task.workspace_id, None)
            if task.workspace_id
            else None
        )
        Workspace.objects.filter(current_task=task).update(
            current_task=None, active_operation=operation
        )

    @staticmethod
    def complete(task: Task) -> Task:
        """Mark a task as completed."""
        with transaction.atomic():
            lock_runner(task.runner_id)
            if task.workspace_id:
                Workspace.objects.select_for_update().get(pk=task.workspace_id)
            Task.objects.select_for_update().get(pk=task.id)
            task.status = TaskStatus.COMPLETED
            task.completed_at = timezone.now()
            task.save(update_fields=["status", "completed_at"])
            TaskRepository.release_workspace(task)
            return task

    @staticmethod
    def fail(task: Task, error: str) -> Task:
        """Mark a task as failed with an error message."""
        with transaction.atomic():
            lock_runner(task.runner_id)
            if task.workspace_id:
                Workspace.objects.select_for_update().get(pk=task.workspace_id)
            Task.objects.select_for_update().get(pk=task.id)
            task.status = TaskStatus.FAILED
            task.error = error
            task.completed_at = timezone.now()
            task.save(update_fields=["status", "error", "completed_at"])
            TaskRepository.release_workspace(task)
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
    ) -> ImageInstance:
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
    ) -> ImageInstance:
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
            size_bytes=None,
            build_job=build_job,
            created_by=created_by,
            status=status,
            creating_task_id=creating_task_id,
        )
        if credentials:
            image.credentials.set(credentials)
        return image

    @staticmethod
    def get_by_task_id(task_id: str) -> ImageInstance | None:
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
    def mark_ready(image_id, *, runner_ref: str, size_bytes: int) -> None:
        """Update a creating image instance to ready once the runner reports success."""
        ImageInstance.objects.filter(
            id=image_id, status__in=["building", "capturing"]
        ).update(
            status=ImageInstance.Status.READY,
            runner_ref=runner_ref,
            size_bytes=size_bytes,
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
    def get_by_id(image_id: uuid.UUID) -> ImageInstance | None:
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
    ) -> ImageInstance | None:
        """Fetch a built image instance by its runner build relation."""
        return (
            ImageInstance.objects.filter(current_assignments__id=build_job_id)
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
    def list_by_workspace(workspace_id: uuid.UUID) -> QuerySet[ImageInstance]:
        """Return all image instances captured from a workspace."""
        return (
            ImageInstance.objects.filter(origin_workspace_id=workspace_id)
            .exclude(status=ImageInstance.Status.DELETED)
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
        )

    @staticmethod
    def list_by_user(user) -> QuerySet[ImageInstance]:
        """Return all visible image instances created by a specific user."""
        return (
            ImageInstance.objects.filter(created_by=user)
            .exclude(status=ImageInstance.Status.DELETED)
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
        from common.exceptions import ConflictError

        with transaction.atomic():
            ImageInstance.objects.select_for_update().get(id=image_id)
            if (
                Workspace.objects.filter(base_image_instance_id=image_id)
                .exclude(status__in=["removed", "deleted"])
                .exists()
            ):
                raise ConflictError("Image is still used by a workspace")
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
        from common.exceptions import ConflictError

        with transaction.atomic():
            ImageInstance.objects.select_for_update().get(id=image_id)
            if (
                Workspace.objects.filter(base_image_instance_id=image_id)
                .exclude(status__in=["removed", "deleted"])
                .exists()
            ):
                raise ConflictError("Image is still used by a workspace")
            ImageInstance.objects.filter(id=image_id).update(
                status=ImageInstance.Status.PENDING_DELETION,
                delete_requested_at=Coalesce(
                    "delete_requested_at", Value(requested_at)
                ),
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
            delete_last_error=error,
        )

    @staticmethod
    def list_pending_delete_for_runner(runner_id: uuid.UUID) -> QuerySet[ImageInstance]:
        """Return image instances that still need runner-side deletion."""
        return (
            ImageInstance.objects.filter(
                runner_id=runner_id,
                status__in=[
                    ImageInstance.Status.DELETING,
                    ImageInstance.Status.PENDING_DELETION,
                ],
            )
            .exclude(runner_ref="")
            .select_related(
                "runner",
                "origin_definition",
                "origin_workspace",
                "build_job",
            )
        )


class ImageDefinitionRepository:
    """Data access for image definition records."""

    @staticmethod
    def create(**fields):
        return ImageDefinition.objects.create(**fields)

    @staticmethod
    def update_recipe(definition_id, values):
        """Serialize edits with generation rendering/allocation; never auto-build."""
        from common.exceptions import ConflictError

        with transaction.atomic():
            definition = ImageDefinition.objects.select_for_update().get(
                id=definition_id
            )
            if definition.status not in {"active", "deactivated"}:
                raise ConflictError("Image definition is being removed")
            for field, value in values.items():
                if value is not None:
                    setattr(definition, field, value)
            definition.save()
            return definition

    @staticmethod
    def copy_name(base_name, org_id):
        base = ((base_name or "").strip() or "image")[:255]
        candidate = base
        index = 1
        while ImageDefinition.objects.filter(
            organization_id=org_id, name=candidate
        ).exists():
            suffix = " (Copy)" if index == 1 else f" (Copy {index})"
            candidate = base[: 255 - len(suffix)] + suffix
            index += 1
        return candidate

    @staticmethod
    def list_deleting():
        return ImageDefinition.objects.filter(
            status__in=["pending_deletion", "deleting"]
        )

    @staticmethod
    def list_by_org(organization_id: uuid.UUID) -> QuerySet[ImageDefinition]:
        return ImageDefinitionRepository.annotate_build_summaries(
            ImageDefinition.objects.filter(
                Q(organization__isnull=True) | Q(organization_id=organization_id)
            ).exclude(status=ImageDefinition.Status.DELETED)
        ).order_by("name", "-updated_at", "-created_at")

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

        statuses = (
            ImageBuildJob.objects.filter(
                image_definition_id=definition.id,
            )
            .exclude(
                status=ImageBuildJob.Status.DELETED,
            )
            .values_list("status", flat=True)
        )
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
        return (
            ImageDefinition.objects.filter(
                id=image_definition_id,
            )
            .filter(Q(organization__isnull=True) | Q(organization_id=organization_id))
            .first()
        )

    @staticmethod
    def deactivate(definition_id: uuid.UUID) -> None:
        """Deactivate definition: immediately no longer selectable for new workspaces."""
        ImageDefinition.objects.filter(
            id=definition_id, status__in=["active", "deactivated"]
        ).update(
            status=ImageDefinition.Status.DEACTIVATED,
            deactivated_at=timezone.now(),
        )

    @staticmethod
    def activate(definition_id: uuid.UUID) -> None:
        """Re-activate only when no durable deletion request still owns retirement."""
        from .models import ImageDeletionRequest
        from common.exceptions import ConflictError

        if (
            ImageDeletionRequest.objects.filter(
                target_type="definition", target_id=definition_id
            )
            .exclude(phase__in=["completed", "cancelled"])
            .exists()
        ):
            raise ConflictError(
                "Cancel or resolve deletion before restoring definition"
            )
        ImageDefinition.objects.filter(id=definition_id).update(
            status=ImageDefinition.Status.ACTIVE,
            deactivated_at=None,
            delete_last_error="",
        )

    @staticmethod
    def mark_pending_deletion(definition_id: uuid.UUID) -> None:
        """Mark definition pending deletion (waiting for build deletes)."""
        requested_at = timezone.now()
        ImageDefinition.objects.filter(id=definition_id).update(
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
            status=ImageDefinition.Status.DELETE_FAILED,
            delete_last_error=error,
        )


class ImageBuildJobRepository:
    """Data access for runner image build records."""

    @staticmethod
    def activate(job_id):
        job = ImageBuildJob.objects.get(pk=job_id)
        from common.exceptions import ConflictError

        if job.status in ["pending_deletion", "deleting", "deleted", "delete_failed"]:
            raise ConflictError("Image assignment is being removed")
        ImageBuildJob.objects.filter(id=job_id).update(
            status="active", deactivated_at=None
        )
        return ImageBuildJobRepository.get_by_id(job_id)

    @staticmethod
    def list_queued(runner_id):
        return ImageBuildJob.objects.filter(
            runner_id=runner_id, pending_generation__creating_task__status="pending"
        ).select_related(
            "pending_generation__revision", "pending_generation__creating_task"
        )

    @staticmethod
    def list_unallocated_pending(runner_id):
        return ImageBuildJob.objects.filter(
            runner_id=runner_id, status="pending", build_task__isnull=True
        ).select_related("image_definition", "runner")

    @staticmethod
    def ensure_inactive(definition, runner):
        """Create or deactivate an assignment without requesting a build."""
        from common.exceptions import ConflictError

        with transaction.atomic():
            lock_runner(runner.id)
            job, _ = ImageBuildJob.objects.get_or_create(
                image_definition=definition, runner=runner
            )
            job = ImageBuildJob.objects.select_for_update().get(pk=job.id)
            if job.status in [
                "pending_deletion",
                "deleting",
                "deleted",
                "delete_failed",
            ]:
                raise ConflictError("Image assignment is being removed")
            job.status = "deactivated"
            job.save(update_fields=["status", "updated_at"])
            return job

    @staticmethod
    def has_multiple_generations(job_id) -> bool:
        return ImageInstance.objects.filter(build_job_id=job_id).count() > 1

    @staticmethod
    def has_inflight_generations(job_id) -> bool:
        return ImageInstance.objects.filter(
            build_job_id=job_id, status__in=["building", "capturing"]
        ).exists()

    @staticmethod
    def get_by_delete_task(task_id):
        return ImageBuildJob.objects.filter(deleting_task_id=task_id).first()

    @staticmethod
    def list_for_definition(
        image_definition_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
    ) -> QuerySet[ImageBuildJob]:
        """List runner image builds, optionally scoped to an organization.

        The ``build_log`` column is deferred: list/poll responses carry
        status + metadata only (see ``ImageBuildJobListOut``). The full log
        is served on demand via the dedicated ``/log/`` endpoint. This keeps
        the 3s frontend polling from re-reading megabytes of log text.
        """
        queryset = ImageBuildJob.objects.filter(
            image_definition_id=image_definition_id
        ).exclude(status=ImageBuildJob.Status.DELETED)
        if organization_id is not None:
            queryset = queryset.filter(runner__organization_id=organization_id).filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
        return (
            queryset.select_related(
                "runner",
                "image_definition",
                "build_task",
                "current_generation",
                "pending_generation",
            )
            .defer("build_log")
            .annotate(build_log_size=Length("build_log"))
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
            queryset = queryset.filter(runner__organization_id=organization_id).filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
        return queryset.select_related(
            "runner",
            "image_definition",
            "build_task",
            "current_generation",
            "pending_generation",
        ).first()

    @staticmethod
    def get_by_id(build_job_id: uuid.UUID) -> ImageBuildJob | None:
        """Fetch one runner image build by primary key."""
        return (
            ImageBuildJob.objects.filter(id=build_job_id)
            .select_related(
                "runner",
                "image_definition",
                "build_task",
                "current_generation",
                "pending_generation",
            )
            .first()
        )

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
        deleted, _ = (
            ImageBuildJob.objects.filter(
                image_definition_id=image_definition_id,
                runner_id=runner_id,
            )
            .filter(
                Q(image_definition__organization_id=organization_id)
                | Q(image_definition__organization__isnull=True)
            )
            .delete()
        )
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
            delete_last_error=error,
        )

    @staticmethod
    def mark_failed(build_job_id: uuid.UUID, *, error: str = "") -> None:
        """Mark a hung or failed build job as failed."""
        ImageBuildJob.objects.filter(
            id=build_job_id, current_generation__isnull=True
        ).update(
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
        ).select_related("current_generation", "pending_generation", "image_definition")

    @staticmethod
    def list_stale_deletes(*, cutoff: datetime) -> QuerySet[ImageBuildJob]:
        """Return build jobs stuck in deletion past the cutoff."""
        return (
            ImageBuildJob.objects.filter(
                status__in=[
                    ImageBuildJob.Status.PENDING_DELETION,
                    ImageBuildJob.Status.DELETING,
                ],
            )
            .filter(
                Q(delete_requested_at__lt=cutoff)
                | Q(delete_requested_at__isnull=True, updated_at__lt=cutoff)
            )
            .select_related(
                "current_generation", "pending_generation", "image_definition"
            )
        )

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
        ).select_related("runner", "image_definition", "current_generation")

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
            ws_count = (
                Workspace.objects.filter(
                    base_image_instance=instance,
                )
                .exclude(
                    status__in=[
                        WorkspaceStatus.REMOVED,
                        WorkspaceStatus.DELETED,
                    ],
                )
                .count()
            )
            count += ws_count
        return count > 0, count


class ImageGenerationRepository:
    """Atomic generation allocation and task-bound monotonic completion."""

    RECIPE_FIELDS = (
        "runtime_type",
        "base_distro",
        "packages",
        "env_vars",
        "custom_dockerfile",
        "custom_init_script",
    )

    @staticmethod
    def request(*, definition, runner, rendered_input: dict, created_by=None):
        """Allocate a fresh generation without touching any previous image."""
        import hashlib
        import json

        from django.db.models import Max

        from common.exceptions import ConflictError

        from .models import ImageRevision

        expected_recipe = {
            key: getattr(definition, key)
            for key in ImageGenerationRepository.RECIPE_FIELDS
        }
        with transaction.atomic():
            lock_runner(runner.id)
            definition = ImageDefinition.objects.select_for_update().get(
                id=definition.id
            )
            if expected_recipe != {
                key: getattr(definition, key)
                for key in ImageGenerationRepository.RECIPE_FIELDS
            }:
                raise ConflictError("Recipe changed while rendering; retry the build")
            if definition.status not in {"active", "deactivated"}:
                raise ConflictError("Image definition is being removed")
            job, _ = ImageBuildJob.objects.get_or_create(
                image_definition=definition, runner=runner
            )
            job = ImageBuildJob.objects.select_for_update().get(id=job.id)
            if job.status in {
                "pending_deletion",
                "deleting",
                "deleted",
                "delete_failed",
            }:
                raise ConflictError("Image assignment is being removed")
            recipe = {
                key: getattr(definition, key)
                for key in ImageGenerationRepository.RECIPE_FIELDS
            }
            snapshot = {"recipe": recipe, "rendered_input": rendered_input}
            digest = hashlib.sha256(
                json.dumps(snapshot, sort_keys=True).encode()
            ).hexdigest()
            revision, _ = ImageRevision.objects.get_or_create(
                definition=definition,
                digest=digest,
                defaults={"recipe": recipe, "rendered_input": rendered_input},
            )
            number = (job.generations.aggregate(n=Max("generation"))["n"] or 0) + 1
            task = TaskRepository.create(
                task_id=uuid.uuid4(), runner=runner, task_type=TaskType.BUILD_IMAGE
            )
            image_id = uuid.uuid4()
            runner_ref = (
                f"opencuria/generations:{image_id}"
                if definition.runtime_type == "docker"
                else f"/var/lib/opencuria/base-images/{image_id}.qcow2"
            )
            image = ImageInstance.objects.create(
                id=image_id,
                runner=runner,
                runtime_type=definition.runtime_type,
                origin_type="definition_build",
                origin_definition=definition,
                build_job=job,
                revision=revision,
                generation=number,
                creating_task=task,
                created_by=created_by,
                name=f"{definition.name} ({runner.name})",
                runner_ref=runner_ref,
                status="building",
            )
            job.pending_generation = image
            job.build_task = task
            job.build_log = ""
            # Availability belongs to current; rebuild failure never removes it.
            if not job.current_generation_id:
                job.status = "pending"
            job.save()
            from .operations import OperationRepository

            payload = {
                **rendered_input,
                "task_id": str(task.id),
                "build_job_id": str(job.id),
                "image_instance_id": str(image.id),
                "runtime_type": image.runtime_type,
            }
            payload["image_tag" if image.runtime_type == "docker" else "image_path"] = (
                runner_ref
            )
            OperationRepository.prepare(str(task.id), "task:build_image", payload)
            return ImageBuildJobRepository.get_by_id(job.id), image, task

    @staticmethod
    def finish(
        *,
        task_id: str,
        build_job_id: str,
        runner_id: str | None,
        runner_ref: str = "",
        error: str | None = None,
    ) -> bool:
        """Accept only the exact live creation task and immutable runner target."""
        try:
            task_id, build_job_id = (
                uuid.UUID(str(task_id)),
                uuid.UUID(str(build_job_id)),
            )
        except (ValueError, TypeError):
            return False
        with transaction.atomic():
            identity = Task.objects.filter(id=task_id).values("runner_id").first()
            if identity is None:
                return False
            lock_runner(identity["runner_id"])
            job = (
                ImageBuildJob.objects.select_for_update()
                .filter(id=build_job_id)
                .first()
            )
            task = Task.objects.filter(id=task_id).first()
            image = (
                ImageInstance.objects.select_for_update()
                .filter(build_job_id=build_job_id, creating_task_id=task_id)
                .first()
            )
            if task:
                task = Task.objects.select_for_update().get(pk=task.id)
            if not job or not task or not image:
                return False
            if (
                task.type != TaskType.BUILD_IMAGE
                or task.runner_id != job.runner_id
                or image.runner_id != job.runner_id
                or (runner_id is not None and str(job.runner_id) != str(runner_id))
                or task.status not in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}
                or image.status
                not in [
                    ImageInstance.Status.BUILDING,
                    ImageInstance.Status.PENDING_DELETION,
                ]
            ):
                return False
            if error is None and (not runner_ref or runner_ref != image.runner_ref):
                return False
            retiring = image.status == "pending_deletion"
            image.status = (
                "pending_deletion"
                if retiring
                else ("ready" if error is None else "failed")
            )
            image.save(update_fields=["status", "updated_at"])
            if error is None:
                TaskRepository.complete(task)
            else:
                TaskRepository.fail(task, error)
            ImageGenerationRepository.reconcile_assignment(job)
            return True

    @staticmethod
    def reconcile_assignment(job: ImageBuildJob) -> None:
        """Apply the latest requested result under runner/assignment locks.

        Retirement can defer promotion beyond task completion. The durable
        build_task correlation, not pending or successful history, selects it.
        """
        image = (
            (
                ImageInstance.objects.select_for_update()
                .filter(build_job=job, creating_task_id=job.build_task_id)
                .first()
            )
            if job.build_task_id
            else None
        )
        if image is None:
            return
        task = Task.objects.select_for_update().get(pk=job.build_task_id)
        if task.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
            return
        if job.pending_generation_id == image.id:
            job.pending_generation = None
        if job.status not in {
            "pending_deletion",
            "deleting",
            "deleted",
            "delete_failed",
        }:
            if task.status == TaskStatus.COMPLETED and image.status == "ready":
                job.current_generation = image
                job.built_at = task.completed_at
                if job.status != "deactivated":
                    job.status = "active"
            elif (
                task.status == TaskStatus.FAILED
                and image.status == "failed"
                and not job.current_generation_id
                and job.status != "deactivated"
            ):
                job.status = "failed"
        job.save()

    @staticmethod
    def validate_selection(image_id: uuid.UUID) -> ImageInstance:
        """Lock and revalidate a selection in the workspace creation transaction."""
        from common.exceptions import ConflictError

        image = ImageInstance.objects.get(id=image_id)
        # Match allocation, retirement and callbacks: runner precedes graph rows.
        lock_runner(image.runner_id)
        if image.build_job_id:
            definition_id = ImageBuildJob.objects.values_list(
                "image_definition_id", flat=True
            ).get(id=image.build_job_id)
            ImageDefinition.objects.select_for_update().get(id=definition_id)
            ImageBuildJob.objects.select_for_update().get(id=image.build_job_id)
        image = ImageInstance.objects.select_for_update().get(id=image_id)
        if image.status != "ready" or not image.runner_ref:
            raise ConflictError("Selected image is no longer ready")
        if image.build_job_id:
            job = ImageBuildJob.objects.select_for_update().get(id=image.build_job_id)
            definition = ImageDefinition.objects.select_for_update().get(
                id=job.image_definition_id
            )
            if (
                job.current_generation_id != image.id
                or job.status != "active"
                or definition.status != "active"
            ):
                raise ConflictError("Selected image is no longer current")
        return image

    @staticmethod
    def progress(*, build_job_id, task_id, runner_id, line, max_chars):
        """Progress is bound to the latest live attempt, never a completed job."""
        from django.db.models.functions import Concat, Right

        try:
            job_id, task_id = uuid.UUID(str(build_job_id)), uuid.UUID(str(task_id))
        except (ValueError, TypeError):
            return
        cleaned = (line or "").replace("\x00", "").rstrip("\n")[:8000] + "\n"
        queryset = ImageBuildJob.objects.filter(
            id=job_id,
            build_task_id=task_id,
            pending_generation__creating_task_id=task_id,
            pending_generation__status="building",
            build_task__status__in=["pending", "in_progress"],
        )
        if runner_id is not None:
            queryset = queryset.filter(runner_id=runner_id)
        queryset.update(
            build_log=Right(Concat("build_log", Value(cleaned)), max_chars),
            updated_at=timezone.now(),
        )

    @staticmethod
    def capture_result(
        *, task_id, workspace_id, runner_id, runner_ref="", size_bytes=None, error=None
    ) -> bool:
        """Serialize capture callbacks against deletion and terminal task state."""
        try:
            task_id = uuid.UUID(str(task_id))
        except (TypeError, ValueError):
            return False
        with transaction.atomic():
            identity = (
                Task.objects.filter(id=task_id)
                .values("runner_id", "workspace_id")
                .first()
            )
            if identity is None:
                return None
            lock_runner(identity["runner_id"])
            if identity["workspace_id"]:
                Workspace.objects.select_for_update().get(pk=identity["workspace_id"])
            task = Task.objects.filter(id=task_id).first()
            image = (
                ImageInstance.objects.select_for_update()
                .filter(creating_task_id=task_id, origin_type="workspace_capture")
                .first()
            )
            if task:
                task = Task.objects.select_for_update().get(pk=task.id)
            if (
                not task
                or not image
                or task.type != TaskType.CREATE_IMAGE_ARTIFACT
                or str(task.workspace_id) != str(workspace_id)
                or task.runner_id != image.runner_id
                or (runner_id is not None and str(task.runner_id) != str(runner_id))
                or task.status not in {"pending", "in_progress"}
                or image.status not in ["capturing", "pending_deletion"]
            ):
                return False
            if error is None:
                if not runner_ref or (size_bytes is not None and size_bytes < 0):
                    return False
                image.runner_ref = runner_ref
                image.size_bytes = size_bytes
                image.status = (
                    "pending_deletion"
                    if image.status == "pending_deletion"
                    else "ready"
                )
                image.save(update_fields=["runner_ref", "size_bytes", "status"])
                TaskRepository.complete(task)
            else:
                image.status = (
                    "pending_deletion"
                    if image.status == "pending_deletion"
                    else "failed"
                )
                image.save(update_fields=["status"])
                TaskRepository.fail(task, error)
            if task.workspace:
                WorkspaceRepository.update_active_operation(task.workspace, None)
            return True

    @staticmethod
    def delete_result(
        *, task_id, runner_id, image_id="", runner_ref="", error=None
    ) -> ImageInstance | None:
        """Never correlate deletion by naked runner ref or a creation task."""
        try:
            task_id = uuid.UUID(str(task_id))
        except (TypeError, ValueError):
            return None
        with transaction.atomic():
            identity = (
                Task.objects.filter(id=task_id)
                .values("runner_id", "workspace_id")
                .first()
            )
            if identity is None:
                return None
            lock_runner(identity["runner_id"])
            if identity["workspace_id"]:
                Workspace.objects.select_for_update().get(pk=identity["workspace_id"])
            task = Task.objects.filter(id=task_id).first()
            image = (
                ImageInstance.objects.select_for_update()
                .filter(
                    deleting_task_id=task_id,
                    status__in=["deleting", "pending_deletion"],
                )
                .first()
            )
            if task:
                task = Task.objects.select_for_update().get(pk=task.id)
            if (
                not task
                or not image
                or task.type != TaskType.DELETE_IMAGE
                or task.runner_id != image.runner_id
                or task.status not in {"pending", "in_progress"}
                or (runner_id is not None and str(task.runner_id) != str(runner_id))
                or (image_id and str(image.id) != str(image_id))
                or (runner_ref and runner_ref != image.runner_ref)
            ):
                return None
            if error is None:
                ImageInstanceRepository.mark_deleted(image.id)
                TaskRepository.complete(task)
            else:
                ImageInstanceRepository.mark_delete_failed(image.id, error=error)
                TaskRepository.fail(task, error)
            return image
