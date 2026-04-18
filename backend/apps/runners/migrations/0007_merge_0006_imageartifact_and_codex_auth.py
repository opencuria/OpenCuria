from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("runners", "0006_remove_imageartifact_credentials"),
        ("runners", "0006_require_codex_auth_for_codex_cli"),
    ]

    operations = []
