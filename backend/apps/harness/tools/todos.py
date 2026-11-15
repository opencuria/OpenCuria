"""Tool schema and result presentation for session todo replacements."""

from __future__ import annotations

from asgiref.sync import sync_to_async
from pydantic import BaseModel, Field

from ..todos.domain import TodoItem
from ..todos.service import TodoService, TodoValidationError
from .base import Tool, ToolContext, ToolError, ToolResult


class TodoEntryArgs(BaseModel):
    """A single todo entry in the write request."""

    content: str = Field(description="Todo description.")
    status: str = Field(default="pending")
    priority: str = Field(default="medium")


class TodoWriteArgs(BaseModel):
    """Arguments for the todowrite tool."""

    todos: list[TodoEntryArgs] = Field(description="Full todo list.")


class TodoWriteTool(Tool):
    """Replace the session todo list through the shared todo service."""

    name = "todowrite"
    description = (
        "Manage the session todo list. Pass the complete list; it "
        "replaces the previous one. Statuses: pending, in_progress, "
        "completed, cancelled."
    )
    args_schema: type[BaseModel] = TodoWriteArgs
    permission_key = "todowrite"

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
        entries = [
            TodoItem(
                content=entry.content, status=entry.status, priority=entry.priority
            )
            for entry in validated.todos
        ]
        try:
            stored = await sync_to_async(TodoService().replace)(ctx.session_id, entries)
        except TodoValidationError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        lines = [f"[{item.status}] {item.content}" for item in stored.items]
        return ToolResult(
            output="\n".join(lines) if lines else "Todo list cleared.",
            metadata={"count": len(stored.items)},
        )
