"""Todo tools validate replacements and share persisted session state."""

from __future__ import annotations

import pytest

from apps.harness.models import HarnessSession
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.todos.domain import TodoItem
from apps.harness.todos.service import TodoService
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.todos import TodoWriteTool

pytestmark = pytest.mark.django_db(transaction=True)


async def test_todo_normalization_and_clear(
    fake_accessor: FakeAccessor, harness_session: HarnessSession
) -> None:
    """Default tools persist normalized todos visible to fresh service instances."""
    tool = TodoWriteTool()
    service = TodoService()
    context = ToolContext(
        session_id=str(harness_session.id),
        workspace_id=str(harness_session.workspace_id),
        accessor=fake_accessor,
    )
    statuses = ("pending", "in_progress", "completed", "cancelled")

    result = await tool.execute(
        {
            "todos": [
                {"content": f"  item {index} \n", "status": status, "priority": ""}
                for index, status in enumerate(statuses)
            ]
        },
        context,
    )

    assert result.output == "\n".join(
        f"[{status}] item {index}" for index, status in enumerate(statuses)
    )
    assert result.metadata == {"count": 4}
    assert [vars(item) for item in service.list(context.session_id).items] == [
        {
            "content": f"item {index}",
            "status": status,
            "priority": "medium",
            "order": index,
        }
        for index, status in enumerate(statuses)
    ]
    cleared = await tool.execute({"todos": []}, context)
    assert cleared.output == "Todo list cleared."
    assert cleared.metadata == {"count": 0}
    assert TodoService().list(context.session_id).items == []


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"content": " \n "}, "Todo content must not be empty"),
        (
            {"content": "invalid", "status": "unknown"},
            "Invalid status 'unknown'; expected one of "
            "pending, in_progress, completed, cancelled",
        ),
    ],
)
async def test_invalid_replacement_preserves_existing_todos(
    fake_accessor: FakeAccessor,
    harness_session: HarnessSession,
    entry: dict[str, str],
    message: str,
) -> None:
    """An invalid entry rejects the entire replacement with the same tool error."""
    service = TodoService()
    session_id = str(harness_session.id)
    service.replace(session_id, [TodoItem(content="keep me")])
    tool = TodoWriteTool()
    context = ToolContext(
        session_id=session_id,
        workspace_id=str(harness_session.workspace_id),
        accessor=fake_accessor,
    )

    with pytest.raises(ToolError) as error:
        await tool.execute({"todos": [{"content": "valid"}, entry]}, context)

    assert str(error.value) == message
    assert [item.content for item in service.list(session_id).items] == [
        "keep me"
    ]
