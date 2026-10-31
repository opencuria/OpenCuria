"""Delete retired push credentials and their catalog entries without conversion."""

from django.db import migrations, models


def remove_push_credentials(apps, schema_editor):
    """Purge push secrets, activations and references, preserving other services."""
    alias = schema_editor.connection.alias
    services = apps.get_model("credentials", "CredentialService").objects.using(alias)
    retired = services.filter(
        models.Q(credential_type="server_secret")
        | models.Q(slug__in=["mobile-push-apns", "mobile-push-fcm"])
    )
    # Requirements protect their service; remove these obsolete references first.
    requirements = apps.get_model("plugins", "PluginCredentialRequirement")
    requirements.objects.using(alias).filter(credential_service__in=retired).delete()
    retired.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0012_alter_credentialservice_credential_type"),
        ("notifications", "0005_remove_pushconfiguration"),
        ("plugins", "0008_plugin_credential_separation"),
    ]

    operations = [
        migrations.RunPython(remove_push_credentials),
        migrations.AlterField(
            model_name="credentialservice",
            name="credential_type",
            field=models.CharField(
                choices=[
                    ("env", "Environment Variable"),
                    ("file", "Credential File"),
                    ("ssh_key", "SSH Key"),
                    ("mcp_oauth", "MCP OAuth (server-side only)"),
                    ("provider_secret", "Internal provider secret (server-side only)"),
                ],
                default="env",
                help_text="How this credential is injected into a workspace.",
                max_length=20,
            ),
        ),
    ]
