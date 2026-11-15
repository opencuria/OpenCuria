"""Mention app publishing in the seeded OpenCuria plugin's description."""

from __future__ import annotations

import uuid

from django.db import migrations

OPENCURIA_PLUGIN_ID = uuid.UUID("128eb61c-0bc6-4c64-8a1b-2c9dc4e440b6")

PREVIOUS = (
    "Lets the agent manage OpenCuria itself through its MCP server: "
    "workspaces, chats, skills, plugins and scheduled tasks. The "
    "agent acts as the workspace owner."
)
CURRENT = (
    "Lets the agent manage OpenCuria itself through its MCP server: "
    "workspaces, chats, skills, plugins and scheduled tasks, and send Expo "
    "apps it builds to the user's OpenCuria mobile app. The agent acts as "
    "the workspace owner."
)


def _describe(description: str):
    def apply(apps, schema_editor):
        apps.get_model("plugins", "Plugin").objects.filter(
            id=OPENCURIA_PLUGIN_ID
        ).update(description=description)

    return apply


class Migration(migrations.Migration):
    dependencies = [("plugins", "0015_computer_use_accessibility")]

    operations = [migrations.RunPython(_describe(CURRENT), _describe(PREVIOUS))]
