"""Volatile todo storage for sessions without persistent identities."""

from __future__ import annotations

import structlog

from .domain import TodoItem, TodoList, TodoStore

log = structlog.get_logger(__name__)


class InMemoryTodoRepository(TodoStore):
    """Per-process todo storage with isolated read snapshots."""

    def __init__(self) -> None:
        self._store: dict[str, TodoList] = {}

    def list(self, session_id: str) -> TodoList:
        """Return the stored list or an empty one."""
        stored = self._store.get(session_id)
        if stored is None:
            return TodoList(session_id=session_id)
        return TodoList(
            session_id=session_id,
            items=[TodoItem(**vars(item)) for item in stored.items],
        )

    def save(self, session_id: str, items: list[TodoItem]) -> TodoList:
        """Replace the stored list for *session_id*."""
        todo_list = TodoList(session_id=session_id, items=list(items))
        self._store[session_id] = todo_list
        log.debug("todos_saved", session_id=session_id, count=len(todo_list.items))
        return todo_list
