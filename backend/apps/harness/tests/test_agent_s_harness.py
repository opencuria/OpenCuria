"""Tests for the Agent-S computer-use loop/lifecycle.

Uses small fakes (session/completion/accessor/screenshot/action
executor) to pin: lifecycle + cleanup, exactly one screenshot per turn,
signal priority (done/fail, then next, then wait), delays, default and
configured max steps, exactly-once step_start/step_finish, usage/cost
aggregation, cancel/error cleanup, exactly-once video embedding, and
real desktop vs grounding geometry. A final core-loop test drives a
real ``AgentSession`` with a fake provider: a click plan materializes
twice (CODE_VALID + final) so grounding sees two completion calls, but
only one remote execute runs — and 500/500 in a 1000x1000 grounding
frame on a 1920x1080 display becomes ``pyautogui.click(960, 540)``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from apps.harness.agent_s.config import AgentSRunConfig
from apps.harness.agent_s.harness import (
    append_video_to_output,
    classify_signal,
    default_recording_path,
    resolve_run_config,
    run_agent_s_computeruse,
    sanitize_run_id,
    truncate_task_output,
)
from apps.harness.agent_s.ports import Usage as AgentSUsage
from apps.harness.providers.base import Usage

pytestmark = pytest.mark.asyncio

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


async def _no_sleep(_delay: float) -> None:
    return None


def _config(**kw: Any) -> AgentSRunConfig:
    params: dict[str, Any] = {
        "main_model": "fake/main",
        "grounding_model": "fake/main",
        # Explicit 1000-grid: grounding tests pin the upstream 1000x1000
        # coordinate scaling independent of the 1920x1080 domain default.
        "grounding_width": 1000,
        "grounding_height": 1000,
        "action_pre_delay": 0.0,
        "action_post_delay": 0.0,
        "wait_delay": 0.0,
    }
    params.update(kw)
    return AgentSRunConfig(**params)


class FakeAccessor:
    """hold/record lifecycle fake with ordered desktop calls."""

    def __init__(self, *, record_path: str | None = None) -> None:
        self.calls: list[tuple] = []
        self.record_path = record_path

    async def desktop_action(
        self, action: str, args: dict[str, Any] | None = None, timeout=None
    ) -> dict[str, Any]:
        self.calls.append((action, dict(args or {})))
        if action == "record_start":
            run_id = str((args or {}).get("run_id") or "run")
            return {
                "ok": True,
                "path": self.record_path
                or f"/workspace/.opencuria/computeruse/{run_id}/session.mp4",
            }
        if action == "record_stop":
            return {"ok": True, "path": self.record_path or "rec.mp4"}
        return {"ok": True}

    def actions(self) -> list[str]:
        return [call[0] for call in self.calls]


@dataclass
class FakeScreenshot:
    png: bytes = TINY_PNG
    calls: list[int] = field(default_factory=list)

    async def capture_png(
        self, *, max_dimension: int | None = None
    ) -> tuple[bytes, int, int]:
        self.calls.append(int(max_dimension or 0))
        return self.png, 1920, 1080

    async def display_geometry(self) -> tuple[int, int]:
        return 1600, 900


class FakeSession:
    """Scripted ``predict`` fake returning (info, [exec_code])."""

    def __init__(self, turns: list[tuple[dict[str, Any], str]]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.usages: list[AgentSUsage] = []

    async def predict(self, prompt, obs, *, record=None, sleep=None):
        self.calls += 1
        assert obs["screenshot"] == TINY_PNG
        if record is not None:
            usage = AgentSUsage(input_tokens=1, output_tokens=2, cost=0.5)
            self.usages.append(usage)
            record("worker", usage)
        info, code = self.turns.pop(0) if self.turns else ({}, "DONE")
        return dict(info), [code]


class FakeActionExecutor:
    def __init__(self) -> None:
        self.codes: list[str] = []

    async def execute_action_code(self, exec_code: str) -> dict[str, Any]:
        self.codes.append(exec_code)
        return {"exit_code": 0, "stdout": "", "stderr": ""}


class FakeCompletion:
    async def complete(self, messages, *, purpose, temperature, use_thinking):
        raise AssertionError("unused")


class FakeOcr:
    async def read_words(self, screenshot: bytes):
        return []


def _run_kwargs(**kw: Any) -> dict[str, Any]:
    accessor = kw.pop("accessor", None) or FakeAccessor()
    screenshot = kw.pop("screenshot", None) or FakeScreenshot()
    session = kw.pop("session", None) or FakeSession([({}, "DONE")])
    executor = kw.pop("executor", None) or FakeActionExecutor()
    events: list[dict[str, Any]] = []
    params: dict[str, Any] = {
        "prompt": "do the thing",
        "run_id": "run-1",
        "accessor": accessor,
        "completion": FakeCompletion(),
        "code_execution": None,
        "ocr": FakeOcr(),
        "action_executor": executor,
        "screenshot": screenshot,
        "config": _config(),
        "sleep": _no_sleep,
        "session_factory": lambda completion, materializer: session,
    }
    params.update(kw)

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    params.setdefault("emit", _emit)
    params["_events"] = events
    params["_accessor"] = accessor
    params["_screenshot"] = screenshot
    params["_session"] = session
    params["_executor"] = executor
    return params


def _invoke(kwargs: dict[str, Any]):
    events = kwargs.pop("_events")
    kwargs.pop("_accessor")
    kwargs.pop("_screenshot")
    kwargs.pop("_session")
    kwargs.pop("_executor")
    return events


async def test_lifecycle_hold_record_release_order() -> None:
    """hold first, record_start, then record_stop + release in finally."""
    kwargs = _run_kwargs()
    accessor = kwargs["_accessor"]
    events = _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    assert accessor.actions() == ["hold", "record_start", "record_stop", "release"]
    assert result.steps == 1
    assert result.finish_reason == "stop"
    assert result.metadata["agent_s_finish"] == "DONE"
    assert [e["type"] for e in events] == ["step_start", "step_finish"]


async def test_one_screenshot_per_turn() -> None:
    """Three turns capture exactly three screenshots."""
    session = FakeSession(
        [
            ({"plan": "first"}, "import pyautogui; pyautogui.click(1, 1)"),
            ({"plan": "second"}, "import pyautogui; pyautogui.click(2, 2)"),
            ({}, "DONE"),
        ]
    )
    kwargs = _run_kwargs(session=session)
    screenshot = kwargs["_screenshot"]
    events = _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    assert result.steps == 3
    assert len(screenshot.calls) == 3
    assert session.calls == 3
    assert [e["type"] for e in events].count("step_start") == 3
    assert [e["type"] for e in events].count("step_finish") == 3


async def test_signal_priority_done_fail_then_next_then_wait() -> None:
    """Signal order: done/fail stop, next skips execute, wait sleeps."""
    assert classify_signal("DONE now") == "done"
    assert classify_signal("it FAILed") == "fail"
    assert classify_signal("done and next") == "done"
    assert classify_signal("next please") == "next"
    assert classify_signal("wait a bit") == "wait"
    assert classify_signal("import pyautogui") == "action"

    session = FakeSession(
        [
            ({}, "next action"),
            ({}, "please wait(2)"),
            ({}, "DONE"),
        ]
    )
    kwargs = _run_kwargs(session=session)
    executor = kwargs["_executor"]
    _invoke(kwargs)
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    result = await run_agent_s_computeruse(**{**kwargs, "sleep": _sleep, "emit": _emit})
    assert result.steps == 3
    # next + wait never execute remotely.
    assert executor.codes == []
    assert sleeps == [0.0]


async def test_action_executes_and_delays() -> None:
    """Real actions run remotely with pre/post delays in order."""
    session = FakeSession(
        [
            ({}, "import pyautogui; pyautogui.click(10, 10)"),
            ({}, "DONE"),
        ]
    )
    kwargs = _run_kwargs(
        session=session,
        config=_config(action_pre_delay=0.5, action_post_delay=1.5),
    )
    executor = kwargs["_executor"]
    _invoke(kwargs)
    sleeps: list[float] = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    await run_agent_s_computeruse(**{**kwargs, "sleep": _sleep, "emit": _emit})
    assert executor.codes == ["import pyautogui; pyautogui.click(10, 10)"]
    assert sleeps == [0.5, 1.5]


async def test_max_steps_default_and_config() -> None:
    """resolve_run_config defaults to 15; RunOptions caps explicit configs."""
    assert resolve_run_config(effective_model="m").max_steps == 15
    assert (
        resolve_run_config(
            effective_model="m",
            run_options=type("O", (), {"max_steps": 3})(),
        ).max_steps
        == 3
    )
    explicit = AgentSRunConfig(main_model="m", grounding_model="m", max_steps=10)
    capped = resolve_run_config(
        effective_model="m",
        run_options=type("O", (), {"max_steps": 4, "agent_s_config": explicit})(),
    )
    assert capped.max_steps == 4

    session = FakeSession([({}, "import pyautogui")] * 10)
    kwargs = _run_kwargs(session=session, config=_config(max_steps=2))
    _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    assert result.steps == 2
    assert result.finish_reason == "max_steps"
    assert result.metadata["agent_s_finish"] == "MAX_STEPS"


async def test_fail_finish_reason_and_detail() -> None:
    """FAIL stops with error reason; plan detail prefixes the status."""
    kwargs = _run_kwargs(session=FakeSession([({"plan": "nope"}, "FAIL")]))
    _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    assert result.finish_reason == "error"
    assert result.metadata["agent_s_finish"] == "FAIL"
    assert "nope" in result.output


async def test_usage_and_cost_aggregated_per_step() -> None:
    """Inner usages sum into total Usage/cost and per-step step_finish."""
    session = FakeSession([({}, "DONE")])
    kwargs = _run_kwargs(session=session)
    events = _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    assert isinstance(result.usage, Usage)
    assert (result.usage.prompt_tokens, result.usage.completion_tokens) == (1, 2)
    assert result.usage.total_tokens == 3
    assert result.cost == pytest.approx(0.5)
    finish = [e for e in events if e["type"] == "step_finish"][0]
    assert finish["tokens"] == {
        "prompt_tokens": 1,
        "completion_tokens": 2,
        "total_tokens": 3,
    }
    assert finish["cost"] == pytest.approx(0.5)


async def test_cancel_still_cleans_up_and_propagates() -> None:
    """Cancelled predict still emits step_finish, stops recording, releases."""

    class Cancelling(FakeSession):
        async def predict(self, prompt, obs, *, record=None, sleep=None):
            raise asyncio.CancelledError("stop")

    kwargs = _run_kwargs(session=Cancelling([]))
    accessor = kwargs["_accessor"]
    events = _invoke(kwargs)
    with pytest.raises(asyncio.CancelledError):
        await run_agent_s_computeruse(**kwargs)
    assert accessor.actions() == ["hold", "record_start", "record_stop", "release"]
    assert [e["type"] for e in events] == ["step_start", "step_finish"]


async def test_error_still_cleans_up() -> None:
    """Predict errors fail visibly but still stop recording + release."""

    class Broken(FakeSession):
        async def predict(self, prompt, obs, *, record=None, sleep=None):
            raise RuntimeError("boom")

    kwargs = _run_kwargs(session=Broken([]))
    accessor = kwargs["_accessor"]
    events = _invoke(kwargs)
    with pytest.raises(RuntimeError, match="boom"):
        await run_agent_s_computeruse(**kwargs)
    assert accessor.actions() == ["hold", "record_start", "record_stop", "release"]
    assert [e["type"] for e in events] == ["step_start", "step_finish"]


async def test_record_start_failure_skips_stop_but_releases() -> None:
    """record_start failure raises; record_stop skipped, release kept."""

    class NoRecord(FakeAccessor):
        async def desktop_action(self, action, args=None, timeout=None):
            self.calls.append((action, dict(args or {})))
            if action == "record_start":
                raise RuntimeError("ffmpeg missing")
            return {"ok": True}

    kwargs = _run_kwargs(accessor=NoRecord())
    accessor = kwargs["_accessor"]
    _invoke(kwargs)
    with pytest.raises(RuntimeError, match="record_start failed"):
        await run_agent_s_computeruse(**kwargs)
    assert accessor.actions() == ["hold", "record_start", "release"]


async def test_video_embedded_exactly_once() -> None:
    """Status output carries the final record_stop video markdown once."""
    kwargs = _run_kwargs()
    _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    # FakeAccessor.record_stop returns "rec.mp4" (no path override), so
    # the final record_stop path wins over the record_start default.
    marker = "![Computer use](rec.mp4)"
    assert result.output.count(marker) == 1
    assert result.metadata["recording_path"] == "rec.mp4"
    again = append_video_to_output(result.output, result.metadata["recording_path"])
    assert again == result.output

    long_body = "x" * 9000
    truncated, was = truncate_task_output(long_body, "rec.mp4", 8000)
    assert was is True
    assert truncated.count(marker) == 1


async def test_record_stop_path_wins_for_output_and_metadata() -> None:
    """A transcoded record_stop path replaces the default in all outputs."""

    class TranscodingAccessor(FakeAccessor):
        async def desktop_action(self, action, args=None, timeout=None):
            result = await super().desktop_action(action, args, timeout)
            if action == "record_stop":
                return {"ok": True, "path": "/videos/final.mp4"}
            return result

    kwargs = _run_kwargs(accessor=TranscodingAccessor())
    _invoke(kwargs)
    result = await run_agent_s_computeruse(**kwargs)
    assert result.metadata["recording_path"] == "/videos/final.mp4"
    assert result.output.endswith("\n\n![Computer use](/videos/final.mp4)")
    assert "session.mp4" not in result.output


async def test_cleanup_survives_outer_cancel() -> None:
    """Cancelling during record_stop still stops recording + releases."""

    class SlowStopAccessor(FakeAccessor):
        def __init__(self) -> None:
            super().__init__()
            self.release_seen = False

        async def desktop_action(self, action, args=None, timeout=None):
            if action == "record_stop":
                self.calls.append((action, dict(args or {})))
                # Yield control without real sleeping so the outer task
                # reliably reaches the record_stop await before we
                # cancel it deterministically below.
                gate = asyncio.Event()
                asyncio.get_running_loop().call_later(0.01, gate.set)
                await gate.wait()
                return {"ok": True, "path": "/videos/final.mp4"}
            if action == "release":
                self.release_seen = True
            return await super().desktop_action(action, args, timeout)

    kwargs = _run_kwargs(
        accessor=SlowStopAccessor(), session=FakeSession([({}, "DONE")])
    )
    accessor = kwargs["_accessor"]
    _invoke(kwargs)
    task = asyncio.ensure_future(run_agent_s_computeruse(**kwargs))
    # Wait until cleanup actually started (record_stop entered) instead
    # of racing the cancel against the fast FakeSession body: poll for
    # the record_stop call, then cancel while it is still in flight.
    for _ in range(1000):
        if "record_stop" in accessor.actions():
            break
        await asyncio.sleep(0)
    else:  # pragma: no cover - the run always reaches cleanup quickly
        raise AssertionError("run did not reach record_stop")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert accessor.actions() == ["hold", "record_start", "record_stop", "release"]
    assert accessor.release_seen is True


async def test_real_geometry_wins_after_hold() -> None:
    """Materializer uses display_geometry (1600x900), not the stale config."""
    seen: dict[str, Any] = {}

    def _factory(completion, materializer):
        seen["width"] = materializer.config.width
        seen["height"] = materializer.config.height
        return FakeSession([({}, "DONE")])

    kwargs = _run_kwargs(
        config=_config(desktop_width=1920, desktop_height=1080),
        session_factory=_factory,
    )
    _invoke(kwargs)
    await run_agent_s_computeruse(**kwargs)
    assert (seen["width"], seen["height"]) == (1600, 900)

    calls: list[str] = []

    class OrderAccessor(FakeAccessor):
        async def desktop_action(self, action, args=None, timeout=None):
            calls.append(action)
            return await super().desktop_action(action, args, timeout)

    class OrderScreenshot(FakeScreenshot):
        async def display_geometry(self):
            assert calls[0] == "hold"
            return await super().display_geometry()

    kwargs = _run_kwargs(accessor=OrderAccessor(), screenshot=OrderScreenshot())
    _invoke(kwargs)
    await run_agent_s_computeruse(**kwargs)
    assert calls[0] == "hold"
    assert calls.index("hold") < calls.index("record_start")


async def test_display_geometry_failure_falls_back_to_1920x1080() -> None:
    """display_geometry failure still resolves the materializer to 1920x1080."""
    from apps.harness.agent_s.adapters import DesktopScreenshotAdapter

    seen: dict[str, Any] = {}

    def _factory(completion, materializer):
        seen["width"] = materializer.config.width
        seen["height"] = materializer.config.height
        return FakeSession([({}, "DONE")])

    class FailingAccessor(FakeAccessor):
        async def desktop_action(self, action, args=None, timeout=None):
            if action == "display_info":
                self.calls.append((action, dict(args or {})))
                raise RuntimeError("display down")
            return await super().desktop_action(action, args)

    adapter = DesktopScreenshotAdapter(accessor=FailingAccessor())
    assert await adapter.display_geometry() == (1920, 1080)

    class FailingGeometryScreenshot(FakeScreenshot):
        async def capture_png(
            self, *, max_dimension: int | None = None
        ) -> tuple[bytes, int, int]:
            self.calls.append(int(max_dimension or 0))
            return self.png, 1920, 1080

        async def display_geometry(self) -> tuple[int, int]:
            raise RuntimeError("display down")

    failing = FailingGeometryScreenshot()
    kwargs = _run_kwargs(
        screenshot=failing,
        session_factory=_factory,
    )
    _invoke(kwargs)
    await run_agent_s_computeruse(**kwargs)
    assert (seen["width"], seen["height"]) == (1920, 1080)


async def test_observable_parts_plan_and_reasoning() -> None:
    """Plans become agent events; reflections become reasoning deltas."""
    session = FakeSession(
        [
            (
                {
                    "plan": "click save",
                    "reflection": "looks good",
                    "reflection_thoughts": "hmm",
                },
                "DONE",
            )
        ]
    )
    kwargs = _run_kwargs(session=session)
    events = _invoke(kwargs)
    await run_agent_s_computeruse(**kwargs)
    kinds = [(e["type"], e.get("delta")) for e in events]
    assert ("agent", {"plan": "click save"}) in kinds
    assert ("part_updated", {"reasoning": "looks good"}) in kinds
    assert ("part_updated", {"reasoning": "hmm"}) in kinds


async def test_core_loop_click_materializes_twice_executes_once() -> None:
    """Click plan: two grounding calls, one execute, 500/500 -> 960,540."""
    from apps.harness.agent_s.adapters import (
        HarnessCompletionAdapter,
        WorkspaceOcrAdapter,
    )
    from apps.harness.agent_s.session import AgentSession
    from apps.harness.providers.base import (
        Delta,
        ProviderAdapter,
    )
    from apps.harness.providers.base import (
        Usage as HarnessUsage,
    )
    from apps.harness.providers.resolver import ResolvedModel

    plan = (
        "click the save button\n```python\n"
        'agent.click("The save button in the dialog window")\n'
        "```"
    )
    done_plan = "finished\n```python\nagent.done()\n```"
    grounding_texts = ["[500, 500]", "[500, 500]"]

    class RoutingProvider(ProviderAdapter):
        """Worker plans click then done; grounding answers 500/500 twice."""

        name = "fake"

        def __init__(self) -> None:
            self.worker_calls = 0
            self.grounding_calls = 0

        async def chat_stream(  # type: ignore[no-untyped-def]
            self, model, messages, tools, opts=None
        ):
            """Serve worker plans and grounding coords by message shape."""
            last_text = ""
            for message in reversed(messages):
                content = message.content
                parts = content if isinstance(content, list) else []
                for part in reversed(parts):
                    if part.get("type") == "text" and part.get("text"):
                        last_text = str(part["text"])
                        break
                if last_text:
                    break
            if last_text.startswith("Query:"):
                self.grounding_calls += 1
                yield Delta(
                    text=grounding_texts.pop(0),
                    usage=HarnessUsage(1, 1, 2),
                )
                return
            self.worker_calls += 1
            text = plan if self.worker_calls == 1 else done_plan
            yield Delta(text=text, usage=HarnessUsage(1, 1, 2))

    provider = RoutingProvider()

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

    @dataclass
    class ClickScreenshot(FakeScreenshot):
        async def display_geometry(self) -> tuple[int, int]:
            return 1920, 1080

    screenshot = ClickScreenshot()
    executor_codes: list[str] = []

    class RecordingExecutor:
        async def execute_action_code(self, exec_code: str):
            executor_codes.append(exec_code)
            return {"exit_code": 0, "stdout": "", "stderr": ""}

    config = _config(desktop_width=1920, desktop_height=1080)
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    materializer_holder: dict[str, Any] = {}

    def _factory(comp, materializer):
        materializer_holder["materializer"] = materializer
        return AgentSession(
            completion=comp,
            materializer=materializer,
            platform="linux",
            worker_engine_params={"model": "bare"},
            max_trajectory_length=int(config.max_trajectory_length),
            enable_reflection=False,
            code_execution_available=False,
        )

    result = await run_agent_s_computeruse(
        prompt="click save",
        run_id="core-1",
        accessor=accessor,
        completion=completion,
        code_execution=None,
        ocr=WorkspaceOcrAdapter(accessor=accessor, run_id="core-1"),
        action_executor=RecordingExecutor(),
        screenshot=screenshot,
        config=config,
        emit=_emit,
        sleep=_no_sleep,
        session_factory=_factory,
    )
    assert result.steps == 2
    assert result.finish_reason == "stop"
    # CODE_VALID probe + final materialization = 2 grounding calls.
    assert provider.grounding_calls == 2
    grounding_completions = [c for c in completion.calls if c["purpose"] == "grounding"]
    assert len(grounding_completions) == 2
    # Exactly one remote execute with desktop-scaled coordinates.
    assert len(executor_codes) == 1
    assert "pyautogui.click(960, 540" in executor_codes[0]
    executes = [c for c in accessor.calls if c[0] == "execute"]
    assert executes == []


async def test_derive_worker_engine_type_mapping() -> None:
    """Map providers: OpenRouter→open_router, ChatGPT→openai,
    Bedrock Claude→anthropic, compat→huggingface.
    """
    from apps.harness.agent_s.harness import derive_worker_engine_type

    assert derive_worker_engine_type("openrouter/openai/gpt-4o") == "open_router"
    assert derive_worker_engine_type("chatgpt/gpt-5") == "openai"
    assert (
        derive_worker_engine_type("amazon-bedrock/anthropic.claude-sonnet-4-5")
        == "anthropic"
    )
    assert derive_worker_engine_type("openai-compatible/acme/model") == "huggingface"
    assert derive_worker_engine_type("openai-compatible/claude-x") == "huggingface"
    # Legacy unprefixed refs default to the OpenRouter provider.
    assert derive_worker_engine_type("custom/acme-model") == "open_router"


async def test_run_derives_engine_type_and_explicit_params_win() -> None:
    """run_agent_s_computeruse derives engine_type; explicit params override."""
    from apps.harness.agent_s.harness import run_agent_s_computeruse

    async def _emit(event: dict[str, Any]) -> None:
        return None

    import apps.harness.agent_s.session as session_mod

    orig_session_cls = session_mod.AgentSession
    derived: list[str] = []

    class SpySession(orig_session_cls):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            derived.append(self.state.engine_type)

        async def predict(
            self,
            instruction,
            obs,
            *,
            record=None,
            sleep=None,
            signature_binders=None,
        ):
            return {"plan": "done now"}, ["DONE"]

    session_mod.AgentSession = SpySession  # type: ignore[assignment]
    try:
        config_or = _config(main_model="openrouter/acme/main")
        result = await run_agent_s_computeruse(
            prompt="task",
            run_id="engine-2",
            accessor=FakeAccessor(),
            completion=FakeCompletion(),
            code_execution=None,
            ocr=FakeOcr(),
            action_executor=FakeActionExecutor(),
            screenshot=FakeScreenshot(),
            config=config_or,
            emit=_emit,
            sleep=_no_sleep,
            session_factory=None,
            worker_engine_params=None,
        )
        assert result.finish_reason == "stop"
        config_gpt = _config(main_model="chatgpt/gpt-5")
        await run_agent_s_computeruse(
            prompt="task",
            run_id="engine-3",
            accessor=FakeAccessor(),
            completion=FakeCompletion(),
            code_execution=None,
            ocr=FakeOcr(),
            action_executor=FakeActionExecutor(),
            screenshot=FakeScreenshot(),
            config=config_gpt,
            emit=_emit,
            sleep=_no_sleep,
            session_factory=None,
            worker_engine_params={"engine_type": "custom", "model": "m"},
        )
    finally:
        session_mod.AgentSession = orig_session_cls
    assert derived == ["open_router", "custom"]


async def test_flush_parity_openrouter_drops_turns_chatgpt_trims_images() -> None:
    """OpenRouter (non-long) drops whole turns; ChatGPT (openai) trims old images."""
    from apps.harness.agent_s import worker as worker_mod
    from apps.harness.agent_s.worker import create_state

    long_engine = "openai"  # what ChatGPT maps to
    short_engine = "open_router"  # what OpenRouter maps to

    short = create_state(
        platform="linux",
        worker_engine_params={"engine_type": short_engine},
        max_trajectory_length=1,
    )
    short.generator_messages += [
        {"role": "user", "content": [{"type": "text", "text": "u1"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": [{"type": "text", "text": "u2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
    ]
    worker_mod.flush_messages(short)
    assert [m["content"][0]["text"] for m in short.generator_messages] == [
        short.generator_messages[0]["content"][0]["text"],
        "u2",
        "a2",
    ]

    long_state = create_state(
        platform="linux",
        worker_engine_params={"engine_type": long_engine},
        max_trajectory_length=1,
    )
    long_state.generator_messages = [
        {"role": "system", "content": [{"type": "text", "text": "s"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "t"},
                {"type": "image_url", "image_url": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "t2"},
                {"type": "image_url", "image_url": {}},
            ],
        },
    ]
    worker_mod.flush_messages(long_state)
    assert len(long_state.generator_messages[1]["content"]) == 1
    assert len(long_state.generator_messages[2]["content"]) == 2
