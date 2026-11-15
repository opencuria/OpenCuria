# Durable encrypted ChatGPT device flow state.
import uuid

import django.conf
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0032_provider_turn_snapshots"),
        migrations.swappable_dependency(django.conf.settings.AUTH_USER_MODEL),
        ("organizations", "0002_versioned_images"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProviderOAuthFlow",
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
                ("target_account_id", models.UUIDField(blank=True, null=True)),
                (
                    "label",
                    models.CharField(blank=True, default="ChatGPT", max_length=255),
                ),
                ("shared", models.BooleanField(default=False)),
                ("device_auth_id_encrypted", models.TextField()),
                ("user_code_encrypted", models.TextField()),
                ("interval", models.PositiveSmallIntegerField(default=5)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("polling", "Polling"),
                            ("consumed", "Consumed"),
                            ("cancelled", "Cancelled"),
                            ("expired", "Expired"),
                            ("denied", "Denied"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("poll_token", models.UUIDField(blank=True, null=True)),
                (
                    "connected_account_id",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="provider_oauth_flows",
                        to="organizations.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="provider_oauth_flows",
                        to=django.conf.settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"db_table": "harness_provider_oauth_flow"},
        ),
        migrations.AddIndex(
            model_name="provideroauthflow",
            index=models.Index(
                fields=["organization", "user", "status"],
                name="harness_oauth_flow_owner_idx",
            ),
        ),
    ]
