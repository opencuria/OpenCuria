import uuid

from django.db import migrations, models


def _forward(apps, schema_editor):
    ProviderConfig = apps.get_model("harness", "ProviderConfig")
    AgentConfig = apps.get_model("harness", "AgentConfig")
    for pc in ProviderConfig.objects.all().iterator():
        org_id = pc.organization_id
        rows = [
            (
                "build",
                (pc.default_model or ""),
                (pc.default_effort or ""),
                False,
                "fixed",
            ),
            (
                "plan",
                (pc.default_model or ""),
                (pc.default_effort or ""),
                False,
                "fixed",
            ),
            (
                "computeruse",
                (pc.computer_use_model or ""),
                (pc.computer_use_effort or ""),
                False,
                "fixed",
            ),
            ("general", "", "", True, "inherit"),
            ("explore", "", "", True, "inherit"),
        ]
        cu_model = (pc.computer_use_model or "").strip()
        cu_effort = (pc.computer_use_effort or "").strip()
        rows = [
            (a, m, e, im, es) for (a, m, e, im, es) in rows if a != "computeruse"
        ]
        if cu_model or cu_effort:
            rows.append(("computeruse", cu_model, cu_effort, False, "fixed"))
        else:
            rows.append(("computeruse", "", "", True, "inherit"))
        for agent, model, effort, inherit_model, strategy in rows:
            AgentConfig.objects.update_or_create(
                organization_id=org_id,
                agent=agent,
                defaults={
                    "model": model,
                    "effort": effort,
                    "inherit_model": inherit_model,
                    "effort_strategy": strategy,
                },
            )


def _reverse(apps, schema_editor):
    AgentConfig = apps.get_model("harness", "AgentConfig")
    AgentConfig.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("harness", "0015_providerconfig_computer_use_effort_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentConfig",
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
                ("agent", models.CharField(help_text="Agent name (build, plan, ...).", max_length=64)),
                ("model", models.CharField(blank=True, default="", max_length=255)),
                ("effort", models.CharField(blank=True, default="", max_length=50)),
                ("inherit_model", models.BooleanField(default=False)),
                (
                    "effort_strategy",
                    models.CharField(
                        choices=[
                            ("fixed", "Fixed"),
                            ("inherit", "Inherit"),
                            ("lowest", "Lowest"),
                            ("medium", "Medium"),
                            ("highest", "Highest"),
                        ],
                        default="fixed",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "organization",
                    models.ForeignKey(
                        help_text="Owning organization.",
                        on_delete=models.CASCADE,
                        related_name="harness_agent_configs",
                        to="organizations.organization",
                    ),
                ),
            ],
            options={
                "db_table": "harness_agent_config",
                "ordering": ["agent"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=["organization", "agent"],
                        name="harness_agent_config_org_agent_uniq",
                    ),
                ],
            },
        ),
        migrations.RunPython(_forward, _reverse),
    ]
