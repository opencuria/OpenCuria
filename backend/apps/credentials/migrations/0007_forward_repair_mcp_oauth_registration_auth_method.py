"""Repair the OAuth registration auth-method column on databases past 0006."""

from __future__ import annotations

from django.db import migrations


def repair_registration_auth_method(apps, schema_editor):
    registration_model = apps.get_model("credentials", "McpOAuthClientRegistration")
    field = registration_model._meta.get_field("token_endpoint_auth_method")
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, registration_model._meta.db_table
            )
        }

    if field.column in columns:
        return

    # Supply the model's intended default while adding the non-null column so
    # existing registrations remain usable without changing their IDs/secrets.
    add_field = field.clone()
    add_field.default = "none"
    add_field.set_attributes_from_name(field.name)
    schema_editor.add_field(registration_model, add_field)


def noop(apps, schema_editor):
    """Keep the repair migration reversible without removing user data."""


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0006_repair_legacy_mcp_oauth"),
        ("plugins", "0007_repair_notion_mcp_oauth"),
    ]

    operations = [migrations.RunPython(repair_registration_auth_method, noop)]
