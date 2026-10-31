"""Expose backend desktop control without widening customized tool allowlists."""

from django.db import migrations

PLUGIN_ID = "7ca41130-a36a-491d-83fa-cd912bcaaf52"


def upgrade(apps, schema_editor):
    servers = apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin_id=PLUGIN_ID, slug="cua_repl"
    )
    for server in servers:
        policy = dict(server.tool_policy or {})
        if policy.get("exposed_tools") != ["js", "js_reset"]:
            continue
        if "desktop" in policy.get("internal_tools", []):
            continue
        policy["exposed_tools"] = ["js", "js_reset", "desktop"]
        policy["execution_tools"] = list(
            dict.fromkeys([*policy.get("execution_tools", []), "desktop"])
        )
        server.tool_policy = policy
        server.save(update_fields=["tool_policy"])


def downgrade(apps, schema_editor):
    servers = apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin_id=PLUGIN_ID, slug="cua_repl"
    )
    for server in servers:
        policy = dict(server.tool_policy or {})
        if policy.get("exposed_tools") != ["js", "js_reset", "desktop"]:
            continue
        policy["exposed_tools"] = ["js", "js_reset"]
        policy["execution_tools"] = [
            name for name in policy.get("execution_tools", []) if name != "desktop"
        ]
        server.tool_policy = policy
        server.save(update_fields=["tool_policy"])


class Migration(migrations.Migration):
    dependencies = [("plugins", "0013_computer_use_compatibility")]
    operations = [migrations.RunPython(upgrade, downgrade)]
