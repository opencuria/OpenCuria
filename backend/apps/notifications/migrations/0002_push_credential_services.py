"""Seed push providers in the existing credential catalog."""

from django.db import migrations


def seed_services(apps, schema_editor):
    service = apps.get_model("credentials", "CredentialService")
    activation = apps.get_model("credentials", "OrgCredentialServiceActivation")
    organization = apps.get_model("organizations", "Organization")
    for provider, name in (
        ("apns", "Apple Push Notification service"),
        ("fcm", "Firebase Cloud Messaging"),
    ):
        seeded, _ = service.objects.get_or_create(
            slug=f"mobile-push-{provider}",
            organization__isnull=True,
            defaults={
                "name": f"Mobile Push {provider.upper()}",
                "description": (
                    f"{name} server credential JSON. Select an organization "
                    "credential in mobile notification configuration; "
                    "do not attach it to a workspace."
                ),
                "credential_type": "server_secret",
                "label": "Server credential JSON",
            },
        )
        activation.objects.bulk_create(
            [
                activation(organization_id=org_id, credential_service=seeded)
                for org_id in organization.objects.values_list("id", flat=True)
            ],
            ignore_conflicts=True,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0001_initial"),
        ("credentials", "0011_provider_accounts"),
    ]
    # Credentials may reference these services; do not delete them on rollback.
    operations = [migrations.RunPython(seed_services, migrations.RunPython.noop)]
