"""Accessibility runtime upgrades retain customized server settings."""

import importlib

import pytest
from django.apps import apps

from apps.plugins.bundled.computer_use import PLUGIN_ID, VERSION
from apps.plugins.models import PluginMcpServer

MIGRATIONS = {
    # migration module: (upgraded version, target version)
    "0015_computer_use_accessibility": ("0.2.0", "0.2.1"),
    "0018_computer_use_session_runtime": ("0.2.1", "0.3.0"),
    "0019_computer_use_desktop_bus": ("0.3.0", "0.3.1"),
    "0020_computer_use_error_output": ("0.3.1", "0.3.2"),
    "0021_computer_use_toggle_labels": ("0.3.2", "0.3.3"),
    "0022_computer_use_regions_terminal": ("0.3.3", VERSION),
}


@pytest.mark.django_db
@pytest.mark.parametrize("name", sorted(MIGRATIONS))
@pytest.mark.parametrize("custom", [False, True, None])
def test_upgrade_is_idempotent_and_preserves_customization(name, custom):
    migration = importlib.import_module(f"apps.plugins.migrations.{name}")
    old, new = MIGRATIONS[name]
    runtime = (
        {"id": "computer-use", "version": old}
        if custom is False
        else {"id": "custom", "version": "1"}
        if custom
        else {}
    )
    server = PluginMcpServer.objects.get(plugin_id=PLUGIN_ID, slug="cua_repl")
    server.runtime_artifact = runtime
    server.save(update_fields=["runtime_artifact"])
    policy = server.tool_policy
    expected = {"id": "computer-use", "version": new} if custom is False else runtime
    migration.upgrade(apps, None)
    migration.upgrade(apps, None)
    server.refresh_from_db()
    assert server.runtime_artifact == expected
    assert server.tool_policy == policy
    migration.downgrade(apps, None)
    server.refresh_from_db()
    assert server.runtime_artifact == runtime
