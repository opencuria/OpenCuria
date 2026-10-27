"""Restart recovery coordinating the schedule ledger and harness-owned turns."""

from __future__ import annotations

from django.utils import timezone

from apps.harness.run_recovery_service import RunRecoveryService

from .models import ScheduledTaskRun
from .repositories import ScheduledTaskRepository


class ScheduledTaskRecoveryService:
    """Settle lost occurrences while keeping harness cleanup in its context."""

    def __init__(
        self,
        repository: type[ScheduledTaskRepository] | None = None,
        harness: RunRecoveryService | None = None,
    ) -> None:
        self.repository = repository or ScheduledTaskRepository
        self.harness = harness or RunRecoveryService()

    def recover_backend_restart(self) -> int:
        """Recover active ledger rows and their turns in one locked transaction."""
        now = timezone.now()
        error = "Backend restarted before the run completed"
        recovered = 0
        with self.repository.restart_runs() as runs:
            for run in runs:
                assistant = self.harness.find_assistant(
                    run.session_id, run.assistant_message
                )
                completed = assistant is not None and assistant.completed_at is not None
                fields: dict[str, object] = {"assistant_message": assistant}
                if completed:
                    fields.update(
                        status=(
                            ScheduledTaskRun.Status.ERROR
                            if assistant.error or assistant.finish == "error"
                            else ScheduledTaskRun.Status.SUCCEEDED
                        ),
                        finished_at=assistant.completed_at,
                        error=assistant.error or "",
                    )
                else:
                    fields.update(
                        status=ScheduledTaskRun.Status.INTERRUPTED,
                        finished_at=now,
                        error=error,
                        reason="backend_restart",
                    )
                self.repository.update_run(run, **fields)
                recovered += 1
                if not completed and run.session_id is not None:
                    self.harness.interrupt_turn(
                        run.session_id,
                        assistant.id if assistant is not None else None,
                        at=now,
                        error=error,
                    )
        return recovered
