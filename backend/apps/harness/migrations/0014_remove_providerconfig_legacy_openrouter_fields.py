"""Remove legacy OpenRouter credential fields from ProviderConfig."""

from django.db import migrations


class Migration(migrations.Migration):
    """Drop api_key_encrypted and base_url from harness_provider_config."""

    dependencies = [
        ("harness", "0013_migrate_openrouter_connection"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="providerconfig",
            name="api_key_encrypted",
        ),
        migrations.RemoveField(
            model_name="providerconfig",
            name="base_url",
        ),
    ]
