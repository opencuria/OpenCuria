"""Fence legacy non-journaled credential tasks for authenticated upgrade recovery."""

from django.db import transaction

from .locking import lock_runner
from .models import LifecycleCommand, Task, Workspace
from .operations import OperationRepository
from .workspace_operation_repository import WorkspaceOperationRepository


class LegacyCredentialSyncRepository:
    """Convert one orphaned legacy write per idle workspace into an inspection."""

    @staticmethod
    def fence() -> None:
        """Never replay legacy secrets; retain ownership until a new runner seals it."""
        identities = list(
            Task.objects.filter(
                type="inject_credentials",
                status__in=["pending", "in_progress"],
                lifecyclecommand__isnull=True,
                workspace__isnull=False,
                runner__status="online",
            ).values("id", "runner_id", "workspace_id")
        )
        for identity in identities:
            with transaction.atomic():
                runner = lock_runner(identity["runner_id"])
                workspace = Workspace.objects.select_for_update().get(
                    pk=identity["workspace_id"]
                )
                if not runner.sid or workspace.current_task_id:
                    continue
                if WorkspaceOperationRepository.capture_active(
                    workspace.id
                ) or WorkspaceOperationRepository.recreate_active(workspace.id):
                    continue
                task = Task.objects.select_for_update().get(pk=identity["id"])
                if task.status not in {"pending", "in_progress"} or (
                    LifecycleCommand.objects.filter(task=task).exists()
                ):
                    continue
                OperationRepository.allocate(task)
                LifecycleCommand.objects.filter(task=task).update(
                    event="task:inject_credentials",
                    target=str(workspace.id),
                    payload={
                        "task_id": str(task.id),
                        "workspace_id": str(workspace.id),
                        "_legacy_credential_sync": True,
                    },
                    deliveries=1,
                )
                OperationRepository.intervene(
                    task,
                    "Interrupted legacy credential sync; waiting for an upgraded "
                    "runner to verify and seal delayed credential delivery.",
                )
