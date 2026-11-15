"""Dispatcher contract: validation and error mapping."""

from __future__ import annotations

import json
import uuid

from apps.mcp_app.context import ToolContext
from apps.mcp_app.errors import ToolError
from apps.mcp_app.registry import ToolSpec
from apps.mcp_app.schema import obj, string
from apps.mcp_app.server import call_tool
from common.exceptions import ConflictError, NotFoundError


class _User:
    id = 7
    email = "user@example.com"


def _ctx() -> ToolContext:
    return ToolContext(
        user=_User(),
        organization_id=uuid.uuid4(),
        organization_name="Acme",
        role="member",
        workspace_id=uuid.uuid4(),
    )


def _spec(handler) -> ToolSpec:
    return ToolSpec(
        name="skill_probe",
        title="Probe",
        description="A probe tool used by the dispatcher tests only.",
        input_schema=obj({"name": string("A name.")}, ("name",)),
        handler=handler,
        read_only=True,
    )


async def _echo(ctx, args):
    return {"name": args["name"], "id": uuid.UUID(int=1)}


async def test_successful_call_returns_json_text_and_structured_content():
    result = await call_tool(
        _ctx(),
        "skill_probe",
        {"name": "Ä"},
        tools=[_spec(_echo)],
    )

    assert result.isError is False
    assert result.structuredContent == {
        "name": "Ä",
        "id": "00000000-0000-0000-0000-000000000001",
    }
    assert json.loads(result.content[0].text) == result.structuredContent
    assert "Ä" in result.content[0].text  # no ASCII escaping


async def test_unknown_tool_and_bad_arguments_are_tool_errors():
    tools = [_spec(_echo)]

    unknown = await call_tool(_ctx(), "nope", {}, tools=tools)
    invalid = await call_tool(
        _ctx(),
        "skill_probe",
        {"name": "x", "nmae": "typo"},
        tools=tools,
    )
    missing = await call_tool(_ctx(), "skill_probe", {}, tools=tools)

    assert unknown.isError and "Unknown tool" in unknown.content[0].text
    assert invalid.isError and "'nmae'" in invalid.content[0].text
    assert (
        missing.isError and "'name' is a required property" in missing.content[0].text
    )


async def test_service_errors_become_actionable_messages():
    async def tool_error(ctx, args):
        raise ToolError("Call skill_list first.")

    async def not_found(ctx, args):
        raise NotFoundError("Skill", "abc")

    async def conflict(ctx, args):
        exc = ConflictError("Plugin selection is incomplete")
        exc.gaps = [{"service_name": "GitHub token", "plugin_name": "GitHub"}]
        raise exc

    ctx = _ctx()
    texts = []
    for handler in (tool_error, not_found, conflict):
        result = await call_tool(
            ctx, "skill_probe", {"name": "x"}, tools=[_spec(handler)]
        )
        assert result.isError
        texts.append(result.content[0].text)

    assert texts[0] == "Error: Call skill_list first."
    assert "Skill 'abc' not found" in texts[1]
    assert "GitHub token (for plugin GitHub)" in texts[2]
    assert "workspace_update" in texts[2]


async def test_unexpected_errors_do_not_leak_details():
    async def boom(ctx, args):
        raise RuntimeError("password=hunter2")

    result = await call_tool(
        _ctx(),
        "skill_probe",
        {"name": "x"},
        tools=[_spec(boom)],
    )

    assert result.isError
    assert "hunter2" not in result.content[0].text
    assert "failed unexpectedly" in result.content[0].text
