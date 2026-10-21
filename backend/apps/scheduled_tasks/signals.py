"""Harness run-event receivers for scheduled-task history."""

from __future__ import annotations

import uuid

from django.dispatch import receiver

from apps.harness.run_events import assistant_run_finished

from .services import ScheduledTaskService


@receiver(assistant_run_finished)
def settle_scheduled_task_runs(sender, message_id: uuid.UUID, **kwargs) -> None:
    """Settle linked task runs as soon as their harness assistant finishes.

    History must settle even when the scheduler is not running; a missed
    event is repaired by ``reconcile_runs`` and ``list_runs``.
    """
    ScheduledTaskService().complete_assistant_run(message_id)
