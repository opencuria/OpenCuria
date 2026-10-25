"""Declarative tool specifications and their registration decorator."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from mcp.types import Tool, ToolAnnotations

from apps.accounts.models import APIKeyPermission

from .context import ToolContext

#: Handlers receive the caller context and validated arguments and return a
#: JSON-serializable dict; expected failures raise ``ToolError``.
ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool: model-facing contract, required permission, handler."""

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    permission: APIKeyPermission
    handler: ToolHandler
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False

    def to_mcp(self) -> Tool:
        """Return the ``tools/list`` entry including behavior annotations."""
        return Tool(
            name=self.name,
            title=self.title,
            description=self.description,
            inputSchema=self.input_schema,
            annotations=ToolAnnotations(
                title=self.title,
                readOnlyHint=self.read_only,
                destructiveHint=self.destructive,
                idempotentHint=self.idempotent or self.read_only,
                openWorldHint=False,
            ),
        )


def registrar(
    target: list[ToolSpec],
) -> Callable[..., Callable[[ToolHandler], ToolHandler]]:
    """Return a decorator that appends a ``ToolSpec`` for a handler to *target*."""

    def tool(
        name: str,
        *,
        title: str,
        description: str,
        schema: dict[str, Any],
        permission: APIKeyPermission,
        read_only: bool = False,
        destructive: bool = False,
        idempotent: bool = False,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorate(handler: ToolHandler) -> ToolHandler:
            target.append(
                ToolSpec(
                    name=name,
                    title=title,
                    description=inspect.cleandoc(description),
                    input_schema=schema,
                    permission=permission,
                    handler=handler,
                    read_only=read_only,
                    destructive=destructive,
                    idempotent=idempotent,
                )
            )
            return handler

        return decorate

    return tool
