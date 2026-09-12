"""Regression/backcompat tests for the Agent-S computeruse integration.

Replaces the legacy OpenComputer JSON tool-loop tests
(``left_click``/``view_screen`` tool schemas, ledger compaction, last-step
tool gating): Agent-S children never compose the OpenCuria system prompt,
never see tool schemas (``HarnessRunner._filtered_schemas`` offers
``tools=[]`` to the Agent-S wire), and truncate through the shared
``agent_s.harness`` helpers with a sanitized run id.
"""

from __future__ import annotations

from typing import Any

from apps.harness.agent_s.harness import (
    append_video_to_output,
    default_recording_path,
    sanitize_run_id,
    truncate_task_output,
)
from apps.harness.agents.definitions import get_agent
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import (
    agent_s_tool_registry,
    computeruse_tool_registry,
    default_tool_registry,
)
from apps.harness.tools.subagents import TASK_OUTPUT_MAX_CHARS, _child_registry


def test_video_helper_appends_exactly_once() -> None:
    """Recording markdown is appended once even when called repeatedly."""
    run_id = sanitize_run_id("test-run-abc")
    recording_path = default_recording_path(run_id)
    once = append_video_to_output("done", recording_path)
    twice = append_video_to_output(once, recording_path)
    marker = f"![Computer use]({recording_path})"
    assert twice.count(marker) == 1
    assert twice == once


def test_truncate_task_output_preserves_video_marker() -> None:
    """Long outputs still embed the session recording markdown exactly once."""
    run_id = sanitize_run_id("test-run-abc")
    recording_path = default_recording_path(run_id)
    long_body = "x" * (TASK_OUTPUT_MAX_CHARS + 500)
    output, truncated = truncate_task_output(
        long_body, recording_path, TASK_OUTPUT_MAX_CHARS
    )
    marker = f"![Computer use]({recording_path})"
    assert marker in output
    assert output.count(marker) == 1
    assert truncated is True
    assert len(output) <= TASK_OUTPUT_MAX_CHARS + 50


def test_agent_s_registry_is_empty_and_alias_matches() -> None:
    """Agent-S registry offers no tools; the legacy alias stays in sync."""
    assert agent_s_tool_registry().names() == []
    assert computeruse_tool_registry().names() == []


def test_default_registry_has_no_legacy_desktop_tool_names() -> None:
    """Default build registry contains no legacy OpenComputer tool names."""
    names = set(default_tool_registry().names())
    legacy = {
        "view_screen",
        "view_region",
        "move_mouse",
        "left_click",
        "right_click",
        "middle_click",
        "double_click",
        "drag",
        "scroll",
        "type_text",
        "press_key",
        "open_url",
        "wait",
        "ask_user",
    }
    assert not names.intersection(legacy)


def test_computeruse_agent_definition_uses_agent_s_prompt_budget() -> None:
    """Computeruse definition skips system prompts and pins the Agent-S budget."""
    agent = get_agent("computeruse")
    assert agent.mode == "subagent"
    assert agent.system_prompt == ""
    assert agent.steps == 15
    assert "Agent-S" in agent.description


def test_child_registry_computeruse_empty_with_hooks_copied() -> None:
    """Computeruse children get an empty registry that keeps parent hooks."""
    parent = default_tool_registry()
    seen: list[str] = []

    async def _before(tool_name: str, args: Any, ctx: Any) -> None:
        seen.append(tool_name)

    async def _after(tool_name: str, args: Any, ctx: Any, result: Any) -> Any:
        return result

    parent.add_before_hook(_before)
    parent.add_after_hook(_after)
    child = _child_registry(parent, "computeruse")
    assert child.names() == []
    assert list(child.before_hooks) == [_before]
    assert list(child.after_hooks) == [_after]


async def test_harness_runner_computeruse_completion_sees_no_tools() -> None:
    """Computeruse runs complete through the Agent-S wire with tools=[]."""
    from apps.harness.agent_s.adapters import HarnessCompletionAdapter
    from apps.harness.agent_s.harness import run_agent_s_computeruse
    from apps.harness.providers.base import (
        Delta,
        ProviderAdapter,
    )
    from apps.harness.providers.base import (
        Usage as HarnessUsage,
    )
    from apps.harness.providers.resolver import ResolvedModel

    class RoutingProvider(ProviderAdapter):
        """Worker answers DONE at once; grounding unused for this path."""

        name = "fake"

        def __init__(self) -> None:
            self.worker_calls = 0

        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            """Serve a DONE plan and record the offered tool schemas."""
            self.calls.append([tool.name for tool in tools])
            self.worker_calls += 1
            yield Delta(
                text="finished\n```python\nagent.done()\n```",
                usage=HarnessUsage(1, 1, 2),
            )

    provider = RoutingProvider()
    provider.calls = []  # type: ignore[attr-defined]

    def _resolve(model_ref: str) -> ResolvedModel:
        return ResolvedModel(
            adapter=provider,
            model_id="bare",
            provider="fake",
            context_length=0,
            max_output_tokens=0,
        )

    completion = HarnessCompletionAdapter(
        _resolve, main_model="fake/main", grounding_model="fake/ground"
    )
    accessor = FakeAccessor()

    class _Screenshot:
        async def capture_png(self, *, max_dimension=None):  # type: ignore[no-untyped-def]
            from apps.harness.tests.conftest import TINY_PNG

            return TINY_PNG, 1920, 1080

        async def display_geometry(self) -> tuple[int, int]:
            return 1920, 1080

    class _Ocr:
        async def read_words(self, screenshot: bytes):  # type: ignore[no-untyped-def]
            return []

    class _Executor:
        async def execute_action_code(self, exec_code: str):  # type: ignore[no-untyped-def]
            raise AssertionError("DONE must not execute")

    from apps.harness.agent_s.config import AgentSRunConfig

    result = await run_agent_s_computeruse(
        prompt="finish",
        run_id="regression-1",
        accessor=accessor,
        completion=completion,
        code_execution=None,
        ocr=_Ocr(),
        action_executor=_Executor(),
        screenshot=_Screenshot(),
        config=AgentSRunConfig(main_model="fake/main", grounding_model="fake/ground"),
        emit=None,
        sleep=lambda _delay: _noop(),
    )
    assert result.finish_reason == "stop"
    assert provider.calls == [[]]
    # The Agent-S wire carries no function calls: message parts are only
    # text/image (never tool calls) and tools=[] is pinned in adapters.
    assert completion.calls and all(
        "tools" not in call or call["tools"] == [] for call in completion.calls
    )
    for call in completion.calls:
        for message in call["messages"]:
            for part in message.get("content", []) or []:
                assert part.get("type") in ("text", "image_url")


async def _noop() -> None:
    return None


async def test_runner_computeruse_path_delegates_without_tool_schemas(
    fake_accessor,
) -> None:
    """HarnessRunner routes computeruse through Agent-S (no legacy tools)."""
    from apps.harness.providers.base import (
        Delta,
        ProviderAdapter,
        Usage,
    )

    class TextProvider(ProviderAdapter):
        """Text-only provider so Agent-S worker/session paths stay offline."""

        name = "fake-cu"

        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            """Yield one DONE plan with usage."""
            yield Delta(
                text="finished\n```python\nagent.done()\n```",
                usage=Usage(1, 1, 2),
            )

    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=TextProvider(),
        tools=agent_s_tool_registry(),
        accessor=fake_accessor,
        emit=_emit,
    )
    result = await runner.run(
        "finish",
        "computeruse",
        "fake/main",
        "build",
        RunOptions(auto_approve=True, session_id="regression-cu"),
    )
    assert result.finish_reason == "stop"
    assert result.output.count("![Computer use](") == 1
