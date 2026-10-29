"""Codex context occupancy stays separate from cumulative turn accounting."""

from __future__ import annotations

from typing import Any

import pytest

from apps.harness.engines.codex.engine import CodexEngine
from apps.harness.providers.base import Usage
from apps.harness.tests.test_codex_core_safety import auth
from apps.harness.tools.base import ToolRegistry

EngineEvents = tuple[CodexEngine, list[dict[str, Any]]]


def notification(**params: Any) -> dict[str, Any]:
    """Build the pinned runtime's thread-scoped token notification."""
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread",
            "turnId": "turn",
            "tokenUsage": {
                "total": {
                    "inputTokens": 1200,
                    "outputTokens": 300,
                    "totalTokens": 1500,
                },
                "last": {"inputTokens": 400, "outputTokens": 100, "totalTokens": 500},
                "modelContextWindow": 258400,
            },
            **params,
        },
    }


@pytest.fixture
def engine() -> EngineEvents:
    """Start at a resumed baseline without requiring a workspace process."""
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runtime = CodexEngine(tools=ToolRegistry(), accessor=None, auth=auth(), emit=emit)
    runtime.thread_id, runtime.turn_id = "thread", "turn"
    runtime.usage_baseline = Usage(1000, 200, 1200)
    runtime.usage, runtime.usage_total, runtime.context_usage = Usage(), {}, {}
    return runtime, events


async def test_usage_streams_current_context_without_counting_history_twice(
    engine: EngineEvents,
) -> None:
    runtime, events = engine
    for _ in range(2):
        await runtime._notification(notification())
    assert runtime.usage == Usage(200, 100, 300)
    assert runtime.usage_total["totalTokens"] == 1500
    context = {
        "prompt_tokens": 400,
        "completion_tokens": 100,
        "total_tokens": 500,
        "context_length": 258400,
    }
    assert runtime.context_usage == context
    assert events[-1] == {
        "type": "part_updated",
        "step": 1,
        "delta": {"context": context},
    }


@pytest.mark.parametrize(
    "params,terminal",
    [({"threadId": "unrelated"}, False), ({"turnId": "old"}, False), ({}, True)],
)
async def test_usage_ignores_other_threads_stale_turns_and_terminal_updates(
    engine: EngineEvents, params: dict[str, str], terminal: bool
) -> None:
    runtime, events = engine
    if terminal:
        runtime.terminal.set()
    await runtime._notification(notification(**params))
    assert runtime.usage == Usage()
    assert runtime.context_usage == {}
    assert events == []


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        "invalid",
        {},
        {"inputTokens": True},
        {"inputTokens": -1, "outputTokens": 2, "totalTokens": 1},
    ],
)
async def test_invalid_usage_does_not_clear_previous_snapshots(
    engine: EngineEvents, invalid: Any
) -> None:
    runtime, events = engine
    await runtime._notification(notification())
    previous = runtime.usage, runtime.usage_total, runtime.context_usage
    events.clear()
    await runtime._notification(
        notification(tokenUsage={"total": invalid, "last": invalid})
    )
    assert (runtime.usage, runtime.usage_total, runtime.context_usage) == previous
    assert events == []
