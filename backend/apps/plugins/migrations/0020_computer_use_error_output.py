"""Upgrade the Computer Use runtime to 0.3.2 (output kept on errors, label fixes)."""

from django.db import migrations


def replace_version(apps, old: str, new: str) -> None:
    """Upgrade the bundled server while preserving customized runtime choices."""
    apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin_id="7ca41130-a36a-491d-83fa-cd912bcaaf52",
        slug="cua_repl",
        runtime_artifact={"id": "computer-use", "version": old},
    ).update(runtime_artifact={"id": "computer-use", "version": new})


def upgrade(apps, schema_editor) -> None:
    replace_version(apps, "0.3.1", "0.3.2")


def downgrade(apps, schema_editor) -> None:
    replace_version(apps, "0.3.2", "0.3.1")


class Migration(migrations.Migration):
    dependencies = [("plugins", "0019_computer_use_desktop_bus")]
    operations = [migrations.RunPython(upgrade, downgrade)]
