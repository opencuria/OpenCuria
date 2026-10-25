"""Guards for the model-facing tool contract: few, consistent, well described."""

from __future__ import annotations

import re

import jsonschema

from apps.mcp_app.tools import TOOLS

PREFIXES = ("opencuria_", "model_", "workspace_", "chat_", "skill_", "plugin_")
PREFIXES += ("scheduled_task_",)
READ_ONLY_VERBS = ("_list", "_get", "_read", "_options", "_context")


def test_tool_set_stays_small_and_uniquely_named():
    names = [tool.name for tool in TOOLS]
    assert 25 <= len(names) <= 40
    assert len(names) == len(set(names))
    for name in names:
        assert re.fullmatch(r"[a-z][a-z_]{2,63}", name), name
        assert name.startswith(PREFIXES), f"{name} lacks a resource prefix"


def test_core_domains_are_covered():
    names = {tool.name for tool in TOOLS}
    for domain in ("workspace", "chat", "skill", "plugin", "scheduled_task"):
        assert f"{domain}_list" in names
    assert {"chat_read", "chat_start", "chat_send", "chat_respond"} <= names
    assert {"skill_create", "skill_update", "skill_delete"} <= names
    assert {"plugin_create", "plugin_update", "plugin_set_active"} <= names
    assert {"scheduled_task_create", "scheduled_task_run_now"} <= names


def _walk_properties(schema: dict, path: str):
    for name, prop in schema.get("properties", {}).items():
        yield f"{path}.{name}", prop
        if prop.get("type") == "object":
            yield from _walk_properties(prop, f"{path}.{name}")
        items = prop.get("items")
        if isinstance(items, dict) and items.get("type") == "object":
            yield from _walk_properties(items, f"{path}.{name}[]")


def test_every_tool_has_a_strict_documented_schema():
    for tool in TOOLS:
        schema = tool.input_schema
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, tool.name
        assert set(schema.get("required", [])) <= set(schema["properties"])
        for path, prop in _walk_properties(schema, tool.name):
            if prop.get("type") == "object" and "additionalProperties" in prop:
                if prop["additionalProperties"] is not False:
                    continue
            assert prop.get("description"), f"{path} needs a description"


def test_descriptions_explain_purpose_and_titles_are_set():
    for tool in TOOLS:
        assert tool.title and tool.title[0].isupper(), tool.name
        assert len(tool.description) >= 40, tool.name
        assert "\n " not in tool.description, f"{tool.name} keeps indentation"


def test_annotations_match_behavior():
    for tool in TOOLS:
        annotations = tool.to_mcp().annotations
        assert annotations.openWorldHint is False
        assert annotations.destructiveHint is tool.name.endswith("_delete"), tool.name
        assert annotations.readOnlyHint is tool.name.endswith(READ_ONLY_VERBS), (
            tool.name
        )
        if annotations.readOnlyHint:
            assert annotations.idempotentHint is True
