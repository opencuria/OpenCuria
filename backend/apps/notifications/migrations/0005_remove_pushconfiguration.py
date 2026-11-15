"""Remove organization-owned push provider configuration."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0004_server_only_push_credentials"),
    ]

    operations = [migrations.DeleteModel(name="PushConfiguration")]
