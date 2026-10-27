"""Declare runtime artifacts, model exposure and managed MCP lifecycle."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("plugins", "0010_seed_opencuria_plugin")]
    operations = [
        migrations.AddField(
            "pluginmcpserver", "tool_policy", models.JSONField(default=dict, blank=True)
        ),
        migrations.AddField(
            "pluginmcpserver",
            "runtime_artifact",
            models.JSONField(default=dict, blank=True),
        ),
    ]
