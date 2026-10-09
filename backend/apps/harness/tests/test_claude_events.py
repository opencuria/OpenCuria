"""Synthetic Claude SDK event normalization and lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.engines.claude.events import ClaudeEventNormalizer, StepState
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolRegistry
from claude_agent_sdk._internal.message_parser import parse_message


class FakeAccessor:
    """Minimal accessor for engine event tests."""

    workspace_id = "workspace"


@pytest.fixture
async def engine_events() -> tuple[ClaudeEngine, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    engine = ClaudeEngine(tools=ToolRegistry(), accessor=FakeAccessor(), emit=emit)
    engine._step_states = {"": StepState()}
    return engine, events


@pytest.mark.asyncio
async def test_stream_event_then_final_message_dedupes_prefix(engine_events) -> None:
    engine, events = engine_events
    partial = SimpleNamespace(
        parent_tool_use_id=None,
        session_id="session-1",
        uuid="message-1",
        event={"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": "Hel"}},
    )
    await engine._emit_partial(partial)
    assistant = SimpleNamespace(
        parent_tool_use_id=None, session_id="session-1", uuid="message-1",
        content=[{"type": "text", "text": "Hello"}],
    )
    await engine._emit_assistant(assistant)
    text = [event["delta"].get("text") for event in events if event.get("type") == "part_updated"]
    assert text == ["Hel", "lo"]
    assert [event["type"] for event in events].count("step_start") == 1
    await engine._emit_assistant(SimpleNamespace(
        parent_tool_use_id=None, session_id="session-1", uuid="message-2",
        content=[{"type": "text", "text": "Next message."}],
    ))
    text = [event["delta"].get("text") for event in events if event.get("type") == "part_updated"]
    assert text == ["Hel", "lo", "Next message."]


@pytest.mark.asyncio
async def test_reasoning_delta_maps_to_reasoning_and_tools_are_not_text(engine_events) -> None:
    engine, events = engine_events
    await engine._emit_partial(SimpleNamespace(
        parent_tool_use_id=None, session_id="session-1", uuid="message-1",
        event={"type": "content_block_delta", "index": 1,
               "delta": {"type": "thinking_delta", "thinking": "Consider"}},
    ))
    await engine._emit_partial(SimpleNamespace(
        parent_tool_use_id=None, session_id="session-1", uuid="message-1",
        event={"type": "content_block_delta", "index": 2,
               "delta": {"type": "input_json_delta", "partial_json": "{}"}},
    ))
    part_events = [event for event in events if event.get("type") == "part_updated"]
    assert part_events == [{"type": "part_updated", "step": 1, "delta": {"reasoning": "Consider"}}]


def test_user_tool_result_normalizer_preserves_error_flag_and_call_id() -> None:
    results = ClaudeEventNormalizer.user_tool_results({
        "type": "user",
        "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tool-7",
            "content": [{"type": "text", "text": "denied"}], "is_error": True,
        }]},
    })
    assert results == [("tool-7", [{"type": "text", "text": "denied"}], True)]


@pytest.mark.parametrize(
    ("subtype", "status", "expected"),
    [
        ("task_notification", "completed", "completed"),
        ("task_notification", "failed", "error"),
        ("task_notification", "stopped", "aborted"),
        ("task_updated", "killed", "aborted"),
        ("task_updated", "failed", "error"),
    ],
)
@pytest.mark.asyncio
async def test_subtask_terminal_status_mapping(engine_events, subtype, status, expected) -> None:
    engine, events = engine_events
    engine._subtask_by_tool["parent-call"] = "task-1"
    engine._agent_kind["task-1"] = "general"
    if subtype == "task_notification":
        await engine._handle_task_notification(SimpleNamespace(
            task_id="task-1", tool_use_id="parent-call", status=status, summary="done"
        ))
    else:
        await engine._handle_task_updated(SimpleNamespace(
            subtype="task_updated",
            data={"task_id": "task-1", "tool_use_id": "parent-call",
                  "patch": {"status": status, "result": "done"}},
        ))
    finished = [event for event in events if event.get("type") == "subtask_finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == expected


@pytest.mark.asyncio
async def test_task_notification_and_later_task_update_are_idempotent(engine_events) -> None:
    engine, events = engine_events
    engine._subtask_by_tool["parent-call"] = "task-1"
    await engine._handle_task_notification(SimpleNamespace(
        task_id="task-1", tool_use_id="parent-call", status="stopped", summary="stopped"
    ))
    await engine._handle_task_updated(SimpleNamespace(
        subtype="task_updated",
        data={"task_id": "task-1", "tool_use_id": "parent-call", "patch": {"status": "killed"}},
    ))
    assert [event["status"] for event in events if event.get("type") == "subtask_finished"] == ["aborted"]


def test_tool_output_normalizes_sdk_content() -> None:
    result = SimpleNamespace(content=[SimpleNamespace(text="first"), SimpleNamespace(text="second")])
    assert ClaudeEventNormalizer.tool_output(result) == "first\nsecond"


def _stream_event(event: dict[str, Any], *, parent: str | None = None):
    """Parse a real SDK StreamEvent from its wire-format envelope."""
    return parse_message({
        "type": "stream_event",
        "uuid": "sdk-session-message",
        "session_id": "sdk-session",
        "parent_tool_use_id": parent,
        "event": event,
    })


def _assistant_message(message_id: str, content: list[dict[str, Any]], *, parent: str | None = None):
    """Parse a real SDK AssistantMessage with the API message ID intact."""
    return parse_message({
        "type": "assistant",
        "uuid": f"sdk-envelope-{message_id}",
        "session_id": "sdk-session",
        "parent_tool_use_id": parent,
        "message": {
            "id": message_id,
            "model": "claude-test",
            "content": content,
            "usage": {},
            "stop_reason": "end_turn",
        },
    })


@pytest.mark.asyncio
async def test_api_message_ids_dedupe_final_messages_not_sdk_session_ids(engine_events) -> None:
    engine, events = engine_events
    for message_id in ("api-1", "api-2"):
        await engine._emit_partial(_stream_event({
            "type": "message_start", "message": {"id": message_id, "content": []}
        }))
        await engine._emit_partial(_stream_event({
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }))
        await engine._emit_partial(_stream_event({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "Same output."},
        }))
        await engine._emit_partial(_stream_event({"type": "content_block_stop", "index": 0}))
        final = _assistant_message(message_id, [{"type": "text", "text": "Same output."}])
        await engine._emit_assistant(final)
        await engine._emit_assistant(final)

    text = [event["delta"]["text"] for event in events if event.get("type") == "part_updated" and "text" in event.get("delta", {})]
    assert "".join(text) == "Same output.Same output."
    assert engine._step_states[""].current_message_id == "api-2"


@pytest.mark.asyncio
async def test_stream_block_order_tracks_reasoning_text_and_tool_indices(engine_events) -> None:
    engine, events = engine_events
    engine.tools = ToolRegistry()
    engine.tools.register(default_tool_registry().get("read"))
    await engine._emit_partial(_stream_event({
        "type": "message_start", "message": {"id": "api-blocks", "content": []}
    }))
    await engine._emit_partial(_stream_event({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "thinking", "thinking": ""},
    }))
    await engine._emit_partial(_stream_event({
        "type": "content_block_delta", "index": 0,
        "delta": {"type": "thinking_delta", "thinking": "Reasoning."},
    }))
    await engine._emit_partial(_stream_event({"type": "content_block_stop", "index": 0}))
    await engine._emit_partial(_stream_event({
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "text", "text": ""},
    }))
    await engine._emit_partial(_stream_event({
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "text_delta", "text": "Answer."},
    }))
    await engine._emit_partial(_stream_event({"type": "content_block_stop", "index": 1}))
    await engine._emit_partial(_stream_event({
        "type": "content_block_start", "index": 2,
        "content_block": {"type": "tool_use", "id": "tool-stream", "name": "Read", "input": {}},
    }))
    await engine._emit_partial(_stream_event({"type": "content_block_stop", "index": 2}))

    await engine._emit_assistant(_assistant_message("api-blocks", [
        {"type": "thinking", "thinking": "Reasoning.", "signature": "sig"},
        {"type": "text", "text": "Answer."},
        {"type": "tool_use", "id": "tool-stream", "name": "Read", "input": {"file_path": "/workspace/a"}},
    ]))
    parts = [event for event in events if event.get("type") == "part_updated"]
    assert [event["delta"] for event in parts] == [
        {"reasoning": "Reasoning."}, {"text": "Answer."}
    ]
    queued = [event for event in events if event.get("type") == "tool_queued"]
    assert len(queued) == 1
    assert queued[0]["call_id"] == "tool-stream"
    assert queued[0]["arguments"] == ""
    assert events.index(queued[0]) > events.index(parts[-1])


@pytest.mark.asyncio
async def test_subagent_complete_api_messages_route_once_per_message() -> None:
    child_events: list[tuple[str, dict[str, Any]]] = []

    async def on_child(parent: str, event: dict[str, Any]) -> None:
        child_events.append((parent, event))

    engine = ClaudeEngine(
        tools=ToolRegistry(), accessor=FakeAccessor(), on_child_event=on_child
    )
    for message_id in ("child-api-1", "child-api-2"):
        final = _assistant_message(
            message_id, [{"type": "text", "text": "Child text."}], parent="task-call"
        )
        await engine._emit_assistant(final)
        await engine._emit_assistant(final)

    text = [
        event["delta"]["text"]
        for parent, event in child_events
        if parent == "task-call"
        and event.get("type") == "part_updated"
        and "text" in event.get("delta", {})
    ]
    assert text == ["Child text.", "Child text."]


@pytest.mark.asyncio
async def test_final_message_flushes_cross_delta_redaction_without_duplicate_prefix(engine_events) -> None:
    engine, events = engine_events
    engine.auth_env = {"API_TOKEN": "fixture-secret-token"}
    await engine._emit_partial(_stream_event({
        "type": "message_start", "message": {"id": "api-secret", "content": []}
    }))
    await engine._emit_partial(_stream_event({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "text", "text": ""},
    }))
    for chunk in ("before fixture-secret-", "token after"):
        await engine._emit_partial(_stream_event({
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": chunk},
        }))
    final = _assistant_message("api-secret", [{
        "type": "text", "text": "before fixture-secret-token after"
    }])
    await engine._emit_assistant(final)
    await engine._emit_assistant(final)

    text = [event["delta"]["text"] for event in events if event.get("type") == "part_updated" and "text" in event.get("delta", {})]
    assert "fixture-secret-token" not in "".join(text)
    assert "".join(text) == "before [redacted] after"


@pytest.mark.asyncio
async def test_early_tool_queue_is_updated_by_policy_hook_without_duplicate_queue(fake_accessor) -> None:
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.agents.definitions import get_agent
    from apps.harness.engines.claude.permissionbridge import PermissionBridge
    from apps.harness.runner import RunOptions
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    events: list[dict[str, Any]] = []
    approvals: list[str] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def approve(**kwargs: Any) -> str:
        approvals.append(str(kwargs["call_id"]))
        return "once"

    engine = ClaudeEngine(
        tools=default_tool_registry(), accessor=fake_accessor, emit=emit,
        evaluator=PermissionEvaluator(global_rules={"read": "ask"}),
    )
    options = RunOptions(
        session_id="early-queue", workspace_id=fake_accessor.workspace_id,
        on_permission=approve,
    )
    engine._options = options
    engine._agent = get_agent("build")
    engine._bridge = PermissionBridge(
        tools=engine.tools, evaluator=engine.evaluator, options=options,
        agent=get_agent("build"), mode="build", max_depth=2, engine=engine,
    )
    engine._wire_to_registry = {"Read": "read"}
    await engine._emit_partial(_stream_event({
        "type": "message_start", "message": {"id": "api-tool", "content": []}
    }))
    await engine._emit_partial(_stream_event({
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "id": "early-read", "name": "Read", "input": {}},
    }))
    hook = engine._bridge.hooks(engine)["PreToolUse"][0].hooks[0]
    result = await hook({
        "tool_name": "Read", "tool_input": {"file_path": "/workspace/doc.md"}
    }, "early-read", None)

    queued = [event for event in events if event.get("type") == "tool_queued"]
    assert len(queued) == 1
    assert queued[0]["call_id"] == "early-read"
    assert queued[0]["arguments"] == ""
    assert engine._tool_events["early-read"]["event_arguments"] == {"path": "/workspace/doc.md"}
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"
    approval = await engine._bridge.can_use_tool(
        engine, "Read", {"file_path": "/workspace/doc.md"},
        ToolPermissionContext(tool_use_id="early-read"),
    )
    assert isinstance(approval, PermissionResultAllow)
    assert approvals == ["early-read"]
    started = [event for event in events if event.get("type") == "tool_started"]
    assert len(started) == 1
    assert started[0]["arguments"] == '{"path": "/workspace/doc.md"}'
