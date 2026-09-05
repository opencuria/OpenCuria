"""Todo management tool with a swappable repository behind it.

M6 will introduce the persistent ``Todo`` model (``HarnessSession`` FK).
Until then this tool stores todos in memory. The indirection matters:
``TodoRepository`` is the only seam the tool talks to, so M6 can replace
``InMemoryTodoRepository`` with a Django-backed implementation without
touching :class:`TodoWriteTool`.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

import structlog
from pydantic import BaseModel, Field

from .base import Tool, ToolContext, ToolError, ToolResult

log = structlog.get_logger(__name__)

VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")


@dataclass
class TodoItem:
    """A single todo entry."""

    content: str
    status: str = "pending"
    priority: str = "medium"
    order: int = 0


@dataclass
class TodoList:
    """All todos of one session."""

    session_id: str
    items: list[TodoItem] = field(default_factory=list)


class TodoRepository(abc.ABC):
    """Persistence seam for session todos.

    M6 replaces the in-memory implementation with a Django repository
    backed by the ``Todo`` model. The tool code stays unchanged.
    """

    @abc.abstractmethod
    def list(self, session_id: str) -> TodoList:
        """Return the todo list for *session_id* (empty if none)."""

    @abc.abstractmethod
    def save(self, session_id: str, items: list[TodoItem]) -> TodoList:
        """Replace the todo list for *session_id* and return it."""


class InMemoryTodoRepository(TodoRepository):
    """Volatile per-process todo storage (v1 placeholder for M6)."""

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


# Module-level default store: one per backend process. M6 swaps this for
# a persistent repository behind the same interface.
default_todo_repository: TodoRepository = InMemoryTodoRepository()


class TodoEntryArgs(BaseModel):
    """A single todo entry in the write request."""

    content: str = Field(description="Todo description.")
    status: str = Field(default="pending")
    priority: str = Field(default="medium")


class TodoWriteArgs(BaseModel):
    """Arguments for the todowrite tool."""

    todos: list[TodoEntryArgs] = Field(description="Full todo list.")


class TodoWriteTool(Tool):
    """Replace the session todo list (stored via TodoRepository)."""

    name = "todowrite"
    description = (
        "Manage the session todo list. Pass the complete list; it "
        "replaces the previous one. Statuses: pending, in_progress, "
        "completed, cancelled."
    )
    args_schema: type[BaseModel] = TodoWriteArgs
    permission_key = "todowrite"

    def __init__(self, repository: TodoRepository | None = None) -> None:
        self._repository = repository or default_todo_repository

    def title(self, args: BaseModel) -> str:
        """Return a short title for a todowrite invocation."""
        assert isinstance(args, TodoWriteArgs)
        return f"Update todos ({len(args.todos)})"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Validate and store the todo list."""
        validated = self.coerce_args(args)
        assert isinstance(validated, TodoWriteArgs)
        args = validated
        items: list[TodoItem] = []
        for order, entry in enumerate(args.todos):
            if not entry.content.strip():
                raise ToolError("Todo content must not be empty", tool=self.name)
            if entry.status not in VALID_STATUSES:
                raise ToolError(
                    f"Invalid status '{entry.status}'; expected one of "
                    f"{', '.join(VALID_STATUSES)}",
                    tool=self.name,
                )
            items.append(
                TodoItem(
                    content=entry.content.strip(),
                    status=entry.status,
                    priority=entry.priority or "medium",
                    order=order,
                )
            )
        stored = self._repository.save(ctx.session_id, items)
        lines = [f"[{item.status}] {item.content}" for item in stored.items]
        return ToolResult(
            output="\n".join(lines) if lines else "Todo list cleared.",
            metadata={"count": len(stored.items)},
        )
