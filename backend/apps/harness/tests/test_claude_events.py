"""Synthetic Claude SDK event normalization and lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.engines.claude.events import (
    ClaudeEventNormalizer,
    SdkMessageTypes,
    StepState,
)
from apps.harness.providers.base import Usage
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import ToolRegistry


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
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hel"},
        },
    )
    await engine._emit_partial(partial)
    assistant = SimpleNamespace(
        parent_tool_use_id=None,
        session_id="session-1",
        uuid="message-1",
        content=[{"type": "text", "text": "Hello"}],
    )
    await engine._emit_assistant(assistant)
    text = [
        event["delta"].get("text")
        for event in events
        if event.get("type") == "part_updated"
    ]
    assert text == ["Hel", "lo"]
    assert [event["type"] for event in events].count("step_start") == 1
    await engine._emit_assistant(
        SimpleNamespace(
            parent_tool_use_id=None,
            session_id="session-1",
            uuid="message-2",
            content=[{"type": "text", "text": "Next message."}],
        )
    )
    text = [
        event["delta"].get("text")
        for event in events
        if event.get("type") == "part_updated"
    ]
    assert text == ["Hel", "lo", "Next message."]


@pytest.mark.asyncio
async def test_reasoning_delta_maps_to_reasoning_and_tools_are_not_text(
    engine_events,
) -> None:
    engine, events = engine_events
    await engine._emit_partial(
        SimpleNamespace(
            parent_tool_use_id=None,
            session_id="session-1",
            uuid="message-1",
            event={
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "thinking_delta", "thinking": "Consider"},
            },
        )
    )
    await engine._emit_partial(
        SimpleNamespace(
            parent_tool_use_id=None,
            session_id="session-1",
            uuid="message-1",
            event={
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": "{}"},
            },
        )
    )
    part_events = [event for event in events if event.get("type") == "part_updated"]
    assert part_events == [
        {"type": "part_updated", "step": 1, "delta": {"reasoning": "Consider"}}
    ]


def test_user_tool_result_normalizer_preserves_error_flag_and_call_id() -> None:
    results = ClaudeEventNormalizer.user_tool_results(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-7",
                        "content": [{"type": "text", "text": "denied"}],
                        "is_error": True,
                    }
                ],
            },
        }
    )
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
async def test_subtask_terminal_status_mapping(
    engine_events, subtype, status, expected
) -> None:
    engine, events = engine_events
    engine._subtask_by_tool["parent-call"] = "task-1"
    engine._agent_kind["task-1"] = "general"
    if subtype == "task_notification":
        await engine._handle_task_notification(
            SimpleNamespace(
                task_id="task-1",
                tool_use_id="parent-call",
                status=status,
                summary="done",
            )
        )
    else:
        await engine._handle_task_updated(
            SimpleNamespace(
                subtype="task_updated",
                data={
                    "task_id": "task-1",
                    "tool_use_id": "parent-call",
                    "patch": {"status": status, "result": "done"},
                },
            )
        )
    finished = [event for event in events if event.get("type") == "subtask_finished"]
    assert len(finished) == 1
    assert finished[0]["status"] == expected


@pytest.mark.asyncio
async def test_task_notification_and_later_task_update_are_idempotent(
    engine_events,
) -> None:
    engine, events = engine_events
    engine._subtask_by_tool["parent-call"] = "task-1"
    await engine._handle_task_notification(
        SimpleNamespace(
            task_id="task-1",
            tool_use_id="parent-call",
            status="stopped",
            summary="stopped",
        )
    )
    await engine._handle_task_updated(
        SimpleNamespace(
            subtype="task_updated",
            data={
                "task_id": "task-1",
                "tool_use_id": "parent-call",
                "patch": {"status": "killed"},
            },
        )
    )
    assert [
        event["status"] for event in events if event.get("type") == "subtask_finished"
    ] == ["aborted"]


def test_tool_output_normalizes_sdk_content() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text="first"), SimpleNamespace(text="second")]
    )
    assert ClaudeEventNormalizer.tool_output(result) == "first\nsecond"


def _stream_event(event: dict[str, Any], *, parent: str | None = None):
    """Parse a real SDK StreamEvent from its wire-format envelope."""
    return parse_message(
        {
            "type": "stream_event",
            "uuid": "sdk-session-message",
            "session_id": "sdk-session",
            "parent_tool_use_id": parent,
            "event": event,
        }
    )


def _assistant_message(
    message_id: str,
    content: list[dict[str, Any]],
    *,
    parent: str | None = None,
    usage: dict[str, Any] | None = None,
):
    """Parse a real SDK AssistantMessage with the API message ID intact."""
    return parse_message(
        {
            "type": "assistant",
            "uuid": f"sdk-envelope-{message_id}",
            "session_id": "sdk-session",
            "parent_tool_use_id": parent,
            "message": {
                "id": message_id,
                "model": "claude-test",
                "content": content,
                "usage": usage or {},
                "stop_reason": "end_turn",
            },
        }
    )


@pytest.mark.asyncio
async def test_api_message_ids_dedupe_final_messages_not_sdk_session_ids(
    engine_events,
) -> None:
    engine, events = engine_events
    for message_id in ("api-1", "api-2"):
        await engine._emit_partial(
            _stream_event(
                {
                    "type": "message_start",
                    "message": {
                        "id": message_id,
                        "content": [],
                        "usage": {"input_tokens": 2, "cache_read_input_tokens": 1},
                    },
                }
            )
        )
        await engine._emit_partial(
            _stream_event(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            )
        )
        await engine._emit_partial(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Same output."},
                }
            )
        )
        await engine._emit_partial(
            _stream_event({"type": "content_block_stop", "index": 0})
        )
        if message_id == "api-2":
            await engine._emit_partial(
                _stream_event({"type": "message_delta", "usage": {"output_tokens": 4}})
            )
        final = _assistant_message(
            message_id,
            [{"type": "text", "text": "Same output."}],
            usage=(
                {"input_tokens": 2, "cache_read_input_tokens": 1, "output_tokens": 4}
                if message_id == "api-1"
                else {}
            ),
        )
        await engine._emit_assistant(final)
        await engine._emit_assistant(final)

    text = [
        event["delta"]["text"]
        for event in events
        if event.get("type") == "part_updated" and "text" in event.get("delta", {})
    ]
    assert "".join(text) == "Same output.Same output."
    assert engine._step_states[""].current_message_id == "api-2"
    finishes = [event for event in events if event.get("type") == "step_finish"]
    assert [event["step"] for event in finishes] == [1]
    assert finishes[0]["tokens"] == {
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "total_tokens": 7,
    }
    await engine._flush_open_steps(
        Usage(prompt_tokens=8, completion_tokens=9, total_tokens=17, cost=0.3)
    )
    finishes = [event for event in events if event.get("type") == "step_finish"]
    assert [event["step"] for event in finishes] == [1, 2]
    assert finishes[-1]["tokens"] == {
        "prompt_tokens": 5,
        "completion_tokens": 5,
        "total_tokens": 10,
    }
    assert sum(event["tokens"]["total_tokens"] for event in finishes) == 17
    assert sum(event["cost"] for event in finishes) == pytest.approx(0.3)
    assert engine._step_states[""].finished_usage == Usage(
        prompt_tokens=8, completion_tokens=9, total_tokens=17, cost=0.3
    )
    before = len(finishes)
    await engine._flush_open_steps(
        Usage(prompt_tokens=8, completion_tokens=9, total_tokens=17, cost=0.3)
    )
    assert (
        len([event for event in events if event.get("type") == "step_finish"]) == before
    )


@pytest.mark.asyncio
async def test_parallel_tools_stay_in_one_api_message_step(engine_events) -> None:
    engine, events = engine_events
    await engine._emit_partial(
        _stream_event(
            {"type": "message_start", "message": {"id": "parallel-turn", "usage": {}}}
        )
    )
    engine._tool_events.update(
        {
            call_id: {
                "started": False,
                "parent_tool_use_id": None,
                "step": 1,
                "tool": "read",
                "title": call_id,
                "event_arguments": {},
            }
            for call_id in ("tool-a", "tool-b")
        }
    )
    await engine._start_tool("tool-a", parent_id=None)
    await engine._start_tool("tool-b", parent_id=None)
    await engine._finish_tool_completed("tool-a", "a", parent_id=None)
    await engine._finish_tool_completed("tool-b", "b", parent_id=None)
    await engine._emit_assistant(
        _assistant_message("parallel-turn", [{"type": "text", "text": "Done."}])
    )

    assert [event["step"] for event in events if event["type"] == "step_start"] == [1]
    assert [event["step"] for event in events if event["type"] == "tool_started"] == [
        1,
        1,
    ]
    assert engine._steps == 1


@pytest.mark.asyncio
async def test_nested_and_root_message_turns_have_independent_steps(
    engine_events,
) -> None:
    engine, events = engine_events
    child_events: list[dict[str, Any]] = []

    async def emit_child(_parent: str, event: dict[str, Any]) -> None:
        child_events.append(event)

    engine.on_child_event = emit_child
    await engine._emit_assistant(
        _assistant_message("root-turn", [{"type": "text", "text": "root"}])
    )
    await engine._emit_assistant(
        _assistant_message(
            "child-turn-1", [{"type": "text", "text": "child 1"}], parent="task-id"
        )
    )
    await engine._emit_assistant(
        _assistant_message(
            "child-turn-2", [{"type": "text", "text": "child 2"}], parent="task-id"
        )
    )
    await engine._emit_assistant(
        _assistant_message("root-turn-2", [{"type": "text", "text": "root 2"}])
    )

    root_steps = [event["step"] for event in events if event["type"] == "step_start"]
    child_steps = [
        event["step"] for event in child_events if event["type"] == "step_start"
    ]
    assert root_steps == [1, 2]
    assert engine._step_states["task-id"].step == 2
    assert engine._steps == 2
    assert child_steps == [1, 2]


@pytest.mark.asyncio
async def test_cancel_cleanup_flushes_redactor_and_finishes_inactive_step(
    engine_events,
) -> None:
    engine, events = engine_events
    await engine._emit_partial(
        _stream_event(
            {
                "type": "message_start",
                "message": {"id": "cancelled-message", "usage": {}},
            }
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "A safe cancellation tail."},
            }
        )
    )

    class Client:
        interrupted = False

        async def interrupt(self) -> None:
            self.interrupted = True

    client = Client()
    await engine._shield_cancelled_cleanup(client)

    assert client.interrupted
    assert [event["type"] for event in events].count("step_finish") == 1
    assert {"type": "aborted", "reason": "cancelled"} in events
    text = [
        event["delta"]["text"]
        for event in events
        if event["type"] == "part_updated" and "text" in event["delta"]
    ]
    assert "".join(text) == "A safe cancellation tail."
    assert not engine._step_states[""].started


@pytest.mark.asyncio
async def test_cancel_cleanup_bounds_stalled_step_finish_sink(
    engine_events, monkeypatch
) -> None:
    import asyncio
    import time

    from apps.harness.engines.claude import engine as claude_engine_module

    engine, events = engine_events
    await engine._emit_assistant(
        _assistant_message("cancel-stalled", [{"type": "text", "text": "partial"}])
    )
    finish_started = asyncio.Event()
    cancelled_emit = asyncio.Event()

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)
        if event.get("type") == "step_finish":
            finish_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled_emit.set()
                raise

    engine._emit = emit
    monkeypatch.setattr(claude_engine_module, "CANCEL_CLEANUP_TIMEOUT_S", 0.05)
    started = time.monotonic()
    await engine._shield_cancelled_cleanup(client=None)
    elapsed = time.monotonic() - started

    assert finish_started.is_set()
    assert cancelled_emit.is_set()
    assert elapsed < 0.5
    assert not engine._step_states[""].started
    assert [event["type"] for event in events].count("step_finish") == 1
    assert {"type": "aborted", "reason": "cancelled"} in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hook_type", "task_type", "requested_type", "expected"),
    [
        ("general-purpose", "local_agent", "explore", "general"),
        ("", "local_agent", "explore", "explore"),
        ("", "local", "", ""),
    ],
)
async def test_task_started_keeps_only_strictly_canonical_child_identity(
    engine_events, hook_type, task_type, requested_type, expected
) -> None:
    from apps.harness.agents.definitions import canonical_subagent_type, get_agent
    from apps.harness.engines.claude.message_projector import ClaudeMessageProjector
    from apps.harness.engines.claude.permissionbridge import PermissionBridge
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.runner import RunOptions

    engine, _events = engine_events
    call_id = "task-call"
    task_id = "sdk-child-id"
    engine._tool_events[call_id] = {
        "depth": 2,
        "step": 1,
        "event_arguments": (
            {"subagent_type": requested_type} if requested_type else {}
        ),
    }
    engine._pending_task_tool_ids.add(call_id)
    if hook_type:
        canonical_hook_type = canonical_subagent_type(hook_type)
        engine._agent_type_raw[task_id] = canonical_hook_type
        engine._agent_kind[task_id] = canonical_hook_type
    options = RunOptions(session_id="task-started", depth=0, max_depth=5)
    engine._options = options
    engine._agent = get_agent("build")
    engine._bridge = PermissionBridge(
        tools=engine.tools,
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        options=options,
        agent=engine._agent,
        mode="build",
        max_depth=5,
        engine=engine,
    )
    projector = ClaudeMessageProjector(engine, cli_version="", sdk_version="")
    message = parse_message(
        {
            "type": "system",
            "subtype": "task_started",
            "task_id": task_id,
            "description": "fixture child",
            "task_type": task_type,
            "tool_use_id": call_id,
            "uuid": "task-started-message",
            "session_id": "sdk-session",
        }
    )

    await projector.handle_message(message)

    if expected:
        assert engine._agent_type_raw[task_id] == expected
        assert engine._agent_kind[task_id] == expected
        parent, child, depth = engine._bridge.scope(
            engine, {"agent_id": task_id}
        )
        assert parent == call_id
        assert child.name == expected
        assert depth == 3
    else:
        assert task_id not in engine._agent_type_raw
        with pytest.raises(ValueError, match="Unknown Claude subagent type"):
            engine._bridge.scope(engine, {"agent_id": task_id})


@pytest.mark.asyncio
async def test_stream_usage_and_max_turns_result_are_attributed_once() -> (
    None
):
    from apps.harness.engines.claude.message_projector import ClaudeMessageProjector

    class ResultMessage:
        pass

    types = SdkMessageTypes(
        assistant=type("Assistant", (), {}),
        result=ResultMessage,
        stream=type("Stream", (), {}),
        user=type("User", (), {}),
        system=type("System", (), {}),
        task_notification=type("TaskNotification", (), {}),
        task_progress=type("TaskProgress", (), {}),
        task_started=type("TaskStarted", (), {}),
    )
    original = ClaudeEventNormalizer.message_types
    ClaudeEventNormalizer.message_types = staticmethod(lambda: types)
    try:

        class EngineStub:
            async def _bind_external_session(self, _sid: str) -> None:
                pass

            @staticmethod
            def _safe_error_text(text: str) -> str:
                return text

        projector = ClaudeMessageProjector(EngineStub(), cli_version="", sdk_version="")
        message = ResultMessage()
        message.subtype = "error_max_turns"
        message.is_error = True
        message.errors = ["turn limit reached"]
        message.result = "partial result"
        message.session_id = "session"
        message.usage = {
            "input_tokens": 4,
            "output_tokens": 2,
        }
        message.model_usage = None
        outcome = await projector.handle_message(message)
        assert outcome is not None
        assert outcome[1] == "partial result"
        assert outcome[2] == Usage(prompt_tokens=4, completion_tokens=2, total_tokens=6)
    finally:
        ClaudeEventNormalizer.message_types = original


@pytest.mark.asyncio
async def test_stream_block_order_tracks_reasoning_text_and_tool_indices(
    engine_events,
) -> None:
    engine, events = engine_events
    engine.tools = ToolRegistry()
    engine.tools.register(default_tool_registry().get("read"))
    await engine._emit_partial(
        _stream_event(
            {"type": "message_start", "message": {"id": "api-blocks", "content": []}}
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            }
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "Reasoning."},
            }
        )
    )
    await engine._emit_partial(
        _stream_event({"type": "content_block_stop", "index": 0})
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": "text", "text": ""},
            }
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "Answer."},
            }
        )
    )
    await engine._emit_partial(
        _stream_event({"type": "content_block_stop", "index": 1})
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {
                    "type": "tool_use",
                    "id": "tool-stream",
                    "name": "Read",
                    "input": {},
                },
            }
        )
    )
    await engine._emit_partial(
        _stream_event({"type": "content_block_stop", "index": 2})
    )

    await engine._emit_assistant(
        _assistant_message(
            "api-blocks",
            [
                {"type": "thinking", "thinking": "Reasoning.", "signature": "sig"},
                {"type": "text", "text": "Answer."},
                {
                    "type": "tool_use",
                    "id": "tool-stream",
                    "name": "Read",
                    "input": {"file_path": "/workspace/a"},
                },
            ],
        )
    )
    parts = [event for event in events if event.get("type") == "part_updated"]
    assert [event["delta"] for event in parts] == [
        {"reasoning": "Reasoning."},
        {"text": "Answer."},
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
async def test_final_message_flushes_cross_delta_redaction_without_duplicate_prefix(
    engine_events,
) -> None:
    engine, events = engine_events
    engine.auth_env = {"API_TOKEN": "fixture-secret-token"}
    await engine._emit_partial(
        _stream_event(
            {"type": "message_start", "message": {"id": "api-secret", "content": []}}
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }
        )
    )
    for chunk in ("before fixture-secret-", "token after"):
        await engine._emit_partial(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": chunk},
                }
            )
        )
    final = _assistant_message(
        "api-secret", [{"type": "text", "text": "before fixture-secret-token after"}]
    )
    await engine._emit_assistant(final)
    await engine._emit_assistant(final)

    text = [
        event["delta"]["text"]
        for event in events
        if event.get("type") == "part_updated" and "text" in event.get("delta", {})
    ]
    assert "fixture-secret-token" not in "".join(text)
    assert "".join(text) == "before [redacted] after"


@pytest.mark.asyncio
async def test_early_tool_queue_is_updated_by_policy_hook_without_duplicate_queue(
    fake_accessor,
) -> None:
    from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

    from apps.harness.agents.definitions import get_agent
    from apps.harness.engines.claude.permissionbridge import PermissionBridge
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.runner import RunOptions

    events: list[dict[str, Any]] = []
    approvals: list[str] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    async def approve(**kwargs: Any) -> str:
        approvals.append(str(kwargs["call_id"]))
        return "once"

    engine = ClaudeEngine(
        tools=default_tool_registry(),
        accessor=fake_accessor,
        emit=emit,
        evaluator=PermissionEvaluator(global_rules={"read": "ask"}),
    )
    options = RunOptions(
        session_id="early-queue",
        workspace_id=fake_accessor.workspace_id,
        on_permission=approve,
    )
    engine._options = options
    engine._agent = get_agent("build")
    engine._bridge = PermissionBridge(
        tools=engine.tools,
        evaluator=engine.evaluator,
        options=options,
        agent=get_agent("build"),
        mode="build",
        max_depth=2,
        engine=engine,
    )
    engine._wire_to_registry = {"Read": "read"}
    await engine._emit_partial(
        _stream_event(
            {"type": "message_start", "message": {"id": "api-tool", "content": []}}
        )
    )
    await engine._emit_partial(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "early-read",
                    "name": "Read",
                    "input": {},
                },
            }
        )
    )
    hook = engine._bridge.hooks(engine)["PreToolUse"][0].hooks[0]
    result = await hook(
        {"tool_name": "Read", "tool_input": {"file_path": "/workspace/doc.md"}},
        "early-read",
        None,
    )

    queued = [event for event in events if event.get("type") == "tool_queued"]
    assert len(queued) == 1
    assert queued[0]["call_id"] == "early-read"
    assert queued[0]["arguments"] == ""
    assert engine._tool_events["early-read"]["event_arguments"] == {
        "path": "/workspace/doc.md"
    }
    assert result["hookSpecificOutput"]["permissionDecision"] == "ask"
    approval = await engine._bridge.can_use_tool(
        engine,
        "Read",
        {"file_path": "/workspace/doc.md"},
        ToolPermissionContext(tool_use_id="early-read"),
    )
    assert isinstance(approval, PermissionResultAllow)
    assert approvals == ["early-read"]
    started = [event for event in events if event.get("type") == "tool_started"]
    assert len(started) == 1
    assert started[0]["arguments"] == '{"path": "/workspace/doc.md"}'
