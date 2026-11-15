"""Framework-independent session todo records."""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")


@dataclass
class TodoItem:
    """A single todo entry in display order."""

    content: str
    status: str = "pending"
    priority: str = "medium"
    order: int = 0


@dataclass
class TodoList:
    """All todos of one session."""

    session_id: str
    items: list[TodoItem] = field(default_factory=list)
