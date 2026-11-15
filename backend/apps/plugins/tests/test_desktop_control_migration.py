"""Desktop control adoption preserves explicit plugin exposure restrictions."""

import importlib

import pytest
from django.apps import apps

from apps.plugins.bundled.computer_use import PLUGIN_ID, TOOL_POLICY
from apps.plugins.models import PluginMcpServer

migration = importlib.import_module("apps.plugins.migrations.0014_desktop_control_tool")


@pytest.mark.django_db
@pytest.mark.parametrize("exposed", [["js", "js_reset"], ["js_reset"], None, []])
def test_upgrade_preserves_custom_tool_allowlists(exposed):
    server = PluginMcpServer.objects.get(plugin_id=PLUGIN_ID, slug="cua_repl")
    policy = {
        **TOOL_POLICY,
        "exposed_tools": exposed,
        "execution_tools": ["js", "js_reset"],
    }
    server.tool_policy = policy
    server.save(update_fields=["tool_policy"])
    migration.upgrade(apps, None)
    server.refresh_from_db()
    canonical = exposed == ["js", "js_reset"]
    assert server.tool_policy["exposed_tools"] == (
        ["js", "js_reset", "desktop"] if canonical else exposed
    )
    assert ("desktop" in server.tool_policy["execution_tools"]) == canonical
    adopted = dict(server.tool_policy)
    migration.upgrade(apps, None)
    server.refresh_from_db()
    assert server.tool_policy == adopted
    migration.downgrade(apps, None)
    server.refresh_from_db()
    assert server.tool_policy == policy
