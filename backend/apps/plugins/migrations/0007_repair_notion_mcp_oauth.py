"""Repair the Notion OAuth service seeded by an earlier development migration."""

from __future__ import annotations

import uuid

from django.db import migrations
from django.db.models import Q

NOTION_PLUGIN_ID = uuid.UUID("1d672e4d-0bf0-4db9-8e71-7d98a82b2b17")
NOTION_SERVICE_ID = uuid.UUID("3293653a-257a-4f20-92fb-7d363c7c765f")
LEGACY_SERVICE_SLUG = "mcp-oauth"
NOTION_SERVICE_SLUG = "notion-notion_oauth-oauth"


def repair_notion_oauth(apps, schema_editor):
    plugin_model = apps.get_model("plugins", "Plugin")
    server_model = apps.get_model("plugins", "PluginMcpServer")
    requirement_model = apps.get_model("plugins", "PluginCredentialRequirement")
    service_model = apps.get_model("credentials", "CredentialService")
    credential_model = apps.get_model("credentials", "Credential")

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

    server = server_model.objects.filter(plugin=plugin, slug="notion").first()
    if server is None:
        server = server_model.objects.create(
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

    requirement = requirement_model.objects.filter(
        plugin=plugin, key="notion_oauth"
    ).first()
    assigned_service = requirement.credential_service if requirement else None
    service = service_model.objects.filter(
        slug=NOTION_SERVICE_SLUG, organization__isnull=True
    ).first()

    # Keep the old service row (and its credentials) when it belongs only to
    # Notion. A shared service must remain untouched; Notion gets a dedicated
    # service instead, and Notion-bound credentials are moved below.
    if service is None and assigned_service is not None:
        other_requirements = requirement_model.objects.filter(
            credential_service=assigned_service
        ).exclude(pk=requirement.pk)
        if (
            assigned_service.organization_id is None
            and assigned_service.slug == LEGACY_SERVICE_SLUG
            and not other_requirements.exists()
        ):
            slug_collision = (
                service_model.objects.filter(
                    organization__isnull=True, slug=NOTION_SERVICE_SLUG
                )
                .exclude(pk=assigned_service.pk)
                .exists()
            )
            if not slug_collision:
                assigned_service.slug = NOTION_SERVICE_SLUG
                assigned_service.save(update_fields=["slug"])
                service = assigned_service

    if service is None:
        service_id = NOTION_SERVICE_ID
        if service_model.objects.filter(pk=service_id).exists():
            service_id = uuid.uuid4()
        service = service_model.objects.create(
            id=service_id,
            organization_id=None,
            name="Notion OAuth",
            slug=NOTION_SERVICE_SLUG,
            description="Server-side OAuth authorization for Notion MCP.",
            credential_type="mcp_oauth",
            label="Connect Notion",
            env_var_name="",
            target_path="",
            plugin_owned=True,
            oauth_plugin_slug="notion",
            oauth_requirement_key="notion_oauth",
        )

    changed = []
    if service.credential_type != "mcp_oauth":
        service.credential_type = "mcp_oauth"
        changed.append("credential_type")
    if not service.name:
        service.name = "Notion OAuth"
        changed.append("name")
    if not service.description:
        service.description = "Server-side OAuth authorization for Notion MCP."
        changed.append("description")
    if not service.label:
        service.label = "Connect Notion"
        changed.append("label")
    if not service.plugin_owned:
        service.plugin_owned = True
        changed.append("plugin_owned")
    if service.oauth_plugin_slug != "notion":
        service.oauth_plugin_slug = "notion"
        changed.append("oauth_plugin_slug")
    if service.oauth_requirement_key != "notion_oauth":
        service.oauth_requirement_key = "notion_oauth"
        changed.append("oauth_requirement_key")
    if changed:
        service.save(update_fields=changed)

    if requirement is None:
        requirement_model.objects.create(
            plugin=plugin,
            key="notion_oauth",
            credential_service=service,
            required=True,
            description="Authorize access to your Notion workspace.",
            plugin_owned_service=True,
        )
    else:
        if requirement.credential_service_id != service.pk:
            # OAuth credentials have an explicit MCP server binding; migrate
            # only those bound to Notion and leave other consumers untouched.
            notion_credentials = credential_model.objects.filter(
                Q(service_id=requirement.credential_service_id)
                & (
                    Q(oauth_server_id=server.pk)
                    | Q(oauth_server_id__isnull=True, oauth_server_url=server.url)
                )
            )
            for credential in notion_credentials.iterator():
                duplicate = credential_model.objects.filter(
                    service_id=service.pk,
                    oauth_server_id=credential.oauth_server_id,
                )
                if credential.user_id:
                    duplicate = duplicate.filter(user_id=credential.user_id)
                else:
                    duplicate = duplicate.filter(
                        organization_id=credential.organization_id
                    )
                if not duplicate.exclude(pk=credential.pk).exists():
                    credential_model.objects.filter(pk=credential.pk).update(
                        service_id=service.pk
                    )
            requirement.credential_service = service
        if not requirement.plugin_owned_service:
            requirement.plugin_owned_service = True
        requirement.save(update_fields=["credential_service", "plugin_owned_service"])


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0006_repair_legacy_mcp_oauth"),
        ("plugins", "0006_mcp_oauth"),
    ]

    operations = [migrations.RunPython(repair_notion_oauth, migrations.RunPython.noop)]
