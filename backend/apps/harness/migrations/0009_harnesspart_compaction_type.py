"""Add compaction harness part type."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Allow persisted compaction summary parts."""

    dependencies = [
        ("harness", "0008_providerconfig_computer_use_model"),
    ]

    operations = [
        migrations.AlterField(
            model_name="harnesspart",
            name="type",
            field=models.CharField(
                choices=[
                    ("text", "Text"),
                    ("reasoning", "Reasoning"),
                    ("tool", "Tool"),
                    ("step-start", "Step start"),
                    ("step-finish", "Step finish"),
                    ("subtask", "Subtask"),
                    ("patch", "Patch"),
                    ("agent", "Agent"),
                    ("compaction", "Compaction"),
                ],
                max_length=16,
            ),
        ),
    ]
