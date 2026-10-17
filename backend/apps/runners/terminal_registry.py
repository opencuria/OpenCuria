"""Durable routing records for interactive terminal PTY sessions."""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction

from .enums import TaskStatus, TaskType
from .locking import lock_runner
from .models import Task, TerminalSession, Workspace


class TerminalSessionRepository:
    """Persist terminal identity and runner ownership across API processes."""

    @staticmethod
    def claim(workspace: Workspace) -> tuple[TerminalSession, Task | None, bool]:
        """Return the active terminal or atomically reserve one with a task."""
        try:
            return TerminalSessionRepository._claim_locked(workspace)
        except IntegrityError:
            return TerminalSessionRepository._active_claim(workspace)

    @staticmethod
    def _active_claim(
        workspace: Workspace,
    ) -> tuple[TerminalSession, Task | None, bool]:
        """Return a session that won a concurrent database uniqueness race."""
        active = TerminalSession.objects.filter(
            workspace=workspace,
            status__in=[
                TerminalSession.Status.STARTING,
                TerminalSession.Status.ACTIVE,
            ],
        ).first()
        if active is None:
            raise IntegrityError("Active terminal uniqueness race lost without winner")
        task = (
            Task.objects.filter(pk=active.task_id).first() if active.task_id else None
        )
        return active, task, False

    @staticmethod
    def _claim_locked(
        workspace: Workspace,
    ) -> tuple[TerminalSession, Task | None, bool]:
        """Create a session under the workspace/runner lifecycle locks."""
        with transaction.atomic():
            lock_runner(workspace.runner_id)
            locked_workspace = Workspace.objects.select_for_update().get(
                pk=workspace.pk
            )
            TerminalSession.objects.filter(
                workspace=locked_workspace,
                status=TerminalSession.Status.CLOSED,
            ).delete()
            current = (
                TerminalSession.objects.select_for_update()
                .filter(workspace=locked_workspace)
                .exclude(status=TerminalSession.Status.CLOSED)
                .order_by("-created_at")
                .first()
            )
            if current is not None:
                task = (
                    Task.objects.filter(pk=current.task_id).first()
                    if current.task_id
                    else None
                )
                return current, task, False

            task = Task.objects.create(
                id=uuid.uuid4(),
                runner_id=locked_workspace.runner_id,
                workspace=locked_workspace,
                type=TaskType.START_TERMINAL,
                status=TaskStatus.PENDING,
            )
            session = TerminalSession.objects.create(
                terminal_id=uuid.uuid4(),
                workspace=locked_workspace,
                runner_id=locked_workspace.runner_id,
                task=task,
                status=TerminalSession.Status.STARTING,
            )
            return session, task, True

    @staticmethod
    def get_active(
        workspace_id: uuid.UUID, terminal_id: uuid.UUID | str | None = None
    ) -> TerminalSession | None:
        """Find a starting or active terminal, optionally by its exact ID."""
        query = TerminalSession.objects.filter(workspace_id=workspace_id).exclude(
            status=TerminalSession.Status.CLOSED
        )
        if terminal_id is not None:
            query = query.filter(terminal_id=terminal_id)
        return query.select_related("runner").first()

    @staticmethod
    def mark_started(
        *,
        workspace_id: str,
        terminal_id: str,
        runner_id: str,
    ) -> bool:
        """Mark the exact runner-owned terminal active after its PTY exists."""
        return bool(
            TerminalSession.objects.filter(
                workspace_id=workspace_id,
                terminal_id=terminal_id,
                runner_id=runner_id,
                status__in=[
                    TerminalSession.Status.STARTING,
                    TerminalSession.Status.ACTIVE,
                ],
            ).update(status=TerminalSession.Status.ACTIVE)
        )

    @staticmethod
    def record_output(
        *, workspace_id: str, terminal_id: str, runner_id: str, sequence: int
    ) -> str:
        """Advance output high-water metadata for an owned live terminal.

        Output bytes intentionally remain in the runner's bounded ring buffer.
        Returns accepted, duplicate, gap or unknown; replayed duplicates may
        still be forwarded to clients whose cursor is behind this watermark.
        """
        if type(sequence) is not int or sequence < 1:
            return "invalid"
        with transaction.atomic():
            session = (
                TerminalSession.objects.select_for_update()
                .filter(
                    workspace_id=workspace_id,
                    terminal_id=terminal_id,
                    runner_id=runner_id,
                    status__in=[
                        TerminalSession.Status.STARTING,
                        TerminalSession.Status.ACTIVE,
                    ],
                )
                .first()
            )
            if session is None:
                return "unknown"
            if sequence <= session.last_sequence:
                return "duplicate"
            if sequence != session.last_sequence + 1:
                return "gap"
            session.last_sequence = sequence
            session.save(update_fields=["last_sequence", "updated_at"])
            return "accepted"

    @staticmethod
    def close(*, workspace_id: str, terminal_id: str, runner_id: str) -> bool:
        """Close only the matching terminal and runner generation route."""
        updated = (
            TerminalSession.objects.filter(
                workspace_id=workspace_id,
                terminal_id=terminal_id,
                runner_id=runner_id,
            )
            .exclude(status=TerminalSession.Status.CLOSED)
            .update(status=TerminalSession.Status.CLOSED)
        )
        return bool(updated)
