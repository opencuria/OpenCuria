"""Django persistence for session todos."""

from __future__ import annotations

import uuid

from ..models import Todo
from .domain import TodoItem, TodoList, TodoStore


class TodoRepository(TodoStore):
    """Persist replacements and expose ordered todo records for presentation."""

    model = Todo

    @staticmethod
    def list_for_session(session_id: uuid.UUID) -> list[Todo]:
        """Return all todos of a session in display order."""
        return list(
            Todo.objects.filter(session_id=session_id).order_by("order", "created_at")
        )

    def list(self, session_id: str) -> TodoList:
        """Return the todo list for *session_id* (empty if none)."""
        try:
            session_uuid = uuid.UUID(str(session_id))
        except (TypeError, ValueError):
            return TodoList(session_id=str(session_id))
        return TodoList(
            session_id=str(session_id),
            items=[
                TodoItem(
                    content=row.content,
                    status=row.status,
                    priority=row.priority or "medium",
                    order=row.order,
                )
                for row in self.list_for_session(session_uuid)
            ],
        )

    def save(self, session_id: str, items: list[TodoItem]) -> TodoList:
        """Replace the todo list for *session_id* and return it."""
        try:
            session_uuid = uuid.UUID(str(session_id))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Todo save needs a real session UUID, got {session_id!r}"
            ) from exc
        Todo.objects.filter(session_id=session_uuid).delete()
        for order, item in enumerate(items):
            Todo.objects.create(
                session_id=session_uuid,
                content=item.content,
                status=item.status,
                priority=item.priority or "medium",
                order=order,
            )
        return self.list(str(session_id))
