"""Tests for the computer-use agent loop (recording + image compaction)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import computeruse_tool_registry, default_tool_registry
from apps.harness.computeruse_loop import (
    append_video_to_output,
    count_image_url_parts,
    truncate_task_output,
)
from apps.harness.tools.subagents import TASK_OUTPUT_MAX_CHARS


class FakeProvider(ProviderAdapter):
    """Scripted provider for computer-use loop tests."""

    name = "fake"

    def __init__(self, steps: list[list[Delta]]) -> None:
        self._steps = [list(step) for step in steps]
        self.calls: list[dict[str, Any]] = []
        self.messages: list[list[LLMMessage]] = []

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Yield the next canned step."""
        self.calls.append({"model": model, "tools": [tool.name for tool in tools]})
        self.messages.append(list(messages))
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        for delta in step:
            yield delta


class WaitForeverProvider(ProviderAdapter):
    """Block until cancelled so abort tests can stop the loop."""

    name = "wait"

    def __init__(self) -> None:
        self.messages: list[list[LLMMessage]] = []

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Sleep until cancelled."""
        self.messages.append(list(messages))
        await asyncio.sleep(3600)
        yield Delta(text="never", usage=Usage(0, 0, 0))


def _text_step(text: str) -> list[Delta]:
    return [Delta(text=text, usage=Usage(1, 1, 2, 0.001))]


def _tool_step(
    tool: str,
    args: dict[str, Any],
    *,
    call_id: str = "call-1",
) -> list[Delta]:
    return [
        Delta(
            tool_calls=(
                {
                    "index": 0,
                    "id": call_id,
                    "name": tool,
                    "arguments": json.dumps(args),
                },
            ),
            usage=Usage(2, 2, 4, 0.002),
        )
    ]


def _computeruse_runner(
    provider: ProviderAdapter,
    accessor: FakeAccessor,
    *,
    session_id: str = "cu-session-1",
) -> tuple[HarnessRunner, RunOptions]:
    return HarnessRunner(
        provider=provider,
        tools=computeruse_tool_registry(),
        accessor=accessor,
        emit=lambda event: asyncio.sleep(0),
    ), RunOptions(
        auto_approve=True,
        session_id=session_id,
        workspace_id="ws-1",
    )


async def test_computeruse_happy_path_records_and_compacts_images() -> None:
    """Setup, click, compaction, and recording markdown on success."""
    provider = FakeProvider(
        [
            _tool_step("left_click", {"x": 100, "y": 200}, call_id="c1"),
            _text_step("clicked"),
        ]
    )
    accessor = FakeAccessor()
    runner, opts = _computeruse_runner(provider, accessor)
    result = await runner.run("click button", "computeruse", "m", "build", opts)

    actions = [call[0] for call in accessor.desktop_calls]
    assert actions[0] == "hold"
    assert actions[1] == "record_start"
    assert "screenshot" in actions
    assert "click" in actions
    assert "record_stop" in actions
    assert actions[-1] == "release"
    assert "![Computer use](/workspace/.opencuria/computeruse/" in result.output
    assert result.metadata.get("recording_path", "").endswith("session.mp4")

    first_images = count_image_url_parts(provider.messages[0])
    assert first_images == 1
    second_images = count_image_url_parts(provider.messages[1])
    assert second_images == 1
    assert result.output.endswith("session.mp4)")


async def test_build_agent_does_not_offer_view_screen() -> None:
    """Regression: default build registry excludes computer-use tools."""
    provider = FakeProvider([_text_step("ok")])
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(),
        emit=lambda event: asyncio.sleep(0),
    )
    await runner.run("hi", "build", "m", "build", RunOptions(auto_approve=True))
    assert "view_screen" not in provider.calls[0]["tools"]


async def test_computeruse_abort_still_stops_recording() -> None:
    """Cancellation triggers record_stop in the finally block."""
    provider = WaitForeverProvider()
    accessor = FakeAccessor()
    runner = HarnessRunner(
        provider=provider,
        tools=computeruse_tool_registry(),
        accessor=accessor,
        emit=lambda event: asyncio.sleep(0),
    )
    task = asyncio.create_task(
        runner.run(
            "wait",
            "computeruse",
            "m",
            "build",
            RunOptions(auto_approve=True, session_id="abort-session"),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any(call[0] == "record_stop" for call in accessor.desktop_calls)
    assert any(call[0] == "release" for call in accessor.desktop_calls)


def test_truncate_task_output_preserves_video_marker() -> None:
    """Long outputs still embed the session recording markdown."""
    run_id = "test-run-abc"
    long_body = "x" * (TASK_OUTPUT_MAX_CHARS + 500)
    output, truncated = truncate_task_output(long_body, run_id, TASK_OUTPUT_MAX_CHARS)
    video_path = f"/workspace/.opencuria/computeruse/{run_id}/session.mp4"
    assert f"![Computer use]({video_path})" in output
    assert truncated is True
    assert len(output) <= TASK_OUTPUT_MAX_CHARS + 50


def test_append_video_to_output_does_not_duplicate_marker() -> None:
    """Recording markdown is appended once even when called repeatedly."""
    run_id = "dup-run"
    once = append_video_to_output("done", run_id)
    twice = append_video_to_output(once, run_id)
    marker = f"![Computer use](/workspace/.opencuria/computeruse/{run_id}/session.mp4)"
    assert twice.count(marker) == 1
    assert twice == once


async def test_computeruse_ledger_discards_old_screenshots() -> None:
    """Two tool rounds keep one JPEG and move prior actions into the ledger."""
    provider = FakeProvider(
        [
            _tool_step("left_click", {"x": 100, "y": 200}, call_id="c1"),
            _tool_step("left_click", {"x": 300, "y": 400}, call_id="c2"),
            _text_step("clicked twice"),
        ]
    )
    accessor = FakeAccessor()
    runner, opts = _computeruse_runner(provider, accessor, session_id="ledger-session")
    await runner.run("double click", "computeruse", "m", "build", opts)

    last_messages = provider.messages[-1]
    assert count_image_url_parts(last_messages) == 1
    ledger_found = False
    for message in last_messages:
        content = message.content
        if isinstance(content, str) and "Completed action ledger" in content:
            ledger_found = True
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text" and "Completed action ledger" in str(
                    part.get("text", "")
                ):
                    ledger_found = True
    assert ledger_found


async def test_record_start_failure_still_releases_desktop_hold() -> None:
    """record_start errors fail fast and still release the computer-use lease."""

    class RecordStartFails(FakeAccessor):
        async def desktop_action(
            self,
            action: str,
            args: dict[str, Any] | None = None,
            timeout: float | None = None,
        ) -> dict[str, Any]:
            self.desktop_calls.append((action, args, timeout))
            if action == "record_start":
                raise RuntimeError("ffmpeg missing")
            return await super().desktop_action(action, args, timeout)

    provider = FakeProvider([_text_step("never reached")])
    accessor = RecordStartFails()
    runner, opts = _computeruse_runner(provider, accessor)
    with pytest.raises(RuntimeError, match="record_start failed|ffmpeg missing"):
        await runner.run("go", "computeruse", "m", "build", opts)
    actions = [call[0] for call in accessor.desktop_calls]
    assert actions[0] == "hold"
    assert "record_start" in actions
    assert "record_stop" not in actions
    assert actions[-1] == "release"
