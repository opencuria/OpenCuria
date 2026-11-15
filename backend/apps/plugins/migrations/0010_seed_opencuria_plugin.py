"""Seed the global OpenCuria plugin (built-in MCP server, explicit opt-in)."""

from __future__ import annotations

import uuid

from django.db import migrations, models

OPENCURIA_PLUGIN_ID = uuid.UUID("128eb61c-0bc6-4c64-8a1b-2c9dc4e440b6")
OPENCURIA_PLUGIN_SLUG = "opencuria"


def seed_opencuria_plugin(apps, schema_editor):
    plugin_model = apps.get_model("plugins", "Plugin")
    server_model = apps.get_model("plugins", "PluginMcpServer")
    plugin, _ = plugin_model.objects.update_or_create(
        id=OPENCURIA_PLUGIN_ID,
        defaults={
            "organization": None,
            "name": "OpenCuria",
            "slug": OPENCURIA_PLUGIN_SLUG,
            "description": (
                "Lets the agent manage OpenCuria itself through its MCP server: "
                "workspaces, chats, skills, plugins and scheduled tasks. The "
                "agent acts as the workspace owner."
            ),
            "enabled": True,
            "published": True,
        },
    )
    server_model.objects.update_or_create(
        plugin=plugin,
        slug="mcp",
        defaults={
            "name": "OpenCuria MCP",
            "transport": "builtin",
            "command": "",
            "args": [],
            "cwd": "/workspace",
            "env": {},
            "url": "",
            "headers": {},
            "auth_type": "none",
            "oauth_requirement_key": "",
            "resources": {},
            "startup_timeout_seconds": 30,
            # chat tools may wait up to 120 s for an agent reply.
            "request_timeout_seconds": 180,
        },
    )
    # Explicit opt-in: never create OrgPluginActivation rows here.


def unseed_opencuria_plugin(apps, schema_editor):
    apps.get_model("plugins", "Plugin").objects.filter(id=OPENCURIA_PLUGIN_ID).delete()


class Migration(migrations.Migration):
    dependencies = [("plugins", "0009_managed_desktop_resources")]

    operations = [
        migrations.AlterField(
            model_name="pluginmcpserver",
            name="transport",
            field=models.CharField(
                choices=[
                    ("stdio", "STDIO"),
                    ("streamable_http", "Streamable HTTP"),
                    ("sse", "SSE"),
                    ("builtin", "Built-in"),
                ],
                default="stdio",
                max_length=20,
            ),
        ),
        migrations.RunPython(seed_opencuria_plugin, unseed_opencuria_plugin),
    ]
