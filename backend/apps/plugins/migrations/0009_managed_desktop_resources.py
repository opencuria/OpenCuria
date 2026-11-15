"""Declare managed desktop resources without overwriting customized seeds."""

from importlib import import_module

from django.db import migrations, models

seed = import_module("apps.plugins.migrations.0002_seed_playwright_plugin")
headed = import_module("apps.plugins.migrations.0004_playwright_headed_desktop")
artifacts = import_module("apps.plugins.migrations.0005_playwright_workspace_artifacts")


def upgrade_playwright(apps, schema_editor):
    """Upgrade only the untouched global seed; org copies stay unchanged."""
    servers = apps.get_model("plugins", "PluginMcpServer")
    servers.objects.filter(
        plugin__slug="playwright",
        plugin__organization__isnull=True,
        slug="playwright",
        transport="stdio",
        command="npx",
        args=artifacts.NEW_ARGS,
        env=headed.PLAYWRIGHT_MCP_ENV_V3,
        cwd="/workspace",
        url="",
        headers={},
        auth_type="none",
        oauth_requirement_key="",
        startup_timeout_seconds=120,
        request_timeout_seconds=60,
        resources={},
    ).update(env={}, resources={"desktop": {"activation": "first_tool"}})
    apps.get_model("plugins", "PluginSkill").objects.filter(
        plugin__slug="playwright",
        plugin__organization__isnull=True,
        slug="playwright-basics",
        body=seed.PLAYWRIGHT_SKILL_BODY,
    ).update(
        body=seed.PLAYWRIGHT_SKILL_BODY.replace("headless Chromium", "headed Chromium")
    )
    old_description = (
        "Global browser automation plugin backed by the Playwright MCP "
        "server (headless Chromium). Activate it explicitly per "
        "organization to use it in workspaces."
    )
    apps.get_model("plugins", "Plugin").objects.filter(
        slug="playwright",
        organization__isnull=True,
        description=old_description,
    ).update(
        description=old_description.replace("headless Chromium", "headed Chromium")
    )


class Migration(migrations.Migration):
    dependencies = [("plugins", "0008_plugin_credential_separation")]
    operations = [
        migrations.AddField(
            model_name="pluginmcpserver",
            name="resources",
            field=models.JSONField(default=dict, blank=True),
        ),
        migrations.RunPython(upgrade_playwright, migrations.RunPython.noop),
    ]
