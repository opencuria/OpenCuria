"""Upgrade the immutable Computer Use runtime to the chat-scoped 0.3.0 driver."""

from django.db import migrations


def replace_version(apps, old: str, new: str) -> None:
    """Upgrade the bundled server while preserving customized runtime choices."""
    apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin_id="7ca41130-a36a-491d-83fa-cd912bcaaf52",
        slug="cua_repl",
        runtime_artifact={"id": "computer-use", "version": old},
    ).update(runtime_artifact={"id": "computer-use", "version": new})


def upgrade(apps, schema_editor) -> None:
    replace_version(apps, "0.2.1", "0.3.0")


def downgrade(apps, schema_editor) -> None:
    replace_version(apps, "0.3.0", "0.2.1")


class Migration(migrations.Migration):
    dependencies = [("plugins", "0017_personal_plugins_and_skills")]
    operations = [migrations.RunPython(upgrade, downgrade)]
