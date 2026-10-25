"""Characterize todo validation, storage selection and replacement contracts."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.repositories import HarnessSessionRepository
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.todos.domain import TodoItem
from apps.harness.todos.memory_repository import InMemoryTodoRepository
from apps.harness.todos.service import TodoService
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.todos import TodoWriteTool
from apps.runners.models import Workspace


async def test_todo_normalization_and_clear(fake_accessor: FakeAccessor) -> None:
    """Writes preserve status/priority and normalize content and list order."""
    repository = InMemoryTodoRepository()
    tool = TodoWriteTool(service=TodoService(repository=repository))
    context = ToolContext(
        session_id="todo-contract", workspace_id="workspace", accessor=fake_accessor
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
    assert [vars(item) for item in repository.list(context.session_id).items] == [
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
    assert repository.list(context.session_id).items == []


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
    fake_accessor: FakeAccessor, entry: dict[str, str], message: str
) -> None:
    """An invalid entry rejects the entire replacement with the same tool error."""
    repository = InMemoryTodoRepository()
    repository.save("todo-contract", [TodoItem(content="keep me")])
    tool = TodoWriteTool(service=TodoService(repository=repository))
    context = ToolContext(
        session_id="todo-contract", workspace_id="workspace", accessor=fake_accessor
    )

    with pytest.raises(ToolError) as error:
        await tool.execute({"todos": [{"content": "valid"}, entry]}, context)

    assert str(error.value) == message
    assert [item.content for item in repository.list(context.session_id).items] == [
        "keep me"
    ]


def test_ephemeral_todos_share_storage_and_isolate_sessions() -> None:
    """Separate resolutions share session state; reads cannot mutate stored rows."""
    session_id = f"ephemeral-{uuid.uuid4()}"
    first = TodoService.for_session(session_id)
    first.replace(session_id, [TodoItem(content="original")])
    second = TodoService.for_session(session_id)

    snapshot = second.list(session_id)
    snapshot.items[0].content = "mutated"
    snapshot.items.clear()

    assert second.list(session_id).items == [TodoItem(content="original")]
    assert second.list(f"other-{session_id}").items == []
    first.replace(session_id, [])


@pytest.mark.django_db
def test_persistent_todos_survive_store_resolution(
    harness_workspace: Workspace,
) -> None:
    """Native/Claude resolutions observe the same ordered replacement and clear."""
    workspace = harness_workspace
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        title="Todo persistence contract",
    )
    session_id = str(session.id)
    first = TodoService.for_session(session_id)
    second = TodoService.for_session(session_id)

    first.replace(session_id, [TodoItem(content="first"), TodoItem(content="second")])
    assert [item.content for item in second.list(session_id).items] == [
        "first",
        "second",
    ]
    second.replace(session_id, [TodoItem(content="replacement", priority="high")])
    assert first.list(session_id).items == [
        TodoItem(content="replacement", priority="high")
    ]
    first.replace(session_id, [])
    assert second.list(session_id).items == []
