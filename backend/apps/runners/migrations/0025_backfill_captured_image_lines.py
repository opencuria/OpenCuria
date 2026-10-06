"""Attach existing captures to version lines, then enforce versioned captures."""

import uuid

from django.db import migrations, models


def backfill(apps, schema_editor):
    """Give every existing capture its own line (v1) and persist create repos."""
    Image = apps.get_model("runners", "ImageInstance")
    Line = apps.get_model("runners", "CapturedImage")
    Workspace = apps.get_model("runners", "Workspace")
    Command = apps.get_model("runners", "LifecycleCommand")
    captures = Image.objects.filter(
        origin_type="workspace_capture", captured_image__isnull=True
    ).select_related("runner", "origin_workspace")
    for image in captures:
        line = Line.objects.create(
            id=uuid.uuid4(),
            organization_id=image.runner.organization_id,
            runner_id=image.runner_id,
            created_by_id=image.created_by_id,
            name=image.name,
            status="deleted" if image.status == "deleted" else "active",
        )
        source = image.origin_workspace
        Image.objects.filter(pk=image.pk).update(
            captured_image=line,
            generation=1,
            min_disk_size_gb=source.qemu_disk_size_gb if source else None,
        )
    for command in Command.objects.filter(
        task__type="create_workspace", task__workspace__isnull=False
    ).select_related("task"):
        repos = (command.payload or {}).get("repos") or []
        if repos:
            Workspace.objects.filter(pk=command.task.workspace_id).update(repos=repos)


class Migration(migrations.Migration):
    dependencies = [("runners", "0024_versioned_images")]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="imageinstance",
            constraint=models.UniqueConstraint(
                fields=("captured_image", "generation"),
                name="unique_captured_image_version",
            ),
        ),
        migrations.AddConstraint(
            model_name="imageinstance",
            constraint=models.CheckConstraint(
                condition=models.Q(is_legacy=True)
                | models.Q(
                    origin_type="definition_build",
                    origin_definition__isnull=False,
                    build_job__isnull=False,
                    revision__isnull=False,
                    generation__gte=1,
                    generation__isnull=False,
                    origin_workspace__isnull=True,
                    captured_image__isnull=True,
                )
                | models.Q(
                    origin_type="workspace_capture",
                    origin_definition__isnull=True,
                    build_job__isnull=True,
                    revision__isnull=True,
                    captured_image__isnull=False,
                    generation__gte=1,
                    generation__isnull=False,
                    runtime_type="qemu",
                ),
                name="image_origin_valid",
            ),
        ),
    ]
