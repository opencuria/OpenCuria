"""Add manual_unread_at to HarnessSession for explicit unread marking."""

from django.db import migrations, models


class Migration(migrations.Migration):
    """Persist when the user explicitly marked a session unread."""

    dependencies = [
        ("harness", "0010_harnessmessage_reasoning_effort"),
    ]

    operations = [
        migrations.AddField(
            model_name="harnesssession",
            name="manual_unread_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the user explicitly marked this session unread.",
                null=True,
            ),
        ),
    ]
