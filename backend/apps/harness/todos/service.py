"""Todo validation and storage selection shared by harness tool adapters."""

from __future__ import annotations

import uuid

from .domain import VALID_STATUSES, TodoItem, TodoList, TodoStore
from .memory_repository import InMemoryTodoRepository

_default_repository = InMemoryTodoRepository()


class TodoValidationError(ValueError):
    """A replacement contains an invalid todo entry."""


class TodoService:
    """Validate whole-list replacements before writing to the selected store."""

    def __init__(self, repository: TodoStore | None = None) -> None:
        self._repository = repository or _default_repository

    @classmethod
    def for_session(cls, session_id: str) -> TodoService:
        """Use Django for UUID sessions and shared memory for ephemeral sessions."""
        try:
            uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return cls()
        # Ephemeral todo services do not need Django. Load ORM-backed storage
        # only for persistent sessions; repositories never import consumers.
        from .repositories import TodoRepository

        return cls(repository=TodoRepository())

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
