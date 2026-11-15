"""Upgrade the immutable CUA runtime and expose it through native Codex MCP."""

from django.db import migrations


def upgrade(apps, schema_editor):
    servers = apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin_id="7ca41130-a36a-491d-83fa-cd912bcaaf52", slug="cua_repl"
    )
    for server in servers:
        server.runtime_artifact = {"id": "computer-use", "version": "0.2.0"}
        server.tool_policy = {
            **(server.tool_policy or {}),
            "omit_tools_from": ["code_mode", "deferred"],
        }
        server.save(update_fields=["runtime_artifact", "tool_policy"])


def downgrade(apps, schema_editor):
    servers = apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin_id="7ca41130-a36a-491d-83fa-cd912bcaaf52", slug="cua_repl"
    )
    for server in servers:
        server.runtime_artifact = {"id": "computer-use", "version": "0.1.0"}
        server.tool_policy = dict(server.tool_policy or {})
        server.tool_policy.pop("omit_tools_from", None)
        server.save(update_fields=["runtime_artifact", "tool_policy"])


class Migration(migrations.Migration):
    dependencies = [("plugins", "0012_seed_computer_use")]
    operations = [migrations.RunPython(upgrade, downgrade)]
