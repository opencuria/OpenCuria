"""Accessibility runtime upgrades retain customized server settings."""

import importlib

import pytest
from django.apps import apps

from apps.plugins.bundled.computer_use import PLUGIN_ID, VERSION
from apps.plugins.models import PluginMcpServer

migration = importlib.import_module(
    "apps.plugins.migrations.0015_computer_use_accessibility"
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "runtime",
    [{"id": "computer-use", "version": "0.2.0"}, {"id": "custom", "version": "1"}, {}],
)
def test_upgrade_is_idempotent_and_preserves_customization(runtime):
    server = PluginMcpServer.objects.get(plugin_id=PLUGIN_ID, slug="cua_repl")
    server.runtime_artifact = runtime
    server.save(update_fields=["runtime_artifact"])
    policy = server.tool_policy
    expected = (
        {"id": "computer-use", "version": VERSION}
        if runtime == {"id": "computer-use", "version": "0.2.0"}
        else runtime
    )
    migration.upgrade(apps, None)
    migration.upgrade(apps, None)
    server.refresh_from_db()
    assert server.runtime_artifact == expected
    assert server.tool_policy == policy
    migration.downgrade(apps, None)
    server.refresh_from_db()
    assert server.runtime_artifact == runtime
