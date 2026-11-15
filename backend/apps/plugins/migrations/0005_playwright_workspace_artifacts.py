"""Keep Playwright's default artifacts in the readable workspace tree.

The older /tmp output directory makes browser screenshots appear to succeed,
but their links cannot be read by workspace file tools or embedded in chat.
Only upgrade the untouched seeded command; respect administrators' edits.
"""

from __future__ import annotations

from django.db import migrations

OLD_ARGS = [
    "-y",
    "@playwright/mcp@latest",
    "--isolated",
    "--no-sandbox",
    "--executable-path",
    "/usr/bin/google-chrome-stable",
    "--output-dir",
    "/tmp/.opencuria/playwright",
]
NEW_ARGS = [*OLD_ARGS[:-1], "/workspace/.opencuria/playwright"]


def upgrade_playwright_artifacts(apps, schema_editor):
    """Move only the original headed seed's output directory."""
    mcp_model = apps.get_model("plugins", "PluginMcpServer")
    mcp_model.objects.filter(
        plugin__slug="playwright",
        plugin__organization__isnull=True,
        slug="playwright",
        transport="stdio",
        command="npx",
        args=OLD_ARGS,
    ).update(args=NEW_ARGS)


def restore_playwright_artifacts(apps, schema_editor):
    """Restore the previous seed without overwriting subsequent edits."""
    mcp_model = apps.get_model("plugins", "PluginMcpServer")
    mcp_model.objects.filter(
        plugin__slug="playwright",
        plugin__organization__isnull=True,
        slug="playwright",
        transport="stdio",
        command="npx",
        args=NEW_ARGS,
    ).update(args=OLD_ARGS)


class Migration(migrations.Migration):
    dependencies = [("plugins", "0004_playwright_headed_desktop")]

    operations = [
        migrations.RunPython(upgrade_playwright_artifacts, restore_playwright_artifacts)
    ]
