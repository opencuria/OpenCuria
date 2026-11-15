"""Index session-scoped permission status reads in chronological order."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add a portable lookup index without changing persisted gate records."""

    dependencies = [
        ("harness", "0040_remove_codex_turn_diff_snapshots"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="permissionrequest",
            index=models.Index(
                fields=["session_id", "status", "created_at"],
                name="harness_perm_session_state_idx",
            ),
        ),
    ]
