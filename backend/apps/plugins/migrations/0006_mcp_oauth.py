import uuid

from django.db import migrations, models

NOTION_PLUGIN_ID = uuid.UUID("1d672e4d-0bf0-4db9-8e71-7d98a82b2b17")
NOTION_SERVICE_ID = uuid.UUID("3293653a-257a-4f20-92fb-7d363c7c765f")


def flush_deferred_constraints(apps, schema_editor):
    """Finish PostgreSQL FK trigger events before deferred index DDL."""
    if schema_editor.connection.vendor == "postgresql":
        with schema_editor.connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def seed_notion(apps, schema_editor):
    plugin_model = apps.get_model("plugins", "Plugin")
    server_model = apps.get_model("plugins", "PluginMcpServer")
    requirement_model = apps.get_model("plugins", "PluginCredentialRequirement")
    service_model = apps.get_model("credentials", "CredentialService")
    plugin = (
        plugin_model.objects.filter(id=NOTION_PLUGIN_ID).first()
        or plugin_model.objects.filter(slug="notion", organization__isnull=True).first()
    )
    if plugin is None:
        plugin = plugin_model.objects.create(
            id=NOTION_PLUGIN_ID,
            organization_id=None,
            name="Notion",
            slug="notion",
            description=(
                "Connect Notion through its official MCP server. "
                "Activate explicitly per organization."
            ),
            enabled=True,
            published=True,
        )
    service = (
        service_model.objects.filter(id=NOTION_SERVICE_ID).first()
        or service_model.objects.filter(
            slug="notion-notion_oauth-oauth", organization__isnull=True
        ).first()
    )
    if service is None:
        service = service_model.objects.create(
            id=NOTION_SERVICE_ID,
            organization_id=None,
            name="Notion OAuth",
            slug="notion-notion_oauth-oauth",
            description="Server-side OAuth authorization for Notion MCP.",
            credential_type="mcp_oauth",
            label="Connect Notion",
            env_var_name="",
            target_path="",
            plugin_owned=True,
            oauth_plugin_slug="notion",
            oauth_requirement_key="notion_oauth",
        )
    elif service.slug != "notion-notion_oauth-oauth":
        service.slug = "notion-notion_oauth-oauth"
        service.save(update_fields=["slug"])
    if not requirement_model.objects.filter(plugin=plugin, key="notion_oauth").exists():
        requirement_model.objects.create(
            plugin=plugin,
            key="notion_oauth",
            credential_service=service,
            required=True,
            description="Authorize access to your Notion workspace.",
            plugin_owned_service=True,
        )
    if not server_model.objects.filter(plugin=plugin, slug="notion").exists():
        server_model.objects.create(
            plugin=plugin,
            slug="notion",
            name="Notion",
            transport="streamable_http",
            command="",
            args=[],
            cwd="/workspace",
            env={},
            url="https://mcp.notion.com/mcp",
            headers={},
            auth_type="oauth",
            oauth_requirement_key="notion_oauth",
            startup_timeout_seconds=30,
            request_timeout_seconds=60,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("plugins", "0005_playwright_workspace_artifacts"),
        ("credentials", "0005_mcp_oauth"),
    ]
    operations = [
        migrations.AddField(
            model_name="pluginmcpserver",
            name="auth_type",
            field=models.CharField(
                choices=[("none", "None"), ("oauth", "OAuth")],
                default="none",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="pluginmcpserver",
            name="oauth_requirement_key",
            field=models.SlugField(blank=True, default="", max_length=255),
        ),
        migrations.RunPython(seed_notion, migrations.RunPython.noop),
        migrations.RunPython(
            flush_deferred_constraints,
            migrations.RunPython.noop,
        ),
    ]
