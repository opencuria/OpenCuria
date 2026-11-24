"""Store push proxy handles and device encryption keys instead of native tokens."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0006_drop_direct_push_registrations"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="mobiledevice",
            name="apns_environment",
        ),
        migrations.RemoveField(
            model_name="mobiledevice",
            name="app_id",
        ),
        migrations.RemoveField(
            model_name="mobiledevice",
            name="encrypted_activity_token",
        ),
        migrations.RemoveField(
            model_name="mobiledevice",
            name="encrypted_push_token",
        ),
        migrations.RemoveField(
            model_name="mobiledevice",
            name="push_token_hash",
        ),
        migrations.AddField(
            model_name="mobiledevice",
            name="encrypted_activity_handle",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="mobiledevice",
            name="encrypted_auth_secret",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="mobiledevice",
            name="encrypted_push_handle",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="mobiledevice",
            name="encryption_key",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="mobiledevice",
            name="push_handle_hash",
            field=models.CharField(
                blank=True, db_index=True, default="", max_length=64
            ),
        ),
    ]
