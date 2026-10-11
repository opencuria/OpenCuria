"""Sanitized lifecycle records for runner activity presentation."""

from .deletion_repository import DeletionRepository
from .models import CaptureRequest, LifecycleCommand, Runner, WorkspaceRecreateRequest


class ActivityRepository:
    """Read workflow relationships without exposing command inputs or secrets."""

    @staticmethod
    def records(runner: Runner) -> dict:
        """Return execution facts and their enclosing workflows for one runner."""
        return {
            "operations": list(
                LifecycleCommand.objects.filter(task__runner=runner)
                .exclude(task__status="completed")
                .order_by("-created_at")
                .values(
                    "task_id",
                    "task__type",
                    "task__status",
                    "task__error",
                    "task__workspace_id",
                    "task__workspace__name",
                    "task__workspace__current_task_id",
                    "phase",
                    "target",
                    "heartbeat_at",
                    "deadline_at",
                    "deliveries",
                    "created_at",
                )
            ),
            "capture_requests": list(
                CaptureRequest.objects.filter(workspace__runner=runner)
                .order_by("-created_at")
                .values(
                    "id",
                    "workspace_id",
                    "workspace__name",
                    "image_id",
                    "image__name",
                    "phase",
                    "child_id",
                    "image__creating_task_id",
                    "prior_running",
                    "resume_suppressed",
                    "diagnostic",
                    "created_at",
                )
            ),
            "recreate_requests": list(
                WorkspaceRecreateRequest.objects.filter(
                    workspace__runner=runner
                ).values(
                    "id",
                    "workspace_id",
                    "workspace__name",
                    "phase",
                    "reason",
                    "child_id",
                    "diagnostic",
                    "created_at",
                )
            ),
            "deletions": [
                request
                for request in DeletionRepository.list(runner.organization_id)
                if any(
                    item["runner_id"] == str(runner.id)
                    for item in (
                        request["approval"].get("images", [])
                        + request["approval"].get("workspaces", [])
                    )
                )
            ],
        }
