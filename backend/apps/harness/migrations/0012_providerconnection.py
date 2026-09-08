"""Create ProviderConnection for per-provider credentials."""

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """Add harness_provider_connection table."""

    dependencies = [
        ("harness", "0011_harnesssession_manual_unread_at"),
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProviderConnection",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("openrouter", "OpenRouter"),
                            ("chatgpt", "ChatGPT"),
                            ("amazon-bedrock", "Amazon Bedrock"),
                        ],
                        help_text=(
                            "Provider identifier "
                            "(openrouter, chatgpt, amazon-bedrock)."
                        ),
                        max_length=32,
                    ),
                ),
                (
                    "credentials_encrypted",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text=(
                            "Fernet-encrypted JSON object with provider secrets."
                        ),
                    ),
                ),
                (
                    "config",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text=(
                            "Non-secret provider settings (base_url, region, etc.)."
                        ),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        help_text="Owning organization.",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="provider_connections",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "db_table": "harness_provider_connection",
                "ordering": ["provider"],
            },
        ),
        migrations.AddConstraint(
            model_name="providerconnection",
            constraint=models.UniqueConstraint(
                fields=("organization", "provider"),
                name="harness_provider_connection_org_provider_uniq",
            ),
        ),
    ]
