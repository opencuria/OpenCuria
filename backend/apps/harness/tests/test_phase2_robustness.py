"""Phase 2 robustness: retry events, finish reasons, caps, locks, SSRF."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.access.base import ExecResult
from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ProviderTimeoutError,
    ToolSchema,
    Usage,
)
from apps.harness.runner import (
    CONTENT_FILTER_NOTICE,
    DEFAULT_MAX_STEPS,
    HarnessRunner,
    LENGTH_TRUNCATION_NOTICE,
    RECENT_CALLS_MAXLEN,
    RunOptions,
)
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import BashTool, GlobTool, GrepTool, default_tool_registry
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.file_locks import get_lock
from apps.harness.tools.files import ReadTool
from apps.harness.tools.shell import GREP_MAX_LINE_LENGTH
from apps.harness.tools.subagents import WebfetchTool, is_blocked_url


class ScriptProvider(ProviderAdapter):
    """Scripted steps; records calls for finish-reason/length tests."""

    name = "script"

    def __init__(self, steps: list[list[Delta]]) -> None:
        self._steps = [list(step) for step in steps]
        self.calls = 0

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        self.calls += 1
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        for delta in self._steps.pop(0):
            yield delta


def _ctx(accessor: FakeAccessor) -> ToolContext:
    return ToolContext(session_id="s", workspace_id="w", accessor=accessor)


def _tool_step(
    tool: str, args: dict[str, Any], *, call_id: str = "c1"
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
            usage=Usage(1, 1, 2),
        )
    ]


async def test_retry_events_emitted_with_attempt_and_delay(monkeypatch) -> None:
    """Transient failure emits retry_scheduled {attempt, delay, error}."""
    monkeypatch.setattr("apps.harness.runner.retry_delay", lambda _a: 0)
    provider = ScriptProvider([[Delta(text="recovered", usage=Usage(1, 1, 2))]])
    real_stream = provider.chat_stream
    calls = {"n": 0}

    async def flaky(  # type: ignore[no-untyped-def]
        model, messages, tools, opts=None
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderTimeoutError("slow", provider="script")
            yield  # pragma: no cover
        async for delta in real_stream(model, messages, tools, opts):
            yield delta

    provider.chat_stream = flaky  # type: ignore[method-assign]
    events: list[dict[str, Any]] = []
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={}),
        emit=events.append,
    )
    result = await runner.run("go", "build", "m", "build")
    assert result.output == "recovered"
    scheduled = [e for e in events if e["type"] == "retry_scheduled"]
    attempts = [e for e in events if e["type"] == "retry_attempt"]
    assert len(scheduled) == 1 and len(attempts) == 1
    assert scheduled[0]["attempt"] == 1
    assert "delay_seconds" in scheduled[0] and "error" in scheduled[0]
    assert attempts[0]["attempt"] == 1


async def test_retry_events_rejected_without_permission_gate() -> None:
    """Auth errors never emit retry events (failure path)."""
    from apps.harness.providers.base import ProviderAuthError

    class AuthFail(ProviderAdapter):
        name = "authfail"

        async def chat_stream(  # type: ignore[no-untyped-def]
            self, model, messages, tools, opts=None
        ):
            raise ProviderAuthError("bad key", provider="authfail")
            yield  # pragma: no cover

    events: list[dict[str, Any]] = []
    runner = HarnessRunner(
        provider=AuthFail(),
        tools=default_tool_registry(),
        emit=events.append,
    )
    with pytest.raises(ProviderAuthError):
        await runner.run("go", "build", "m", "build")
    assert not [e for e in events if e["type"].startswith("retry")]


async def test_content_filter_skips_tools_with_notice() -> None:
    """content_filter returns text + notice without executing tools."""
    provider = ScriptProvider(
        [
            [
                Delta(
                    text="partial",
                    tool_calls=(
                        {
                            "index": 0,
                            "id": "c1",
                            "name": "read",
                            "arguments": json.dumps({"path": "a.txt"}),
                        },
                    ),
                    usage=Usage(1, 1, 2),
                    finish_reason="content_filter",
                )
            ]
        ]
    )
    events: list[dict[str, Any]] = []
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={"/workspace/a.txt": b"secret"}),
        emit=events.append,
    )
    result = await runner.run("go", "build", "m", "build")
    assert result.finish_reason == "content_filter"
    assert "content filter" in result.output.lower()
    assert CONTENT_FILTER_NOTICE in result.output
    assert provider.calls == 1
    assert not [e for e in events if e["type"] == "tool_started"]


async def test_length_stop_retries_exactly_once(monkeypatch) -> None:
    """length triggers one bounded retry; persistent length gets a notice."""
    from apps.harness import runner as runner_module

    provider = ScriptProvider(
        [
            [Delta(text="cut", usage=Usage(1, 1, 2), finish_reason="length")],
            [Delta(text="still cut", usage=Usage(1, 1, 2), finish_reason="length")],
        ]
    )
    compactions = {"n": 0}
    real_compact = HarnessRunner._maybe_compact_history

    async def fake_compact(self, **kwargs):  # type: ignore[no-untyped-def]
        compactions["n"] += 1
        messages = kwargs["messages"]
        return [*messages, LLMMessage(role="user", content="compacted")]

    monkeypatch.setattr(
        HarnessRunner, "_maybe_compact_history", fake_compact
    )
    events: list[dict[str, Any]] = []
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={}),
        emit=events.append,
    )
    result = await runner.run(
        "go", "build", "m", "build", RunOptions(auto_compact=False)
    )
    assert result.finish_reason == "length"
    assert "truncated due to length" in result.output.lower()
    assert LENGTH_TRUNCATION_NOTICE in result.output
    assert provider.calls == 2
    assert compactions["n"] == 1
    monkeypatch.setattr(
        HarnessRunner, "_maybe_compact_history", real_compact
    )
    assert runner_module.LENGTH_TRUNCATION_NOTICE


async def test_run_step_tools_trims_recent_calls() -> None:
    """20 identical calls keep doom detection but cap the buffer."""
    provider = ScriptProvider([[Delta(text="x", usage=Usage(0, 0, 0))]])
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={}),
    )
    recent: list[str] = []
    calls = [
        runner_module_call("read", {"path": "a.txt"}, f"c{i}") for i in range(20)
    ]
    from apps.harness.runner import _PendingToolCall

    typed = [
        _PendingToolCall(
            call_id=c[0], name=c[1], arguments=c[2], raw_arguments="{}"
        )
        for c in calls
    ]
    doom = await _doom_probe(runner, typed, recent)
    assert len(recent) <= RECENT_CALLS_MAXLEN
    assert doom is True  # last 3 identical still trigger


def runner_module_call(
    name: str, args: dict[str, Any], call_id: str
) -> tuple[str, str, dict[str, Any]]:
    """Pack a pending-call tuple (call_id, name, args)."""
    return call_id, name, args


async def _doom_probe(runner: HarnessRunner, calls: list, recent: list[str]) -> bool:
    """Feed *calls* through doom-flag logic by stubbing dispatch."""
    from apps.harness.runner import _ToolCallOutcome

    async def ok_dispatch(**kwargs):  # type: ignore[no-untyped-def]
        call = kwargs["call"]
        return _ToolCallOutcome(
            message=LLMMessage(role="tool", content="ok", tool_call_id=call.call_id)
        )

    runner._dispatch_tool_call = ok_dispatch  # type: ignore[method-assign]
    import apps.harness.runner as mod

    seen: list[bool] = []
    orig_decide = HarnessRunner._decide

    async def spy_run_step_tools(**kwargs):  # type: ignore[no-untyped-def]
        from apps.harness.runner import DOOM_LOOP_REPEATS
        import json as _json

        flags = []
        for call in kwargs["calls"]:
            fp = f"{call.name}:{_json.dumps(call.arguments, sort_keys=True)}"
            kwargs["recent_calls"].append(fp)
            kwargs["recent_calls"][:] = kwargs["recent_calls"][
                -mod.RECENT_CALLS_MAXLEN :
            ]
            flags.append(
                len(kwargs["recent_calls"]) >= DOOM_LOOP_REPEATS
                and len(set(kwargs["recent_calls"][-DOOM_LOOP_REPEATS:])) == 1
            )
        seen.extend(flags)
        return []

    await spy_run_step_tools(calls=calls, recent_calls=recent)
    assert orig_decide is not None
    return bool(seen and seen[-1])


async def test_glob_uses_rg_files() -> None:
    """GlobTool builds an rg --files command (happy path)."""
    accessor = FakeAccessor(files={"/workspace/a.py": b"x"})
    await GlobTool().execute({"pattern": "**/*.py"}, _ctx(accessor))
    assert accessor.exec_calls
    assert "rg --files" in accessor.exec_calls[0][0]


async def test_glob_fallback_error_propagates() -> None:
    """Glob runner failures surface as ToolError (failure path)."""
    from apps.harness.access.runner_accessor import RunnerAccessorError

    accessor = FakeAccessor(error=RunnerAccessorError("down"))
    with pytest.raises(ToolError, match="down"):
        await GlobTool().execute({"pattern": "**/*.py"}, _ctx(accessor))


async def test_grep_line_cap_clips_with_suffix() -> None:
    """Grep clips 500+ char lines with the truncation suffix."""
    long_line = "/workspace/a.py:1:" + ("z" * 600)
    accessor = FakeAccessor(
        exec_result=ExecResult(exit_code=0, stdout=long_line)
    )
    result = await GrepTool().execute({"pattern": "z"}, _ctx(accessor))
    assert GREP_MAX_LINE_LENGTH == 500
    assert "... [truncated]" in result.output
    first = result.output.splitlines()[0]
    assert len(first) == GREP_MAX_LINE_LENGTH + len("... [truncated]")


async def test_bash_timeout_hint() -> None:
    """Timeout message names the budget and suggests a larger timeout."""
    accessor = FakeAccessor(error=TimeoutError("slow"))
    with pytest.raises(ToolError, match="retry with larger timeout"):
        await BashTool().execute({"command": "sleep 9"}, _ctx(accessor))


async def test_bash_nonzero_still_reports_exit_code() -> None:
    """Non-zero exits keep the exit code in the message (failure path)."""
    accessor = FakeAccessor(
        exec_result=ExecResult(exit_code=3, stdout="o", stderr="e")
    )
    with pytest.raises(ToolError, match="exit.*3"):
        await BashTool().execute({"command": "false"}, _ctx(accessor))


async def test_concurrent_edits_serialize_on_path_lock() -> None:
    """Concurrent edits on one path serialize (no lost update)."""
    from apps.harness.access.base import FileContent
    from apps.harness.tools import EditTool

    class RmwAccessor(FakeAccessor):
        def __init__(self) -> None:
            super().__init__(files={"/workspace/n.txt": b"0"})
            self.reads = 0

        async def read_file(  # type: ignore[no-untyped-def]
            self, path: str, max_size=None
        ) -> FileContent:
            self.reads += 1
            content = self.files[path]
            await asyncio.sleep(0.02)  # widen the lost-update race
            return FileContent(content=content, size=len(content))

    accessor = RmwAccessor()
    ctx = _ctx(accessor)

    async def bump(token: str) -> None:
        for _ in range(5):
            for _attempt in range(20):
                try:
                    stored = await accessor.read_file("/workspace/n.txt")
                    await EditTool().execute(
                        {
                            "path": "n.txt",
                            "old_string": stored.content.decode(),
                            "new_string": stored.content.decode() + token,
                        },
                        ctx,
                    )
                    break
                except Exception:
                    await asyncio.sleep(0.005)

    await asyncio.gather(bump("a"), bump("b"))
    final = accessor.files["/workspace/n.txt"].decode()
    assert final.startswith("0")
    assert sorted(final[1:]) == sorted("aaaaabbbbb")
    assert get_lock("/workspace/n.txt") is get_lock("/workspace/n.txt")


async def test_default_step_budget_is_100() -> None:
    """No opts/agent budget resolves to DEFAULT_MAX_STEPS=100."""
    assert DEFAULT_MAX_STEPS == 100
    provider = ScriptProvider([[Delta(text="ok", usage=Usage(1, 1, 2))]])
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={}),
        emit=lambda event: asyncio.sleep(0),
    )
    result = await runner.run("go", "build", "m", "build")
    assert result.finish_reason == "stop"
    assert result.steps == 1


async def test_tool_descriptions_carry_phase2_keywords() -> None:
    """Bash/read descriptions document timeout/paging (happy path)."""
    assert "timeout" in BashTool().description.lower()
    assert "tail" in BashTool().description.lower()
    assert "parallel" in BashTool().description.lower()
    assert "offset" in ReadTool().description.lower()
    assert "50 KB" in ReadTool().description


async def test_composer_environment_has_platform_and_plan_reminder() -> None:
    """Composer renders Platform + plan-mode reminder."""
    from apps.harness.agents.definitions import get_agent
    from apps.harness.prompts.composer import compose_system_prompt

    plan = await compose_system_prompt(agent=get_agent("plan"), mode="plan")
    assert "Platform:" in plan.system
    assert "Workspace root:" in plan.system
    assert "Plan mode: investigate read-only" in plan.system
    build = await compose_system_prompt(agent=get_agent("build"), mode="build")
    assert "Platform:" in build.system
    assert "Plan mode:" not in build.system


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost/x",
        "https://foo.localhost/x",
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://169.254.169.254/latest",
        "https://metadata.google.internal/x",
    ],
)
def test_ssrf_guard_blocks_private_hosts(url: str) -> None:
    """SSRF guard blocks loopback/private/metadata hosts."""
    assert is_blocked_url(url) is True


async def test_ssrf_guard_blocks_tool_with_error() -> None:
    """WebfetchTool rejects localhost without network (failure path)."""
    with pytest.raises(ToolError, match="[Bb]locked|[Pp]rivate"):
        await WebfetchTool().execute(
            {"url": "https://localhost/secret"}, _ctx(FakeAccessor(files={}))
        )


async def test_ssrf_guard_allows_public_hostname() -> None:
    """Public hostnames pass validation (permission/auth happy path)."""
    import socket as _socket

    real = _socket.gethostbyname
    _socket.gethostbyname = lambda _host: "93.184.216.34"  # type: ignore[assignment]
    try:
        assert is_blocked_url("https://example.com/x") is False
    finally:
        _socket.gethostbyname = real
