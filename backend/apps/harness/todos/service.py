"""Todo validation and persistence shared by harness tool adapters."""

from __future__ import annotations

from .domain import VALID_STATUSES, TodoItem, TodoList
from .repositories import TodoRepository


class TodoValidationError(ValueError):
    """A replacement contains an invalid todo entry."""


class TodoService:
    """Validate whole-list replacements before persisting session todos."""

    def __init__(self) -> None:
        self._repository = TodoRepository()

    def list(self, session_id: str) -> TodoList:
        """Return the ordered todo list of a session."""
        return self._repository.list(session_id)

    def replace(self, session_id: str, entries: list[TodoItem]) -> TodoList:
        """Validate and normalize every entry before replacing the stored list."""
        items = []
        for order, entry in enumerate(entries):
            if not entry.content.strip():
                raise TodoValidationError("Todo content must not be empty")
            if entry.status not in VALID_STATUSES:
                raise TodoValidationError(
                    f"Invalid status '{entry.status}'; expected one of "
                    f"{', '.join(VALID_STATUSES)}"
                )
            items.append(
                TodoItem(
                    content=entry.content.strip(),
                    status=entry.status,
                    priority=entry.priority or "medium",
                    order=order,
                )
            )
        return self._repository.save(session_id, items)
