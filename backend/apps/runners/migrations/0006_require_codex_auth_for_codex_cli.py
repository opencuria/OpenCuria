import uuid

from django.db import migrations


CODEX_AGENT_ID = uuid.UUID("f8168b23-f56a-42dd-bef2-15301148b382")
CODEX_CREDENTIAL_SERVICE_ID = uuid.UUID("74fb7e0b-5f0f-4803-a489-4a39124ad24b")


def require_codex_auth_for_codex_cli(apps, schema_editor):
    agent_model = apps.get_model("runners", "AgentDefinition")
    credential_service_model = apps.get_model("credentials", "CredentialService")

    agent = (
        agent_model.objects.filter(id=CODEX_AGENT_ID, organization__isnull=True).first()
        or agent_model.objects.filter(name="Codex CLI", organization__isnull=True).first()
    )
    if agent is None:
        return

    service = (
        credential_service_model.objects.filter(id=CODEX_CREDENTIAL_SERVICE_ID).first()
        or credential_service_model.objects.filter(slug="openai-codex-auth").first()
    )
    if service is None:
        return

    agent.required_credential_services.add(service)


def unrequire_codex_auth_for_codex_cli(apps, schema_editor):
    agent_model = apps.get_model("runners", "AgentDefinition")
    credential_service_model = apps.get_model("credentials", "CredentialService")

    agent = (
        agent_model.objects.filter(id=CODEX_AGENT_ID, organization__isnull=True).first()
        or agent_model.objects.filter(name="Codex CLI", organization__isnull=True).first()
    )
    if agent is None:
        return

    service = (
        credential_service_model.objects.filter(id=CODEX_CREDENTIAL_SERVICE_ID).first()
        or credential_service_model.objects.filter(slug="openai-codex-auth").first()
    )
    if service is None:
        return

    agent.required_credential_services.remove(service)


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0003_credentialservice_target_path_and_more"),
        ("runners", "0005_fix_agent_command_template_defaults"),
    ]

    operations = [
        migrations.RunPython(
            require_codex_auth_for_codex_cli,
            unrequire_codex_auth_for_codex_cli,
        ),
    ]
