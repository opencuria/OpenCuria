"""Add computer_use_model to ProviderConfig."""

from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    """Add optional computer-use model override."""

    dependencies = [
        ("harness", "0007_harnesssession_reasoning_effort"),
    ]

    operations = [
        migrations.AddField(
            model_name="providerconfig",
            name="computer_use_model",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Model for the computeruse subagent; falls back to default_model.",
                max_length=255,
            ),
        ),
    ]
