"""Tests for MCP discovery caps, namespacing, schemas, and normalization."""

from __future__ import annotations

import base64
import json

import mcp.types as types
import pytest

from apps.harness.mcp_client.connection import (
    MAX_SCHEMA_SERIALIZED_BYTES,
    McpServerHealthError,
    McpTool,
    build_mcp_tool_title,
    check_name_collisions,
    normalize_call_result,
    validate_mcp_args,
)
from apps.harness.mcp_client.naming import (
    MAX_TOOL_NAME_LEN,
    TOOL_NAME_RE,
    mcp_tool_name,
)
from apps.harness.tools.base import ToolError, ToolRegistry


def _text_result(*texts: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=t) for t in texts],
        isError=is_error,
    )


def test_tool_namespacing_deterministic_and_bounded():
    first = mcp_tool_name("my-plugin", "main-server", "do_thing")
    assert first == mcp_tool_name("my-plugin", "main-server", "do_thing")
    assert first == "mcp_my-plugin_main-server_do_thing"
    assert TOOL_NAME_RE.match(first)
    long_name = mcp_tool_name("p" * 40, "s" * 40, "t" * 40)
    assert len(long_name) <= MAX_TOOL_NAME_LEN
    assert TOOL_NAME_RE.match(long_name)
    weird = mcp_tool_name("plug in!", "srv@x", "tool with spaces & stuff")
    assert TOOL_NAME_RE.match(weird)


def test_name_collisions_fail_closed():
    with pytest.raises(McpServerHealthError):
        check_name_collisions(["mcp_a_b_c", "mcp_a_b_c"])
    with pytest.raises(McpServerHealthError):
        check_name_collisions(["mcp_a_b_c"], existing=["Read", "MCP_A_B_C"])
    check_name_collisions(["mcp_a_b_c"], existing=["read", "bash"])


def test_args_validated_against_original_schema_not_lossy():
    schema = {
        "type": "object",
        "properties": {
            "count": {"type": "integer", "minimum": 2, "maximum": 5},
            "mode": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["count"],
        "additionalProperties": False,
    }
    validate_mcp_args(schema, {"count": 3, "mode": "a"})
    with pytest.raises(ToolError):
        validate_mcp_args(schema, {"count": 9, "mode": "a"})
    with pytest.raises(ToolError):
        validate_mcp_args(schema, {"mode": "a"})
    with pytest.raises(ToolError):
        validate_mcp_args(schema, {"count": 3, "extra": 1})


def test_provider_schema_is_original_input_schema():
    tool = McpTool(
        namespaced_name="mcp_p_s_echo",
        original_name="echo",
        description="echo it",
        input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
        connection=None,  # type: ignore[arg-type]
        title_text=build_mcp_tool_title("P", "S", "echo"),
    )
    assert tool.permission_key == "mcp_p_s_echo"
    assert tool.title(tool.coerce_args({})) == "P / S / echo"
    assert tool.parameters_schema() == {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }
    registry = ToolRegistry()
    registry.register(tool)
    schemas = registry.schemas()
    assert schemas[0].parameters["properties"]["x"] == {"type": "integer"}


def test_normalize_text_and_structured():
    result = _text_result("hello", "world")
    normalized = normalize_call_result(result)
    assert normalized.output == "hello\nworld"
    structured = types.CallToolResult(
        content=[],
        structuredContent={"answer": 42},
    )
    assert json.loads(normalize_call_result(structured).output) == {"answer": 42}
    with pytest.raises(ToolError, match="boom"):
        normalize_call_result(_text_result("boom", is_error=True))


def test_normalize_resource_link_and_embedded():
    result = types.CallToolResult(
        content=[
            types.ResourceLink(
                type="resource_link",
                uri="file:///workspace/a.txt",
                name="a.txt",
            )
        ],
    )
    assert "a.txt" in normalize_call_result(result).output
    embedded = types.CallToolResult(
        content=[
            types.EmbeddedResource(
                type="resource",
                resource=types.TextResourceContents(
                    uri="file:///workspace/b.txt",
                    mimeType="text/plain",
                    text="file body here",
                ),
            )
        ],
    )
    assert "file body here" in normalize_call_result(embedded).output


def test_normalize_jpeg_image_maps_to_image_jpeg():
    tiny_jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    )
    result = types.CallToolResult(
        content=[
            types.ImageContent(
                type="image",
                data=base64.b64encode(tiny_jpeg).decode(),
                mimeType="image/jpeg",
            )
        ],
    )
    normalized = normalize_call_result(result)
    assert normalized.image_jpeg == tiny_jpeg
    assert normalized.attachments[0]["url"] == (
        f"data:image/jpeg;base64,{base64.b64encode(tiny_jpeg).decode()}"
    )
    png = types.CallToolResult(
        content=[
            types.ImageContent(
                type="image",
                data=base64.b64encode(b"fakepng").decode(),
                mimeType="image/png",
            )
        ],
    )
    out = normalize_call_result(png)
    assert out.image_jpeg is None
    assert out.attachments[0]["mime"] == "image/png"
    from apps.harness.images import (
        build_tool_message_content,
        select_persisted_tool_attachments,
    )

    parts = build_tool_message_content(out.output, out.attachments)
    assert parts[1]["type"] == "image_url"
    assert select_persisted_tool_attachments(out.attachments) == out.attachments


def test_normalize_oversize_image_is_descriptor():
    from apps.harness.mcp_client.connection import MAX_MCP_IMAGE_BYTES

    big = b"a" * (MAX_MCP_IMAGE_BYTES + 8)
    result = types.CallToolResult(
        content=[
            types.ImageContent(
                type="image",
                data=base64.b64encode(big).decode(),
                mimeType="image/jpeg",
            )
        ],
    )
    out = normalize_call_result(result)
    assert out.image_jpeg is None
    assert out.attachments == []
    assert "exceeds" in out.output


def test_normalize_mcp_image_rejects_unsupported_and_caps_count():
    encoded = base64.b64encode(b"image").decode()
    result = types.CallToolResult(
        content=[
            types.ImageContent(type="image", data=encoded, mimeType="image/svg+xml"),
            *[
                types.ImageContent(type="image", data=encoded, mimeType="image/png")
                for _ in range(3)
            ],
        ]
    )
    out = normalize_call_result(result)
    assert len(out.attachments) == 2
    assert "not supported" in out.output
    assert "omitted" in out.output


def test_discovery_caps_constants_sane():
    from apps.harness.mcp_client import connection as conn

    assert conn.MAX_DISCOVERY_PAGES == 1000
    assert conn.MAX_TOOLS_PER_SERVER == 256
    assert MAX_SCHEMA_SERIALIZED_BYTES == 256 * 1024
