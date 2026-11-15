"""Process composition for execution and its supervised application jobs.

The harness owns command execution. Application wiring belongs here so it never
imports scheduled-task or notification implementations built on harness events.
"""

from apps.harness.execution_worker import ExecutionWorker
from apps.harness.harness_service import HarnessService
from apps.harness.recovery_worker import HarnessRecoveryWorker
from apps.notifications.worker import NotificationWorker
from apps.scheduled_tasks.services import ScheduledTaskScheduler, ScheduledTaskService


def create_execution_worker(service: HarnessService) -> ExecutionWorker:
    """Bind explicit lifecycle ports at the process composition boundary."""
    return ExecutionWorker(
        service,
        scheduler=ScheduledTaskScheduler(service=ScheduledTaskService(harness=service)),
        notifications=NotificationWorker(),
        recovery=HarnessRecoveryWorker(),
    )
