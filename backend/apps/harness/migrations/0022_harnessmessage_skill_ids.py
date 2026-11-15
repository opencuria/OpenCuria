"""Add skill_ids JSON field to HarnessMessage."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Persist active skills per user message."""

    dependencies = [
        ("harness", "0021_harnessmessage_notice_dismissed_at"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnessmessage",
            name="skill_ids",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Skill UUIDs active for this user turn.",
            ),
        ),
    ]
