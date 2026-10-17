# Created for production deployment compatibility tracking.
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [
        migrations.CreateModel(
            name="Release",
            fields=[
                (
                    "version",
                    models.CharField(max_length=255, primary_key=True, serialize=False),
                ),
                ("protocol_min", models.PositiveIntegerField()),
                ("protocol_max", models.PositiveIntegerField()),
                ("database_schema", models.PositiveIntegerField()),
                ("database_schema_min", models.PositiveIntegerField()),
                ("database_schema_max", models.PositiveIntegerField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("draining", "Draining"),
                            ("rollback", "Rollback retained"),
                            ("retired", "Retired"),
                        ],
                        default="active",
                        max_length=16,
                    ),
                ),
                (
                    "registered_at",
                    models.DateTimeField(default=django.utils.timezone.now),
                ),
                ("retired_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ("registered_at", "version")},
        ),
        migrations.CreateModel(
            name="DatabaseSchemaState",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("schema_version", models.PositiveIntegerField()),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
        ),
        migrations.CreateModel(
            name="LifecycleRecoveryLease",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("owner_id", models.CharField(blank=True, default="", max_length=128)),
                (
                    "deployment_version",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                ("owner_token", models.UUIDField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(default=django.utils.timezone.now)),
            ],
            options={"db_table": "deployments_lifecyclerecoverylease"},
        ),
    ]
