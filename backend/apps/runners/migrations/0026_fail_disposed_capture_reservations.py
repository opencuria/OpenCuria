"""Close unstarted image reservations whose stop was explicitly disposed."""

from typing import Any

from django.db import migrations
from django.utils import timezone


def close_reservations(apps: Any, schema_editor: Any) -> None:
    """Repair metadata only; retain disks and every completed image version."""
    alias = schema_editor.connection.alias
    CaptureRequest = apps.get_model("runners", "CaptureRequest")
    ImageInstance = apps.get_model("runners", "ImageInstance")
    image_ids = (
        CaptureRequest.objects.using(alias)
        .filter(
            phase="failed",
            child__type="stop_workspace",
            child__status="failed",
            child__lifecyclecommand__phase="disposed",
        )
        .values_list("image_id", flat=True)
    )
    ImageInstance.objects.using(alias).filter(
        pk__in=image_ids,
        origin_type="workspace_capture",
        status="capturing",
        creating_task__isnull=True,
        runner_ref="",
    ).update(status="failed", updated_at=timezone.now())


class Migration(migrations.Migration):
    dependencies = [("runners", "0025_backfill_captured_image_lines")]

    operations = [
        migrations.RunPython(close_reservations, migrations.RunPython.noop),
    ]
