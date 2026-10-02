# OAuth authorization state pins provider registration metadata against mutation.
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("credentials", "0009_organization_service_help")]

    operations = [
        migrations.AddField(
            model_name="mcpoauthauthorizationstate",
            name="registration_fingerprint",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
    ]
