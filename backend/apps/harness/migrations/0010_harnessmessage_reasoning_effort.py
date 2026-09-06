"""Add reasoning_effort snapshot to HarnessMessage."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Store the effort used for each assistant turn."""

    dependencies = [
        ("harness", "0009_harnesspart_compaction_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnessmessage",
            name="reasoning_effort",
            field=models.CharField(
                blank=True,
                default="",
                help_text="OpenRouter reasoning effort snapshotted for this turn.",
                max_length=16,
            ),
        ),
    ]
