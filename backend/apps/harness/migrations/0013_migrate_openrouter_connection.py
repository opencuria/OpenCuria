"""Migrate OpenRouter credentials from ProviderConfig to ProviderConnection."""

import json

from django.db import migrations

from common.utils import decrypt_value, encrypt_value

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def migrate_openrouter_connections(apps, schema_editor):
    """Copy legacy OpenRouter API keys into ProviderConnection rows."""
    provider_config_model = apps.get_model("harness", "ProviderConfig")
    provider_connection_model = apps.get_model("harness", "ProviderConnection")

    for config in provider_config_model.objects.all().iterator():
        api_key_encrypted = (config.api_key_encrypted or "").strip()
        if not api_key_encrypted:
            continue

        api_key = decrypt_value(api_key_encrypted)
        credentials_encrypted = encrypt_value(json.dumps({"api_key": api_key}))
        base_url = (config.base_url or "").strip() or DEFAULT_OPENROUTER_BASE_URL

        provider_connection_model.objects.update_or_create(
            organization_id=config.organization_id,
            provider="openrouter",
            defaults={
                "credentials_encrypted": credentials_encrypted,
                "config": {"base_url": base_url},
            },
        )


def reverse_migrate_openrouter_connections(apps, schema_editor):
    # Irreversible: legacy ProviderConfig credential fields are removed in 0014.
    pass


class Migration(migrations.Migration):
    """Move OpenRouter secrets from ProviderConfig into ProviderConnection."""

    dependencies = [
        ("harness", "0012_providerconnection"),
    ]

    operations = [
        migrations.RunPython(
            migrate_openrouter_connections,
            reverse_migrate_openrouter_connections,
        ),
    ]
