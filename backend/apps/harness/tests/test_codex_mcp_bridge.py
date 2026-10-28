"""Exercise direct MCP through the pinned Codex binary, including code-only models."""

import base64
import json
import struct
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent

from apps.harness.mcp_client.connection import McpTool, normalize_call_result
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.tests.test_codex_core import extension_registry, fixture_run
from apps.harness.tools.base import ToolError
from apps.plugins.bundled.computer_use import TOOL_POLICY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,deny,expected,fails",
    [
        ("build", False, 1, False),
        ("plan", False, 0, False),
        ("build", True, 0, False),
        ("build", False, 1, True),
    ],
)
async def test_native_mcp_stays_direct_and_preserves_media_and_permissions(
    tmp_path, mode, deny, expected, fails
):
    def chunk(name, data):
        return (
            struct.pack("!I", len(data))
            + name
            + data
            + struct.pack("!I", zlib.crc32(name + data))
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )
    encoded = base64.b64encode(png).decode()
    result = normalize_call_result(
        CallToolResult(
            content=[
                TextContent(type="text", text="documentation"),
                TextContent(type="text", text="state"),
                ImageContent(type="image", mimeType="image/png", data=encoded),
                TextContent(type="text", text="after image"),
            ],
            _meta={"codex/nodeReplExecutionDurationMs": 1},
        )
    )
    connection = SimpleNamespace(
        tool_policy=TOOL_POLICY,
        plugin_slug="computer-use",
        server_slug="cua_repl",
        call_tool=AsyncMock(return_value=result),
    )
    if fails:
        connection.call_tool.side_effect = ToolError(
            "example", content=[{"type": "text", "text": "example"}]
        )
    registry = extension_registry()
    registry.register(
        McpTool(
            namespaced_name="mcp_computer-use_cua_repl_js",
            original_name="js",
            description="Direct Computer Use",
            input_schema={
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            },
            connection=connection,
            title_text="Computer Use",
        )
    )
    _, events, accessor, requests, permissions, _ = await fixture_run(
        tmp_path,
        registry=registry,
        mode=mode,
        code_mode_only=True,
        tool="js",
        namespace="mcp__opencuria_computer_use_cua_repl",
        args={"code": "await cua.getState()"},
        evaluator=PermissionEvaluator(global_rules={"*": "deny"}) if deny else None,
    )
    (tmp_path / "requests.json").write_text(json.dumps(requests, indent=2))
    assert connection.call_tool.await_count == expected, {
        "tools": requests[0].get("tools"),
        "input": requests[-1].get("input"),
        "events": [
            e for e in events if e.get("type") in {"tool_error", "tool_completed"}
        ],
    }
    # Native MCP remains alongside exec for a code-mode-only provider model.
    advertised = next(r["tools"] for r in requests if r.get("model") == "~test/model")
    assert any(
        tool.get("name") == "mcp__opencuria_computer_use_cua_repl"
        and tool["tools"][0]["name"] == "js"
        for tool in advertised
    )
    assert not any(
        tool.get("name") == "mcp_computer-use_cua_repl_js" for tool in advertised
    )
    if expected and not fails:
        wire = requests[-1]["input"]
        rendered = str(wire)
        assert encoded in rendered
        assert (
            rendered.index("documentation")
            < rendered.index("state")
            < rendered.index(encoded)
            < rendered.index("after image")
        )
        assert any(
            e["type"] == "tool_completed"
            and e["tool"] == "mcp_computer-use_cua_repl_js"
            for e in events
        )
        assert len([e for e in events if e["type"] == "tool_completed"]) == 1
    elif fails:
        output = str(requests[-1]["input"][-1])
        assert "example" in output
        assert "args:" not in output and "failed:" not in output
        assert len([e for e in events if e["type"] == "tool_error"]) == 1
    if mode == "plan" and not deny:
        assert len(permissions) == 1
    assert all(stream.process.returncode is not None for stream in accessor.streams)
