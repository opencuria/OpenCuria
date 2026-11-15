"""Shared assignment projection for build results and lifecycle interruption."""

from .enums import TaskStatus
from .models import ImageBuildJob, ImageInstance, Task


class ImageAssignmentRepository:
    """Project the latest requested generation under runner/assignment locks."""

    @staticmethod
    def reconcile(job: ImageBuildJob) -> None:
        """Reconcile terminal execution, retaining current images and delete intents.

        Retirement may defer promotion beyond task completion. The durable
        build_task correlation selects the result, independent of older builds.
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
                and (
                    not job.current_generation_id
                    or job.current_generation.status != "ready"
                )
                and job.status != "deactivated"
            ):
                job.status = "failed"
        job.save()
