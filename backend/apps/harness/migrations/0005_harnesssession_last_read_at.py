"""Add last_read_at to HarnessSession for dashboard unread tracking."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Persist when the user last opened a harness session."""

    dependencies = [
        ("harness", "0004_harnesssession_skill_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnesssession",
            name="last_read_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the user last opened this session (null = never read).",
                null=True,
            ),
        ),
    ]
