"""Repair build assignments left pending after terminal interrupted tasks."""

from typing import Any

from django.db import migrations
from django.utils import timezone


def reconcile_interrupted_builds(apps: Any, schema_editor: Any) -> None:
    """Repair metadata only, preserving current images and deletion intents."""
    alias = schema_editor.connection.alias
    ImageBuildJob = apps.get_model("runners", "ImageBuildJob")
    ImageInstance = apps.get_model("runners", "ImageInstance")
    ImageInstance.objects.using(alias).filter(
        origin_type="definition_build",
        creating_task__status="failed",
        status__in=["creating", "building"],
    ).update(status="failed", updated_at=timezone.now())
    jobs = ImageBuildJob.objects.using(alias).filter(build_task__status="failed")
    for job in jobs.iterator():
        image = (
            ImageInstance.objects.using(alias)
            .filter(build_job_id=job.id, creating_task_id=job.build_task_id)
            .first()
        )
        if image is None:
            continue
        changes = {}
        if job.pending_generation_id == image.id:
            changes["pending_generation_id"] = None
        if (
            job.status == "pending"
            and not job.current_generation_id
            and image.status == "failed"
        ):
            changes["status"] = "failed"
        if changes:
            ImageBuildJob.objects.using(alias).filter(pk=job.id).update(**changes)


class Migration(migrations.Migration):
    dependencies = [("runners", "0029_workspace_disk_usage")]

    operations = [
        migrations.RunPython(reconcile_interrupted_builds, migrations.RunPython.noop),
    ]
