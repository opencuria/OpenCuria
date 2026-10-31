"""Workspace process persistence, including capture fences and restart writes.

Process identity precedence and session visibility belong to ProcessLookupService.
This repository owns queries and atomic writes under the runner-first lock.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone

from common.exceptions import ConflictError

from .enums import ProcessStatus, WorkspaceStatus
from .exceptions import WorkspaceStateError
from .locking import lock_runner
from .models import Workspace, WorkspaceProcess


class WorkspaceProcessRepository:
    """Data access for WorkspaceProcess records."""

    @staticmethod
    def snapshot_definitions(workspace_id: uuid.UUID) -> list[dict[str, str]]:
        """Snapshot persistent start configuration, never session or runtime state."""
        return list(
            WorkspaceProcess.objects.filter(
                workspace_id=workspace_id, kind="persistent"
            )
            .order_by("name")
            .values("name", "command", "workdir")
        )

    @staticmethod
    def restore_definitions(
        workspace: Workspace,
        definitions: list[dict[str, str]],
        *,
        created_by: Any = None,
    ) -> None:
        """Create never-started processes in a new workspace from an image snapshot."""
        WorkspaceProcess.objects.bulk_create(
            [
                WorkspaceProcess(
                    workspace=workspace,
                    name=definition["name"],
                    command=definition["command"],
                    workdir=definition["workdir"],
                    kind="persistent",
                    status=ProcessStatus.EXITED,
                    run_count=0,
                    created_by=created_by,
                )
                for definition in definitions
            ]
        )

    @staticmethod
    def _lock_definition_workspace(
        workspace_id: uuid.UUID, *, require_running: bool = True
    ) -> Workspace:
        """Fence definition writes against capture using the same lock order."""
        runner_id = Workspace.objects.values_list("runner_id", flat=True).get(
            pk=workspace_id
        )
        lock_runner(runner_id)
        workspace = Workspace.objects.select_for_update().get(pk=workspace_id)
        if workspace.active_operation or workspace.current_task_id:
            raise ConflictError("Workspace lifecycle outcome unresolved")
        if require_running and workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError("Workspace must be running to start processes")
        return workspace

    @staticmethod
    def create(
        *,
        process_id: uuid.UUID,
        workspace: Workspace,
        command: str,
        workdir: str = "/workspace",
        name: str = "",
        created_by: Any = None,
        session_id: uuid.UUID | None = None,
        run_count: int = 1,
        kind: str = "persistent",
    ) -> WorkspaceProcess:
        """Create a new process record in running state."""
        with transaction.atomic():
            WorkspaceProcessRepository._lock_definition_workspace(workspace.id)
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
    def update_for_restart(
        process_id: uuid.UUID,
        *,
        command: str,
        workdir: str,
        log_path: str = "",
        run_count: int = 1,
        created_by: Any = None,
        session_id: uuid.UUID | None = None,
    ) -> int:
        """Reset a row for a new run on the same process id.

        Keeps ``name`` unchanged; overwrites command/workdir, resets
        lifecycle to RUNNING, clears pid/exit/ended_at, sets log path,
        run count and a fresh started_at (auto_now_add needs explicit set).
        Returns rows updated.
        """
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
        with transaction.atomic():
            workspace_id = WorkspaceProcess.objects.values_list(
                "workspace_id", flat=True
            ).get(pk=process_id)
            WorkspaceProcessRepository._lock_definition_workspace(workspace_id)
            process = WorkspaceProcess.objects.select_for_update().get(pk=process_id)
            if (
                process.status == ProcessStatus.RUNNING
                or run_count != max(process.run_count or 0, 0) + 1
            ):
                raise ConflictError(
                    "Process changed during start; refresh and try again"
                )
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
        ended_at: datetime | None = None,
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
        with transaction.atomic():
            WorkspaceProcessRepository._lock_definition_workspace(
                workspace_id, require_running=False
            )
            deleted, _ = WorkspaceProcess.objects.filter(
                id=process_id, workspace_id=workspace_id
            ).delete()
            return deleted
