"""Repair OAuth columns/indexes missing from earlier development schemas.

Some local databases had already recorded 0005_mcp_oauth after an earlier
iteration of that migration. The migration state is therefore correct while
the physical schema is incomplete. This migration inspects the physical schema
and only adds missing pieces.
"""

from __future__ import annotations

from django.db import migrations, models
from django.db.models import F, Q

FIELD_NAMES = {
    "CredentialService": (
        "plugin_owned",
        "oauth_plugin_slug",
        "oauth_requirement_key",
    ),
    "Credential": (
        "oauth_server_id",
        "oauth_server_url",
        "oauth_resource",
        "oauth_status",
        "oauth_registration",
    ),
    "McpOAuthClientRegistration": ("token_endpoint_auth_method",),
    "McpOAuthAuthorizationState": (
        "requirement_key",
        "resource",
        "issuer",
    ),
}

CONSTRAINT_NAMES = (
    "unique_personal_mcp_oauth_binding",
    "unique_org_mcp_oauth_binding",
)


def _missing_fields(schema_editor, model, field_names):
    """Add missing columns without forcing SQLite to rebuild an incomplete table."""
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, model._meta.db_table
            )
        }

    for name in field_names:
        field = model._meta.get_field(name)
        if field.column in columns:
            continue

        # Add columns as nullable without a database default. This is a simple
        # ALTER TABLE ADD COLUMN on SQLite, so it preserves every legacy column
        # (including columns from local development extensions). Existing rows
        # are populated below.
        add_field = field.clone()
        if not field.null:
            add_field.null = True
        add_field.default = models.NOT_PROVIDED
        add_field.set_attributes_from_name(field.name)
        schema_editor.add_field(model, add_field)
        columns.add(field.column)


def _fill_field_defaults(model, field_names):
    """Populate defaulted newly-added fields on pre-existing rows."""
    for name in field_names:
        field = model._meta.get_field(name)
        default = field.get_default()
        if default is not None:
            model.objects.filter(**{f"{name}__isnull": True}).update(**{name: default})


def repair_legacy_mcp_oauth(apps, schema_editor):
    for model_name, field_names in FIELD_NAMES.items():
        model = apps.get_model("credentials", model_name)
        _missing_fields(schema_editor, model, field_names)

    credential_model = apps.get_model("credentials", "Credential")
    with schema_editor.connection.cursor() as cursor:
        constraints = schema_editor.connection.introspection.get_constraints(
            cursor, credential_model._meta.db_table
        )
    for name in CONSTRAINT_NAMES:
        if name not in constraints:
            constraint = next(
                item for item in credential_model._meta.constraints if item.name == name
            )
            schema_editor.add_constraint(credential_model, constraint)

    # Earlier OAuth credential rows sometimes retained the URL but not the
    # server UUID. Recover the UUID only when that URL resolves to exactly one
    # persisted plugin MCP server.
    table_names = set(schema_editor.connection.introspection.table_names())
    if "plugins_mcp_server" in table_names:
        plugin_server_model = apps.get_model("plugins", "PluginMcpServer")
        credential_model = apps.get_model("credentials", "Credential")
        for credential in credential_model.objects.filter(
            oauth_server_id__isnull=True
        ).exclude(oauth_server_url="").iterator():
            server_ids = list(
                plugin_server_model.objects.filter(url=credential.oauth_server_url)
                .values_list("id", flat=True)[:2]
            )
            if len(server_ids) != 1:
                continue
            duplicate = credential_model.objects.filter(
                service_id=credential.service_id,
                oauth_server_id=server_ids[0],
            )
            if credential.user_id:
                duplicate = duplicate.filter(user_id=credential.user_id)
            else:
                duplicate = duplicate.filter(
                    organization_id=credential.organization_id
                )
            if not duplicate.exclude(pk=credential.pk).exists():
                credential_model.objects.filter(pk=credential.pk).update(
                    oauth_server_id=server_ids[0]
                )

    state_model = apps.get_model("credentials", "McpOAuthAuthorizationState")
    if any(field.column == "resource" for field in state_model._meta.fields):
        state_model.objects.filter(
            Q(resource="") | Q(resource__isnull=True)
        ).update(resource=F("server_url"))
    if any(field.column == "issuer" for field in state_model._meta.fields):
        for row in state_model.objects.filter(
            Q(issuer="") | Q(issuer__isnull=True)
        ).iterator():
            state_model.objects.filter(pk=row.pk).update(issuer=row.registration.issuer)

    # Older authorization states did not record a requirement key. Recover it
    # from the referenced plugin MCP server when that plugin migration already
    # supplied the field; skip safely if the plugin migration is not yet applied.
    table_names = set(schema_editor.connection.introspection.table_names())
    if "plugins_mcp_server" in table_names:
        with schema_editor.connection.cursor() as cursor:
            server_description = (
                schema_editor.connection.introspection.get_table_description(
                    cursor, "plugins_mcp_server"
                )
            )
        if any(column.name == "oauth_requirement_key" for column in server_description):
            server_model = apps.get_model("plugins", "PluginMcpServer")
            for row in state_model.objects.filter(
                Q(requirement_key="") | Q(requirement_key__isnull=True)
            ).iterator():
                requirement_key = (
                    server_model.objects.filter(pk=row.server_id)
                    .values_list("oauth_requirement_key", flat=True)
                    .first()
                )
                if requirement_key:
                    state_model.objects.filter(pk=row.pk).update(
                        requirement_key=requirement_key
                    )

    # This migration runs once, only where the database recorded a previous
    # OAuth schema. Preserve all non-null customizations in every case.
    for model_name, field_names in FIELD_NAMES.items():
        model = apps.get_model("credentials", model_name)
        _fill_field_defaults(model, field_names)


def noop(apps, schema_editor):
    """Keep the repair migration reversible without removing user data."""


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0005_mcp_oauth"),
        ("plugins", "0006_mcp_oauth"),
    ]

    operations = [migrations.RunPython(repair_legacy_mcp_oauth, noop)]
