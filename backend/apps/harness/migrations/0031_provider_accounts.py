# Provider accounts, per-user selections, and migration of legacy secrets.
import json
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def migrate_provider_accounts(apps, schema_editor):
    """Move native and Claude account secrets into the credential vault."""
    from common.utils import decrypt_value, encrypt_value

    ProviderConnection = apps.get_model("harness", "ProviderConnection")
    HarnessConnection = apps.get_model("harness", "HarnessConnection")
    Credential = apps.get_model("credentials", "Credential")
    CredentialService = apps.get_model("credentials", "CredentialService")

    def service_for(org_id, provider):
        slug = f"provider-account-{provider}"
        service = CredentialService.objects.filter(
            organization_id=org_id, slug=slug
        ).first()
        if service is not None and service.credential_type != "provider_secret":
            raise ValueError("Reserved provider-account credential service is occupied")
        if service is None:
            service = CredentialService.objects.create(
                organization_id=org_id,
                name=f"Provider account ({provider})",
                slug=slug,
                description="Internal provider-account secret storage.",
                credential_type="provider_secret",
                env_var_name="",
                target_path="",
                label="Provider account",
            )
        return service

    for account in ProviderConnection.objects.all().iterator():
        raw = (account.credentials_encrypted or "").strip()
        secret = {}
        if raw:
            value = json.loads(decrypt_value(raw))
            if isinstance(value, dict):
                secret = value
        service = service_for(account.organization_id, account.provider)
        credential = Credential.objects.create(
            organization_id=account.organization_id,
            user_id=None,
            service_id=service.id,
            name=account.provider,
            encrypted_value=encrypt_value(json.dumps(secret)),
            created_by_id=None,
        )
        account.label = account.provider
        account.credential_id = credential.id
        account.save(update_fields=["label", "credential_id"])

    # Preserve the old UUID for any historical session.connection references.
    for old in HarnessConnection.objects.select_related("credential").iterator():
        secret = {}
        if old.credential_id:
            raw_token = decrypt_value(old.credential.encrypted_value)
            if raw_token:
                secret["token"] = raw_token
        service = service_for(old.organization_id, "claude-agent")
        credential = Credential.objects.create(
            organization_id=None,
            user_id=old.user_id,
            service_id=service.id,
            name=old.label or "Claude",
            encrypted_value=encrypt_value(json.dumps(secret)),
            created_by_id=old.user_id,
        )
        if old.credential_id:
            Credential.objects.filter(pk=old.credential_id).delete()
        # Preserve the legacy FK and row as history. Runtime no longer binds
        # it; the new provider-account row has the identical UUID.
        ProviderConnection.objects.create(
            id=old.id,
            organization_id=old.organization_id,
            user_id=old.user_id,
            provider="claude-agent",
            label=old.label or "Claude",
            credential_id=credential.id,
            config={"auth_type": old.auth_type},
        )


def noop_reverse(apps, schema_editor):
    """Provider account conversion is intentionally irreversible."""
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("credentials", "0011_provider_accounts"),
        ("harness", "0030_harnesssession_summary"),
        ("organizations", "0002_versioned_images"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ProviderSelection",
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
                            ("openai-compatible", "OpenAI Compatible"),
                            ("claude-agent", "Claude Agent"),
                        ],
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "harness_provider_selection"},
        ),
        migrations.AlterModelOptions(
            name="providerconnection",
            options={"ordering": ["provider", "created_at", "id"]},
        ),
        migrations.RemoveConstraint(
            model_name="providerconnection",
            name="harness_provider_connection_org_provider_uniq",
        ),
        migrations.AddField(
            model_name="providerconnection",
            name="credential",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="provider_accounts",
                to="credentials.credential",
            ),
        ),
        migrations.AddField(
            model_name="providerconnection",
            name="deleted_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="providerconnection",
            name="label",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="providerconnection",
            name="user",
            field=models.ForeignKey(
                blank=True,
                help_text="Null for an organization-shared account.",
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="provider_connections",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="providerconnection",
            name="config",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Non-secret provider settings (including Claude auth_type).",
            ),
        ),
        migrations.AlterField(
            model_name="providerconnection",
            name="organization",
            field=models.ForeignKey(
                help_text="Organization context owning this account.",
                on_delete=django.db.models.deletion.CASCADE,
                related_name="provider_connections",
                to="organizations.organization",
            ),
        ),
        migrations.AlterField(
            model_name="providerconnection",
            name="provider",
            field=models.CharField(
                choices=[
                    ("openrouter", "OpenRouter"),
                    ("chatgpt", "ChatGPT"),
                    ("amazon-bedrock", "Amazon Bedrock"),
                    ("openai-compatible", "OpenAI Compatible"),
                    ("claude-agent", "Claude Agent"),
                ],
                help_text=(
                    "Native provider identifier or the claude-agent harness provider."
                ),
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="providerconnection",
            constraint=models.UniqueConstraint(
                condition=models.Q(deleted_at__isnull=True, user__isnull=True),
                fields=("organization", "provider", "label"),
                name="provider_account_org_label_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="providerconnection",
            constraint=models.UniqueConstraint(
                condition=models.Q(deleted_at__isnull=True, user__isnull=False),
                fields=("organization", "user", "provider", "label"),
                name="provider_account_user_label_uniq",
            ),
        ),
        migrations.RunPython(
            migrate_provider_accounts, migrations.RunPython.noop
        ),
        migrations.RemoveField(
            model_name="providerconnection",
            name="credentials_encrypted",
        ),
        migrations.AddField(
            model_name="providerselection",
            name="account",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="selections",
                to="harness.providerconnection",
            ),
        ),
        migrations.AddField(
            model_name="providerselection",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="provider_selections",
                to="organizations.organization",
            ),
        ),
        migrations.AddField(
            model_name="providerselection",
            name="user",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="provider_selections",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddConstraint(
            model_name="providerselection",
            constraint=models.UniqueConstraint(
                fields=("organization", "user", "provider"),
                name="provider_selection_org_user_provider_uniq",
            ),
        ),
    ]
