"""Characterize native projection routing, live run inputs and media failures."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from apps.harness.access.file_types import FileContent
from apps.harness.agents.definitions import get_agent
from apps.harness.conversation_service import ConversationService
from apps.harness.engines.base import RunOptions
from apps.harness.engines.codex.engine import CodexEngine
from apps.harness.engines.codex.events import CodexEventProjector
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from apps.harness.schemas import timeline_to_out
from apps.harness.tests.conftest import TINY_PNG
from apps.harness.tests.test_codex_core_safety import auth
from apps.harness.tools.base import ToolRegistry


def test_projection_has_no_application_dependencies() -> None:
    """Include type-only and local imports so the engine cannot creep back in."""
    path = Path(__file__).resolve().parents[1] / "engines" / "codex" / "events.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            violations.extend(
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] not in sys.stdlib_module_names
            )
        elif isinstance(node, ast.ImportFrom):
            if (
                node.level
                or (node.module or "").split(".")[0] not in sys.stdlib_module_names
            ):
                violations.append(f"{'.' * node.level}{node.module or ''}")
    assert violations == [], (
        "Codex event projection must depend only on its injected ports and "
        "the standard library, never on engines, permissions or workspace IO: "
        + ", ".join(violations)
    )


async def test_projection_runs_with_only_event_and_media_ports() -> None:
    """Display mapping needs checked attachments, never an engine or accessor."""
    emit = AsyncMock()
    attachment = {"type": "file", "filename": "generated.png"}
    load_images = AsyncMock(return_value=[attachment])
    projector = CodexEventProjector(
        emit=emit,
        model=lambda: "chatgpt/parent-model",
        is_managed_tool=lambda name: name == "question",
        image_attachments=load_images,
    )
    await projector.item(
        {"id": "managed", "type": "dynamicToolCall", "tool": "question"},
        completed=True,
    )
    emit.assert_not_awaited()
    load_images.assert_not_awaited()
    item = {"id": "image", "type": "imageGeneration", "savedPath": "/workspace/a.png"}
    await projector.item(item, completed=True, parent="spawn")
    load_images.assert_awaited_once_with(item)
    assert [call.args[0]["type"] for call in emit.await_args_list] == [
        "tool_queued",
        "tool_started",
        "tool_queued",
        "tool_completed",
    ]
    assert all(call.args[1] == "spawn" for call in emit.await_args_list)
    assert emit.await_args_list[-1].args[0]["attachments"] == [attachment]


@pytest.mark.parametrize("child_handler", [False, True])
@pytest.mark.parametrize("route", ["root", "explicit_child", "scoped_child"])
async def test_projection_routes_events_and_redacts_current_secrets(
    child_handler: bool, route: str
) -> None:
    """Explicit children cannot leak to root; managed scopes keep their fallback."""
    root, child = AsyncMock(), AsyncMock()
    engine = CodexEngine(
        tools=ToolRegistry(),
        accessor=None,
        auth=auth(),
        emit=root,
        on_child_event=child if child_handler else None,
    )
    # Refreshed credentials replace the inputs after projector construction.
    engine.secret_values = ["refreshed-private-value"]
    token = engine._child_scope.set("spawn" if route == "scoped_child" else None)
    try:
        await engine.projector.item(
            {
                "id": "search",
                "type": "webSearch",
                "query": "refreshed-private-value",
                "results": ["refreshed-private-value"],
            },
            completed=True,
            parent="spawn" if route == "explicit_child" else None,
        )
    finally:
        engine._child_scope.reset(token)
    if route != "root" and child_handler:
        root.assert_not_awaited()
        assert child.await_count == 4
        assert all(call.args[0] == "spawn" for call in child.await_args_list)
        events = [call.args[1] for call in child.await_args_list]
    else:
        child.assert_not_awaited()
        if route == "explicit_child":
            root.assert_not_awaited()
            return
        assert root.await_count == 4
        events = [call.args[0] for call in root.await_args_list]
    assert events[-1]["type"] == "tool_completed"
    assert events[-1]["output"] == '{"results": ["[redacted]"], "action": null}'
    assert "refreshed-private-value" not in str(events)


async def test_projection_uses_current_run_inputs_and_replays_early_children() -> None:
    """Projection observes run setup and binds early text to the native child."""
    root, child = AsyncMock(), AsyncMock()
    engine = CodexEngine(
        tools=ToolRegistry(),
        accessor=None,
        auth=auth(),
        emit=root,
        on_child_event=child,
    )
    engine.allowed = {"question"}
    await engine.projector.item(
        {"id": "managed", "type": "dynamicToolCall", "tool": "question"},
        completed=True,
    )
    root.assert_not_awaited()
    assert engine.projector.calls == {}
    await engine.projector.child_notification(
        {
            "method": "item/completed",
            "params": {
                "threadId": "native-child",
                "item": {"id": "answer", "type": "agentMessage", "text": "Found it"},
            },
        }
    )
    engine.model = "openrouter/parent-model"
    await engine.projector.collaboration(
        {
            "id": "spawn",
            "receiverThreadIds": ["native-child"],
            "model": "~child-model",
            "prompt": "Research",
        }
    )
    started = root.await_args_list[0].args[0]
    assert started["type"] == "subtask_started"
    assert started["model"] == "openrouter/child-model"
    child.assert_awaited_once_with(
        "spawn", {"type": "part_updated", "step": 1, "delta": {"text": "Found it"}}
    )
    assert engine.projector.early == []
    await engine.projector.finish_child("native-child", "completed", "Done")
    await engine.projector.finish_child("native-child", "completed", "Duplicate")
    assert root.await_count == 2
    assert root.await_args_list[-1].args[0]["summary"] == "Done"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("kind", "prompt"),
    [
        pytest.param("subAgentActivity", None, id="activity-without-instructions"),
        pytest.param("collabAgentToolCall", None, id="classic-without-prompt"),
        pytest.param("collabAgentToolCall", " \n ", id="classic-blank-prompt"),
        pytest.param(
            "collabAgentToolCall",
            "Inspect the VM lifecycle.\nPreserve existing VM configuration.\n" * 30,
            id="classic-full-prompt",
        ),
    ],
)
async def test_child_start_keeps_identity_separate_from_task_instructions(
    harness_workspace, kind: str, prompt: str | None
) -> None:
    """Native frames produce honest child transcripts in the shared UI/MCP timeline."""
    frontend = AsyncMock()
    service = HarnessService(emit=frontend)
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        harness_id="codex",
        model="chatgpt/gpt-6.1-sol",
        reasoning_effort="ultra",
        created_by_id=harness_workspace.created_by_id,
    )
    assistant = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", harness_id="codex"
    )
    root_key = str(session.id)
    service._runs[root_key] = {
        "session_id": root_key,
        "message_id": str(assistant.id),
        "tool_parts": {},
        "step_parts": {},
        "subtask_parts": {},
    }

    async def emit(event: dict[str, Any], parent: str | None) -> None:
        if parent:
            await service._on_external_child_event(root_key, parent, event)
        else:
            await service._on_external_root_event(session, assistant, event)

    projector = CodexEventProjector(
        emit=emit,
        model=lambda: session.model,
        is_managed_tool=lambda _: False,
        image_attachments=AsyncMock(return_value=[]),
    )
    agent_path = "/root/memory_reclaim_research"
    item = {"id": "spawn", "type": kind, "reasoningEffort": "xhigh"}
    if kind == "subAgentActivity":
        item.update(kind="started", agentThreadId="native-child", agentPath=agent_path)
    else:
        item.update(
            tool="spawnAgent", receiverThreadIds=["native-child"], prompt=prompt
        )
    await projector.item(item, completed=False)
    await projector.item(item, completed=True)
    await projector.item(item, completed=True)

    child = HarnessSession.objects.get(parent=session)
    child_user = HarnessMessage.objects.get(session=child, role="user")
    available = prompt is not None and bool(prompt.strip())
    assert child.title == (
        agent_path
        if kind == "subAgentActivity"
        else prompt[:255]
        if available
        else "Subagent"
    )
    assert child_user.content == (
        prompt
        if available
        else "The agent did not provide readable task instructions for this subagent."
    )
    assert child_user.engine_meta == {
        "delegation": {
            "prompt_available": available,
            "agent_path": agent_path if kind == "subAgentActivity" else "",
        }
    }
    assert child.created_by_id == session.created_by_id
    assert child.workspace_id == session.workspace_id
    assert child.harness_id == "codex"
    assert child.reasoning_effort == "xhigh"
    timeline = timeline_to_out(
        ConversationService(),
        child,
        interactions=service.interactions,
        pending_steers=service.list_pending_steers,
    )
    assert timeline["messages"][0]["content"] == child_user.content
    part = HarnessPart.objects.get(message=assistant, type="subtask")
    assert part.meta["child_session_id"] == str(child.id)
    starts = [
        call.args[1]
        for call in frontend.await_args_list
        if call.args[0] == "harness.subtask_started"
    ]
    assert len(starts) == 1
    assert starts[0]["description"] == part.title

    await projector.child_notification(
        {
            "method": "turn/completed",
            "params": {
                "threadId": "native-child",
                "turn": {
                    "id": "child-turn",
                    "status": "completed",
                    "items": [
                        {"id": "answer", "type": "agentMessage", "text": "Found it"}
                    ],
                },
            },
        }
    )
    child.refresh_from_db()
    part.refresh_from_db()
    assert child.status == "idle"
    assert part.state == "completed"
    assert (
        HarnessMessage.objects.get(session=child, role="assistant").content
        == "Found it"
    )
    assert HarnessMessage.objects.get(id=assistant.id).content == ""


@pytest.mark.parametrize("failure", ["read_error", "truncated", "oversized", "mime"])
async def test_projection_omits_failed_image_previews(failure: str) -> None:
    """A failed preview must preserve the successful native tool completion."""
    root = AsyncMock()
    reader = AsyncMock()
    if failure == "read_error":
        reader.side_effect = OSError("workspace unavailable")
    else:
        content = b"x" * 6_000_001 if failure == "oversized" else TINY_PNG
        reader.return_value = FileContent(
            content=content, size=len(content), truncated=failure == "truncated"
        )

    class Accessor:
        read_file_no_follow = reader

    engine = CodexEngine(
        tools=ToolRegistry(), accessor=Accessor(), auth=auth(), emit=root
    )
    engine.agent, engine.mode, engine.opts = get_agent("build"), "build", RunOptions()
    await engine.projector.item(
        {
            "id": "image",
            "type": "imageGeneration",
            "savedPath": (
                "/workspace/image.txt" if failure == "mime" else "/workspace/image.png"
            ),
        },
        completed=True,
    )
    reader.assert_awaited_once()
    completed = root.await_args_list[-1].args[0]
    assert completed["type"] == "tool_completed"
    assert completed["attachments"] == []
