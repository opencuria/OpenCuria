"""Tool registry composed from the domain modules, in ``tools/list`` order.

Each domain module registers its tools with ``registrar(TOOLS)``; handlers
are thin adapters over the same services as the REST API.
"""

from __future__ import annotations

from ..registry import ToolSpec
from . import chats, context, plugins, scheduled_tasks, skills, workspaces

DOMAINS = (context, workspaces, chats, skills, plugins, scheduled_tasks)

TOOLS: list[ToolSpec] = [tool for domain in DOMAINS for tool in domain.TOOLS]
