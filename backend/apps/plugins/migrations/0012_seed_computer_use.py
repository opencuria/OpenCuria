"""Ship Computer Use and retain activation for workspaces that used Agent-S."""

import uuid

from django.db import migrations

PLUGIN_ID = uuid.UUID("7ca41130-a36a-491d-83fa-cd912bcaaf52")


def seed(apps, schema_editor):
    plugin, _ = apps.get_model("plugins", "Plugin").objects.update_or_create(
        id=PLUGIN_ID,
        defaults={
            "organization": None,
            "name": "Computer Use",
            "slug": "computer-use",
            "description": (
                "Operate the workspace desktop with accessibility, "
                "screenshots and native input."
            ),
            "enabled": True,
            "published": True,
        },
    )
    apps.get_model("plugins", "PluginMcpServer").objects.update_or_create(
        plugin=plugin,
        slug="cua_repl",
        defaults={
            "name": "Computer Use",
            "transport": "stdio",
            "command": "opencuria-cua",
            "cwd": "/workspace",
            "args": [],
            "env": {},
            "resources": {"desktop": {"activation": "first_tool"}},
            "runtime_artifact": {"id": "computer-use", "version": "0.1.0"},
            "tool_policy": {
                "exposed_tools": ["js", "js_reset"],
                "internal_tools": ["turn_ended"],
                "desktop_tools": ["js"],
                "execution_tools": ["js", "js_reset"],
                "on_run_end": "turn_ended",
                "desktop_kind": "computeruse",
                "exclusive_input": True,
            },
            "startup_timeout_seconds": 120,
            "request_timeout_seconds": 75,
        },
    )
    workspace_ids = set(
        apps.get_model("harness", "HarnessSession")
        .objects.filter(agent_name="computeruse")
        .values_list("workspace_id", flat=True)
    )
    configured = apps.get_model("harness", "ComputerUseConfig").objects.values_list(
        "organization_id", flat=True
    )
    workspaces = (
        apps.get_model("runners", "Workspace")
        .objects.exclude(status="deleted")
        .filter(runner__organization_id__in=configured)
    )
    workspace_ids.update(workspaces.values_list("id", flat=True))
    org_activation = apps.get_model("plugins", "OrgPluginActivation")
    ws_activation = apps.get_model("plugins", "WorkspacePluginActivation")
    for workspace in (
        apps.get_model("runners", "Workspace")
        .objects.filter(id__in=workspace_ids)
        .select_related("runner")
    ):
        org_activation.objects.get_or_create(
            organization_id=workspace.runner.organization_id, plugin=plugin
        )
        ws_activation.objects.get_or_create(workspace=workspace, plugin=plugin)
    # Both UI automation providers share the same workspace input coordinator.
    apps.get_model("plugins", "PluginMcpServer").objects.filter(
        plugin__slug="playwright",
        plugin__organization__isnull=True,
    ).update(tool_policy={"exclusive_input": True})


def unseed(apps, schema_editor):
    apps.get_model("plugins", "Plugin").objects.filter(id=PLUGIN_ID).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("plugins", "0011_mcp_tool_policy"),
        ("harness", "0042_computer_use_config"),
    ]
    operations = [migrations.RunPython(seed, unseed)]
