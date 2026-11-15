"""Keep push signing keys exclusively in the control plane credential vault."""

from django.db import migrations


def protect_push_services(apps, schema_editor):
    service = apps.get_model("credentials", "CredentialService")
    service.objects.filter(
        organization__isnull=True,
        slug__in=["mobile-push-apns", "mobile-push-fcm"],
        name__in=["Mobile Push APNS", "Mobile Push FCM"],
    ).update(credential_type="server_secret", env_var_name="", target_path="")


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0003_mobiledevice_notifications_enabled_at"),
        ("credentials", "0012_alter_credentialservice_credential_type"),
    ]
    operations = [
        migrations.RunPython(protect_push_services, migrations.RunPython.noop)
    ]
