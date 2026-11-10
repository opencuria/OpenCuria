"""Declarative tool specifications and their registration decorator."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any

from mcp.types import Tool, ToolAnnotations

from .context import ToolContext

#: Handlers receive the caller context and validated arguments and return a
#: JSON-serializable dict; expected failures raise ``ToolError``.
ToolHandler = Callable[[ToolContext, dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    """One MCP tool: model-facing contract and handler.

    Members never see what only organization admins may do: ``admin_only``
    tools are hidden from them, and ``member_description`` /
    ``member_schema`` replace a contract that offers admin-only arguments.
    """

    name: str
    title: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    admin_only: bool = False
    member_description: str | None = None
    member_schema: dict[str, Any] | None = None

    def for_role(self, *, admin: bool) -> ToolSpec | None:
        """Return the contract a caller with this role gets, or None if hidden."""
        if admin:
            return self
        if self.admin_only:
            return None
        return replace(
            self,
            description=self.member_description or self.description,
            input_schema=self.member_schema or self.input_schema,
        )

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
        read_only: bool = False,
        destructive: bool = False,
        idempotent: bool = False,
        admin_only: bool = False,
        member_description: str | None = None,
        member_schema: dict[str, Any] | None = None,
    ) -> Callable[[ToolHandler], ToolHandler]:
        def decorate(handler: ToolHandler) -> ToolHandler:
            target.append(
                ToolSpec(
                    name=name,
                    title=title,
                    description=inspect.cleandoc(description),
                    input_schema=schema,
                    handler=handler,
                    read_only=read_only,
                    destructive=destructive,
                    idempotent=idempotent,
                    admin_only=admin_only,
                    member_description=inspect.cleandoc(member_description)
                    if member_description
                    else None,
                    member_schema=member_schema,
                )
            )
            return handler

        return decorate

    return tool
