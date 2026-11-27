"""The in-process MCP server through which Claude Code reaches OpenCuria tools.

``create_sdk_mcp_server`` freezes its tool list. Plugins enabled while an
agent works add tools mid-run, so this server keeps the list mutable: the
engine swaps the definitions and asks the CLI to reconnect, which re-reads
``tools/list``. Tool calls behave exactly like the SDK's own server (argument
validation, error results, content mapping); the helpers come from the
pinned SDK (``catalog.SDK_VERSION``) and are covered by tests.
"""

from __future__ import annotations

from typing import Any

import jsonschema
from claude_agent_sdk import McpSdkServerConfig, SdkMcpTool
from claude_agent_sdk import _build_input_schema as build_input_schema
from claude_agent_sdk import _build_meta as build_meta
from claude_agent_sdk import _convert_tool_content as convert_tool_content
from claude_agent_sdk import _tool_error_result as tool_error_result
from claude_agent_sdk._internal._mcp_compat import build_tool_server
from mcp.types import CallToolResult, Tool

from .servers import SERVER_NAME


class DynamicSdkServer:
    """An SDK MCP server whose tool list can change while the CLI runs."""

    def __init__(self, name: str = SERVER_NAME) -> None:
        self.name = name
        #: Listed to the CLI by reference; replaced in place.
        self._wire_tools: list[Tool] = []
        self._definitions: dict[str, SdkMcpTool[Any]] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self.config: McpSdkServerConfig = McpSdkServerConfig(
            type="sdk",
            name=name,
            instance=build_tool_server(name, "1.0.0", self._wire_tools, self._run),
        )

    @property
    def tool_names(self) -> list[str]:
        """Names currently listed to the CLI."""
        return [tool.name for tool in self._wire_tools]

    def set_tools(self, definitions: list[SdkMcpTool[Any]]) -> bool:
        """Replace the listed tools; return whether any listed tool changed."""
        before = [tool.model_dump() for tool in self._wire_tools]
        self._definitions = {definition.name: definition for definition in definitions}
        self._schemas = {
            definition.name: build_input_schema(definition)
            for definition in definitions
        }
        self._wire_tools[:] = [
            Tool.model_validate(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "inputSchema": self._schemas[definition.name],
                    "annotations": definition.annotations,
                    "_meta": build_meta(definition),
                }
            )
            for definition in definitions
        ]
        return [tool.model_dump() for tool in self._wire_tools] != before

    async def _run(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        definition = self._definitions.get(name)
        if definition is None:
            return tool_error_result(f"Tool '{name}' not found")
        try:
            try:
                jsonschema.validate(instance=arguments, schema=self._schemas[name])
            except jsonschema.ValidationError as exc:
                return tool_error_result(f"Input validation error: {exc.message}")
            result = await definition.handler(arguments)
            return CallToolResult.model_validate(
                {
                    "content": convert_tool_content(result.get("content", [])),
                    "isError": result.get("is_error", False),
                }
            )
        except Exception as exc:
            return tool_error_result(str(exc))
