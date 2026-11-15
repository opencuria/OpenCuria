"""Retain useful desktop settings while removing the Agent-S configuration."""

import django.db.models.deletion
from django.db import migrations, models


def normalize_limits(apps, schema_editor):
    config = apps.get_model("harness", "ComputerUseConfig")
    config.objects.filter(screenshot_max_dimension__lt=320).update(
        screenshot_max_dimension=320
    )
    config.objects.filter(max_steps__gt=1000).update(max_steps=1000)


class Migration(migrations.Migration):
    dependencies = [("harness", "0041_permission_request_lookup_index")]

    operations = [
        migrations.RenameModel("AgentSConfig", "ComputerUseConfig"),
        migrations.AlterModelTable("computeruseconfig", "harness_computer_use_config"),
        migrations.AlterModelOptions("computeruseconfig", options={}),
        migrations.AlterField(
            model_name="computeruseconfig",
            name="organization",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="harness_computer_use_config",
                to="organizations.organization",
            ),
        ),
        migrations.AlterField(
            "computeruseconfig", "max_steps", models.PositiveIntegerField(default=15)
        ),
        migrations.AlterField(
            "computeruseconfig", "enable_recording", models.BooleanField(default=False)
        ),
        migrations.AlterField(
            "computeruseconfig",
            "screenshot_max_dimension",
            models.PositiveIntegerField(default=2400),
        ),
        migrations.RunPython(normalize_limits, migrations.RunPython.noop),
        *[
            migrations.RemoveField("computeruseconfig", name)
            for name in (
                "grounding_model",
                "grounding_width",
                "grounding_height",
                "model_temperature",
                "max_trajectory_length",
                "enable_reflection",
                "enable_code_agent",
                "action_pre_delay",
                "action_post_delay",
                "wait_delay",
            )
        ],
    ]
