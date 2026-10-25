"""Framework-independent todo records and storage contract."""

from __future__ import annotations

import abc
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


class TodoStore(abc.ABC):
    """Storage port shared by persistent and ephemeral todo sessions."""

    @abc.abstractmethod
    def list(self, session_id: str) -> TodoList:
        """Return the todo list for *session_id* (empty if none)."""

    @abc.abstractmethod
    def save(self, session_id: str, items: list[TodoItem]) -> TodoList:
        """Replace the todo list for *session_id* and return it."""
