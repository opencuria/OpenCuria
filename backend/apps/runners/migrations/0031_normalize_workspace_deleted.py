"""Consolidate the legacy removed status into the deletion tombstone."""

from typing import Any

from django.db import migrations, models


def normalize_removed(apps: Any, schema_editor: Any) -> None:
    """Preserve physical resources; only normalize the legacy terminal label."""
    Workspace = apps.get_model("runners", "Workspace")
    Workspace.objects.using(schema_editor.connection.alias).filter(
        status="removed"
    ).update(status="deleted")


class Migration(migrations.Migration):
    dependencies = [("runners", "0030_reconcile_interrupted_builds")]

    operations = [
        migrations.RunPython(normalize_removed, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="workspace",
            name="status",
            field=models.CharField(
                choices=[
                    ("creating", "Creating"),
                    ("running", "Running"),
                    ("stopped", "Stopped"),
                    ("failed", "Failed"),
                    ("pending_deletion", "Pending Deletion"),
                    ("deleting", "Deleting"),
                    ("deleted", "Deleted"),
                    ("delete_failed", "Delete Failed"),
                ],
                default="creating",
                max_length=20,
            ),
        ),
    ]
