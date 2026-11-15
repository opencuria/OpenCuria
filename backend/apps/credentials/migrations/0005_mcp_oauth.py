# Server-side OAuth metadata, credential bindings, and authorization state.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("credentials", "0004_credentialservice_org_scoping")]

    operations = [
        migrations.AlterField(
            model_name="credentialservice",
            name="credential_type",
            field=models.CharField(
                choices=[
                    ("env", "Environment Variable"),
                    ("file", "Credential File"),
                    ("ssh_key", "SSH Key"),
                    ("mcp_oauth", "MCP OAuth (server-side only)"),
                ],
                default="env",
                help_text="How this credential is injected into a workspace.",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="credentialservice",
            name="plugin_owned",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="credentialservice",
            name="oauth_plugin_slug",
            field=models.SlugField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="credentialservice",
            name="oauth_requirement_key",
            field=models.SlugField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="credential",
            name="oauth_server_id",
            field=models.UUIDField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="credential",
            name="oauth_server_url",
            field=models.CharField(blank=True, default="", max_length=2048),
        ),
        migrations.AddField(
            model_name="credential",
            name="oauth_resource",
            field=models.CharField(blank=True, default="", max_length=2048),
        ),
        migrations.AddField(
            model_name="credential",
            name="oauth_status",
            field=models.CharField(blank=True, default="disconnected", max_length=32),
        ),
        migrations.CreateModel(
            name="McpOAuthClientRegistration",
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
                ("server_url", models.CharField(max_length=2048)),
                ("callback_url", models.CharField(max_length=2048)),
                ("issuer", models.CharField(max_length=2048)),
                ("client_id", models.CharField(max_length=2048)),
                ("encrypted_client_secret", models.TextField(blank=True, default="")),
                ("authorization_endpoint", models.CharField(max_length=2048)),
                ("token_endpoint", models.CharField(max_length=2048)),
                (
                    "registration_endpoint",
                    models.CharField(blank=True, default="", max_length=2048),
                ),
                (
                    "token_endpoint_auth_method",
                    models.CharField(default="none", max_length=32),
                ),
                ("scopes_supported", models.JSONField(blank=True, default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "credentials_mcp_oauth_registration"},
        ),
        migrations.AddConstraint(
            model_name="mcpoauthclientregistration",
            constraint=models.UniqueConstraint(
                fields=("server_url", "callback_url", "issuer"),
                name="unique_mcp_oauth_registration",
            ),
        ),
        migrations.AddField(
            model_name="credential",
            name="oauth_registration",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="credentials",
                to="credentials.mcpoauthclientregistration",
            ),
        ),
        migrations.CreateModel(
            name="McpOAuthAuthorizationState",
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
                ("state_hash", models.CharField(max_length=64, unique=True)),
                ("browser_binding_hash", models.CharField(max_length=64)),
                ("encrypted_code_verifier", models.TextField()),
                ("organization_credential", models.BooleanField(default=False)),
                ("server_id", models.UUIDField()),
                (
                    "requirement_key",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("server_url", models.CharField(max_length=2048)),
                ("resource", models.CharField(max_length=2048)),
                ("issuer", models.CharField(max_length=2048)),
                ("redirect_uri", models.CharField(max_length=2048)),
                ("scope", models.CharField(blank=True, default="", max_length=2048)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="organizations.organization",
                    ),
                ),
                (
                    "registration",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="credentials.mcpoauthclientregistration",
                    ),
                ),
                (
                    "service",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="credentials.credentialservice",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"db_table": "credentials_mcp_oauth_state"},
        ),
        migrations.AddIndex(
            model_name="mcpoauthauthorizationstate",
            index=models.Index(
                fields=["expires_at", "consumed_at"],
                name="credentials_expires_482706_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="credential",
            constraint=models.UniqueConstraint(
                fields=("user", "service", "oauth_server_id"),
                condition=models.Q(user__isnull=False, oauth_server_id__isnull=False),
                name="unique_personal_mcp_oauth_binding",
            ),
        ),
        migrations.AddConstraint(
            model_name="credential",
            constraint=models.UniqueConstraint(
                fields=("organization", "service", "oauth_server_id"),
                condition=models.Q(
                    organization__isnull=False,
                    oauth_server_id__isnull=False,
                ),
                name="unique_org_mcp_oauth_binding",
            ),
        ),
    ]
