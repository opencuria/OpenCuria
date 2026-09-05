"""Add skill_ids JSON field to HarnessSession."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Persist selected skills on harness sessions."""

    dependencies = [
        ("harness", "0003_harnessmessage_harnesspart_harnesssession_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnesssession",
            name="skill_ids",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Selected skill UUIDs persisted for subsequent runs.",
            ),
        ),
    ]
