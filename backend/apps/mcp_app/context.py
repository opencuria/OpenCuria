"""Workspace owner and organization scope for in-process MCP calls."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .errors import ToolError


class ContextError(Exception):
    """The workspace owner cannot act in the workspace's organization."""


@dataclass(frozen=True)
class ToolContext:
    """Identity and organization every workspace agent tool call runs with."""

    user: Any
    organization_id: uuid.UUID
    organization_name: str
    role: str
    workspace_id: uuid.UUID

    @property
    def is_admin(self) -> bool:
        """Whether the user administers the workspace's organization."""
        return self.role == "admin"

    def require_admin(self, action: str) -> None:
        """Raise a ToolError unless the user is an organization admin."""
        if not self.is_admin:
            raise ToolError(
                f"Only organization admins can {action}. The user is a "
                f"'{self.role}' of '{self.organization_name}'."
            )
