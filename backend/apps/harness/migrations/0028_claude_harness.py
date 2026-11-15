# Claude engine credentials, per-session resume state, and encrypted transcript rows.
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

CLAUDE_CREDENTIAL_SERVICES = [
    {
        "id": "33333333-3333-4333-8333-333333333331",
        "name": "Claude Agent API Token",
        "slug": "claude-agent-api-token",
        "env_var_name": "ANTHROPIC_API_KEY",
        "label": "Claude API Token",
    },
    {
        "id": "33333333-3333-4333-8333-333333333332",
        "name": "Claude Subscription Token",
        "slug": "claude-agent-subscription-token",
        "env_var_name": "CLAUDE_CODE_OAUTH_TOKEN",
        "label": "Claude Subscription Token",
    },
]


def seed_claude_credential_services(apps, schema_editor):
    """Register Claude secret types in the existing credential catalog."""
    service_model = apps.get_model("credentials", "CredentialService")
    organization_model = apps.get_model("organizations", "Organization")
    activation_model = apps.get_model("credentials", "OrgCredentialServiceActivation")
    services = []
    for values in CLAUDE_CREDENTIAL_SERVICES:
        service = service_model.objects.filter(
            slug=values["slug"], organization__isnull=True
        ).first()
        defaults = {
            "name": values["name"],
            "description": (
                "Used by the Claude harness; secret values remain server-side."
            ),
            "credential_type": "env",
            "env_var_name": values["env_var_name"],
            "label": values["label"],
        }
        if service is None:
            service = service_model.objects.create(
                id=uuid.UUID(values["id"]),
                slug=values["slug"],
                **defaults,
            )
        else:
            for field, value in defaults.items():
                setattr(service, field, value)
            service.save(update_fields=list(defaults))
        services.append(service)
    activation_model.objects.bulk_create(
        [
            activation_model(organization=organization, credential_service=service)
            for organization in organization_model.objects.all()
            for service in services
        ],
        ignore_conflicts=True,
    )


def unseed_claude_credential_services(apps, schema_editor):
    """Remove only the two named global services created by this migration."""
    service_model = apps.get_model("credentials", "CredentialService")
    service_model.objects.filter(
        id__in=[uuid.UUID(service["id"]) for service in CLAUDE_CREDENTIAL_SERVICES],
        organization__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("credentials", "0010_oauth_registration_fingerprint"),
        ("harness", "0027_subagentconfig"),
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            seed_claude_credential_services,
            unseed_claude_credential_services,
        ),
        migrations.CreateModel(
            name="HarnessConnection",
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
                    "auth_type",
                    models.CharField(
                        choices=[
                            ("api_token", "API token"),
                            ("subscription_token", "Subscription token"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "label",
                    models.CharField(blank=True, default="Claude", max_length=255),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "credential",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="harness_connections",
                        to="credentials.credential",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="harness_connections",
                        to="organizations.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="harness_connections",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "db_table": "harness_connection",
                "ordering": ["-updated_at"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "user"),
                        name="harness_connection_org_user_uniq",
                    )
                ],
            },
        ),
        migrations.AddField(
            model_name="harnesssession",
            name="harness_id",
            field=models.CharField(
                default="native",
                help_text="Execution engine that owns this session.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="harnesssession",
            name="connection",
            field=models.ForeignKey(
                blank=True,
                help_text=(
                    "Personal engine connection selected when this session was created."
                ),
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sessions",
                to="harness.harnessconnection",
            ),
        ),
        migrations.AddField(
            model_name="harnesssession",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                help_text="User who owns this session and its engine authorization.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="harness_sessions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="harnesssession",
            name="external_session_id",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="harnesssession",
            name="engine_state",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Engine-specific resumable session state (no credentials).",
            ),
        ),
        migrations.AddField(
            model_name="harnessmessage",
            name="harness_id",
            field=models.CharField(
                default="native",
                help_text="Execution engine that produced this message.",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="harnessmessage",
            name="engine_meta",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Secret-free engine-specific message metadata.",
            ),
        ),
        migrations.CreateModel(
            name="HarnessTranscriptBatch",
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
                ("external_session_id", models.CharField(max_length=64)),
                ("subpath", models.CharField(blank=True, default="", max_length=255)),
                ("digest", models.CharField(max_length=64)),
                ("payload_encrypted", models.TextField()),
                ("position", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "session",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transcript_batches",
                        to="harness.harnesssession",
                    ),
                ),
            ],
            options={
                "db_table": "harness_transcript_batch",
                "ordering": ["position", "created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("session", "external_session_id", "subpath", "digest"),
                        name="harness_transcript_digest_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=(
                            "session",
                            "external_session_id",
                            "subpath",
                            "position",
                        ),
                        name="harness_transcript_position_uniq",
                    ),
                ],
            },
        ),
    ]
