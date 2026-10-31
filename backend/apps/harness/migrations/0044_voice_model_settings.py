"""Persist speech and dictation-cleanup models with provider defaults."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("harness", "0043_remove_agent_s_part_type")]

    operations = [
        migrations.AddField(
            model_name="providerconfig",
            name="speech_model",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                help_text="Speech-to-text model; empty uses the speech default.",
            ),
        ),
        migrations.AddField(
            model_name="providerconfig",
            name="voice_cleanup_model",
            field=models.CharField(
                blank=True,
                default="",
                max_length=255,
                help_text="Dictation cleanup model; empty inherits the small or Build model.",
            ),
        ),
    ]
