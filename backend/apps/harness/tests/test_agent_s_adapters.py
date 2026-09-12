"""Tests for the Agent-S harness adapters (second layer).

Covers :class:`HarnessCompletionAdapter` (exact Agent-S wire, tools=[],
main/grounding routing, temperature, thinking envelope, usage mapping,
provider reasoning only as ``part_updated`` reasoning events),
:class:`WorkspaceCodeExecutionAdapter` (bash/python result shapes),
:class:`WorkspaceActionExecutor` (exact ``desktop_action("execute")`` call),
:class:`WorkspaceOcrAdapter` (run-scoped write/exec/read + TSV parsing),
and :class:`DesktopScreenshotAdapter` (PNG format + max_dimension).
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.access.base import ExecResult, FileContent
from apps.harness.agent_s.adapters import (
    ACTION_EXECUTE_MAX_CHARS,
    ACTION_EXECUTE_TIMEOUT_S,
    AGENT_S_DEPS_HINT,
    DesktopScreenshotAdapter,
    HarnessCompletionAdapter,
    WorkspaceActionExecutor,
    WorkspaceCodeExecutionAdapter,
    WorkspaceOcrAdapter,
    decode_data_url_png,
    encode_png_data_url,
    extract_png_from_wire,
    parse_tesseract_tsv,
)
from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.harness.providers.resolver import ResolvedModel

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeProvider(ProviderAdapter):
    """Scripted provider recording chat_stream inputs."""

    name = "fake"

    def __init__(self, deltas: list[Delta] | None = None) -> None:
        self._deltas = list(deltas or [])
        self.calls: list[dict[str, Any]] = []

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Record the call and replay canned deltas."""
        self.calls.append(
            {
                "model": model,
                "messages": list(messages),
                "tools": list(tools),
                "opts": opts,
            }
        )
        for delta in self._deltas:
            yield delta


def _resolve(provider: FakeProvider, model_id: str = "bare-model"):
    def _fn(model_ref: str) -> ResolvedModel:
        bare = model_ref.split("/", 1)[-1] if "/" in model_ref else model_ref
        return ResolvedModel(
            adapter=provider,
            model_id=model_id or bare,
            provider="fake",
            context_length=0,
            max_output_tokens=0,
        )

    return _fn


def _wire(png: bytes = TINY_PNG) -> list[dict[str, Any]]:
    from apps.harness.agent_s import messages as wire

    return [
        wire.system_message("You are a helpful assistant."),
        wire.append_message(
            [wire.system_message("You are a helpful assistant.")],
            "Query:click the save button\n"
            "Output only the coordinate of one point in your response.\n",
            image_content=png,
            role="user",
            put_text_last=True,
        ),
    ]


async def test_completion_sends_exact_wire_with_tools_empty() -> None:
    """Wire messages map 1:1 to LLMMessage parts; tools=[] always."""
    provider = FakeProvider([Delta(text="[1, 2]", usage=Usage(3, 4, 7, 0.5))])
    adapter = HarnessCompletionAdapter(
        _resolve(provider),
        main_model="fake/main",
        grounding_model="fake/ground",
    )
    wire = _wire()
    result = await adapter.complete(
        wire, purpose="grounding", temperature=0.0, use_thinking=False
    )
    assert result.text == "[1, 2]"
    call = provider.calls[0]
    assert call["tools"] == []
    sent = call["messages"]
    assert [m.role for m in sent] == ["system", "user"]
    assert sent[0].content == [{"type": "text", "text": "You are a helpful assistant."}]
    user_parts = sent[1].content
    assert user_parts[0]["type"] == "image_url"
    assert user_parts[0]["image_url"]["detail"] == "high"
    assert user_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert decode_data_url_png(user_parts[0]["image_url"]["url"]) == TINY_PNG
    assert user_parts[-1] == {
        "type": "text",
        "text": "Query:click the save button\n"
        "Output only the coordinate of one point in your response.\n",
    }
    # The PNG on the wire round-trips through the logging helper.
    assert extract_png_from_wire(wire) == TINY_PNG
    assert encode_png_data_url(TINY_PNG).startswith("data:image/png;base64,")


async def test_completion_routes_main_and_grounding_models() -> None:
    """Worker/text purposes use main; grounding uses the grounding model."""
    provider = FakeProvider([Delta(text="ok")])
    seen: list[str] = []

    def _fn(model_ref: str) -> ResolvedModel:
        seen.append(model_ref)
        return ResolvedModel(
            adapter=provider,
            model_id="bare",
            provider="fake",
            context_length=0,
            max_output_tokens=0,
        )

    adapter = HarnessCompletionAdapter(
        _fn, main_model="fake/main", grounding_model="fake/ground"
    )
    wire = _wire()
    for purpose in ("worker", "reflection", "text_span", "code", "code_summary"):
        await adapter.complete(
            wire, purpose=purpose, temperature=0.0, use_thinking=False
        )
    await adapter.complete(
        wire, purpose="grounding", temperature=0.0, use_thinking=False
    )
    assert seen == ["fake/main"] * 5 + ["fake/ground"]
    assert [c["purpose"] for c in adapter.calls] == [
        "worker",
        "reflection",
        "text_span",
        "code",
        "code_summary",
        "grounding",
    ]
    assert adapter.main_model == "fake/main"
    assert adapter.grounding_model == "fake/ground"


async def test_completion_forwards_temperature_and_thinking_envelope() -> None:
    """Temperature lands on ChatOptions; thinking wraps reasoning+text."""
    provider = FakeProvider(
        [
            Delta(text="partial-", reasoning="hmm "),
            Delta(text="done", reasoning="aha", usage=Usage(1, 2, 3, 0.01)),
        ]
    )
    events: list[dict[str, Any]] = []

    async def _emit(event: dict[str, Any]) -> None:
        events.append(event)

    adapter = HarnessCompletionAdapter(
        _resolve(provider),
        main_model="fake/main",
        grounding_model="fake/main",
        emit=_emit,
    )
    result = await adapter.complete(
        _wire(), purpose="worker", temperature=0.7, use_thinking=True
    )
    assert provider.calls[0]["opts"].temperature == 0.7
    assert result.text == (
        "<thoughts>\nhmm aha\n</thoughts>\n<answer>\npartial-done\n</answer>"
    )
    # Aggregated usage maps prompt/completion/total + cost.
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1, 2)
    assert result.usage.cost == pytest.approx(0.01)
    # Provider reasoning surfaces only as part_updated reasoning events.
    assert events == [
        {"type": "part_updated", "delta": {"reasoning": "hmm "}},
        {"type": "part_updated", "delta": {"reasoning": "aha"}},
    ]


async def test_completion_reasoning_effort_only_for_chatgpt() -> None:
    """reasoning_effort survives only on chatgpt adapters (temperature safe)."""
    base = ChatOptions(temperature=0.0, reasoning_effort="high")

    class ChatGptProvider(FakeProvider):
        name = "chatgpt"

    chatgpt = ChatGptProvider([Delta(text="ok")])
    other = FakeProvider([Delta(text="ok")])
    for provider, expect_effort in ((chatgpt, "high"), (other, None)):
        adapter = HarnessCompletionAdapter(
            _resolve(provider),
            main_model="fake/main",
            grounding_model="fake/main",
            chat_options_factory=lambda: base,
        )
        await adapter.complete(
            _wire(), purpose="worker", temperature=0.0, use_thinking=False
        )
        opts = provider.calls[0]["opts"]
        assert opts.reasoning_effort == expect_effort
        assert opts.temperature == 0.0
        assert opts.tool_choice is None


async def test_completion_requires_main_model() -> None:
    """Empty main_model is rejected; grounding falls back to main."""
    provider = FakeProvider()
    with pytest.raises(ValueError, match="main_model"):
        HarnessCompletionAdapter(
            _resolve(provider), main_model="  ", grounding_model="fake/g"
        )
    adapter = HarnessCompletionAdapter(
        _resolve(provider), main_model="fake/main", grounding_model="  "
    )
    assert adapter.grounding_model == "fake/main"


class FakeCodeAccessor:
    """Minimal exec_wait fake for code-execution tests."""

    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def exec_wait(
        self, command, workdir="/workspace", env=None, timeout=None
    ) -> ExecResult:
        self.calls.append(
            {"command": list(command), "workdir": workdir, "timeout": timeout}
        )
        return self.result


async def test_code_execution_python_result_shape() -> None:
    """Python runs ``python3 -c`` with split stdout/stderr."""
    accessor = FakeCodeAccessor(ExecResult(exit_code=0, stdout="out", stderr="err"))
    adapter = WorkspaceCodeExecutionAdapter(accessor=accessor)
    result = await adapter.run_python("print(1)")
    assert result == {
        "status": "ok",
        "return_code": 0,
        "output": "out",
        "error": "err",
    }
    assert accessor.calls[0]["command"] == ["python3", "-c", "print(1)"]
    assert accessor.calls[0]["workdir"] == "/workspace"

    accessor = FakeCodeAccessor(ExecResult(exit_code=3, stdout="", stderr="boom"))
    result = await WorkspaceCodeExecutionAdapter(accessor=accessor).run_python("x")
    assert result["status"] == "error"
    assert result["return_code"] == 3
    assert result["error"] == "boom"


async def test_code_execution_bash_result_shape() -> None:
    """Bash runs ``bash -lc`` with combined stdout+stderr output."""
    accessor = FakeCodeAccessor(ExecResult(exit_code=0, stdout="o", stderr="e"))
    adapter = WorkspaceCodeExecutionAdapter(accessor=accessor)
    result = await adapter.run_bash("echo hi", timeout=7)
    assert result == {"status": "ok", "returncode": 0, "output": "oe", "error": ""}
    assert accessor.calls[0]["command"] == ["bash", "-lc", "echo hi"]
    assert accessor.calls[0]["timeout"] == 7

    accessor = FakeCodeAccessor(ExecResult(exit_code=1, stdout="", stderr="nope"))
    result = await WorkspaceCodeExecutionAdapter(accessor=accessor).run_bash("false")
    assert result["status"] == "error"
    assert result["returncode"] == 1
    assert result["output"] == "nope"


class FakeDesktopAccessor:
    """Minimal desktop_action fake."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        self.calls: list[tuple] = []

    async def desktop_action(
        self, action: str, args: dict[str, Any] | None = None, timeout=None
    ) -> dict[str, Any]:
        self.calls.append((action, dict(args or {}), timeout))
        return dict(self.result)


async def test_action_executor_calls_execute_with_materialized_code() -> None:
    """Only the materialized snippet runs via desktop execute RPC."""
    accessor = FakeDesktopAccessor(
        {"ok": True, "exit_code": 0, "stdout": "s", "stderr": ""}
    )
    executor = WorkspaceActionExecutor(accessor=accessor)
    result = await executor.execute_action_code(
        "import pyautogui; pyautogui.click(1, 2)"
    )
    assert result == {"exit_code": 0, "stdout": "s", "stderr": ""}
    assert accessor.calls == [
        (
            "execute",
            {"code": "import pyautogui; pyautogui.click(1, 2)"},
            ACTION_EXECUTE_TIMEOUT_S,
        )
    ]
    assert ACTION_EXECUTE_TIMEOUT_S == 120.0  # runner DESKTOP_EXECUTE_TIMEOUT_S parity


async def test_action_executor_reports_rebuild_hint_for_missing_deps() -> None:
    """Missing pyautogui/tesseract errors point at the image rebuild."""
    with pytest.raises(RuntimeError, match="Rebuild the workspace image"):
        await WorkspaceActionExecutor(
            accessor=FakeDesktopAccessor({"ok": False, "error": "nope"})
        ).execute_action_code("import pyautogui")
    with pytest.raises(RuntimeError, match="Rebuild the workspace image"):
        await WorkspaceActionExecutor(
            accessor=FakeDesktopAccessor(
                {
                    "ok": True,
                    "exit_code": 1,
                    "stdout": "",
                    "stderr": "ModuleNotFoundError: No module named 'pyautogui'",
                }
            )
        ).execute_action_code("import pyautogui")
    assert "Rebuild the workspace image" in AGENT_S_DEPS_HINT


async def test_action_executor_rejects_empty_and_failures() -> None:
    """Empty code is refused; ok/exit failures raise visibly."""
    executor = WorkspaceActionExecutor(accessor=FakeDesktopAccessor({"ok": True}))
    with pytest.raises(ValueError, match="empty action code"):
        await executor.execute_action_code("   ")
    with pytest.raises(RuntimeError, match="Desktop action execution failed"):
        await WorkspaceActionExecutor(
            accessor=FakeDesktopAccessor({"ok": False, "error": "nope"})
        ).execute_action_code("import pyautogui")
    with pytest.raises(RuntimeError, match="exit 3"):
        await WorkspaceActionExecutor(
            accessor=FakeDesktopAccessor(
                {"ok": True, "exit_code": 3, "stdout": "", "stderr": "bad"}
            )
        ).execute_action_code("import pyautogui")
    assert ACTION_EXECUTE_MAX_CHARS == 200_000


class FakeOcrAccessor:
    """Write/exec/read fake for OCR staging tests."""

    def __init__(self, tsv: str) -> None:
        self.tsv = tsv
        self.files: dict[str, bytes] = {}
        self.exec_calls: list[list[str]] = []

    async def write_file(self, path: str, content: bytes, mode=0o644) -> None:
        self.files[path] = bytes(content)

    async def exec_wait(
        self, command, workdir="/workspace", env=None, timeout=None
    ) -> ExecResult:
        self.exec_calls.append(list(command))
        return ExecResult(exit_code=0, stdout="", stderr="")

    async def read_file(self, path: str, max_size=None) -> FileContent:
        return FileContent(content=self.tsv.encode("utf-8"), size=len(self.tsv))


def _tsv() -> str:
    return (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext\n"
        "5\t1\t0\t0\t0\t1\t10\t20\t30\t12\t95\thello\n"
        "5\t1\t0\t0\t0\t2\t50\t20\t40\t12\t90\tworld\n"
        "4\t1\t0\t0\t0\t0\t10\t20\t80\t12\t95\t\n"
    )


async def test_ocr_run_scoped_write_exec_read_and_tsv() -> None:
    """PNG is staged run-scoped; tesseract TSV parses to word rows."""
    accessor = FakeOcrAccessor(_tsv())
    adapter = WorkspaceOcrAdapter(accessor=accessor, run_id="run 1!")
    rows = await adapter.read_words(TINY_PNG)
    assert rows == [
        {
            "text": "hello",
            "block_num": 0,
            "left": 10,
            "top": 20,
            "width": 30,
            "height": 12,
        },
        {
            "text": "world",
            "block_num": 0,
            "left": 50,
            "top": 20,
            "width": 40,
            "height": 12,
        },
    ]
    png_path = "/workspace/.opencuria/computeruse/run-1/ocr/frame.png"
    assert accessor.files[png_path] == TINY_PNG
    assert accessor.exec_calls[0][:2] == ["bash", "-lc"]
    assert "tesseract" in accessor.exec_calls[0][2]
    assert "tsv" in accessor.exec_calls[0][2]
    # pytesseract.image_to_data parity: no --psm override (Tesseract default).
    assert "--psm" not in accessor.exec_calls[0][2]
    assert await adapter.read_words(b"") == []


async def test_ocr_failure_raises() -> None:
    """Nonzero tesseract exit fails the run visibly."""

    class Failing(FakeOcrAccessor):
        async def exec_wait(
            self, command, workdir="/workspace", env=None, timeout=None
        ) -> ExecResult:
            return ExecResult(exit_code=1, stdout="", stderr="missing")

    with pytest.raises(RuntimeError, match="Workspace OCR failed"):
        await WorkspaceOcrAdapter(accessor=Failing(_tsv())).read_words(TINY_PNG)


def test_parse_tesseract_tsv_skips_non_word_rows() -> None:
    """Only level-5 rows with text survive parsing."""
    assert parse_tesseract_tsv("level\ttext\n5\t\n") == []
    assert parse_tesseract_tsv("") == []


async def test_screenshot_requests_png_with_max_dimension() -> None:
    """Screenshot RPC asks for PNG + configured max_dimension."""
    payload = base64.b64encode(TINY_PNG).decode("ascii")
    accessor = FakeDesktopAccessor(
        {"image_b64": payload, "width": 1920, "height": 1080}
    )
    adapter = DesktopScreenshotAdapter(accessor=accessor, max_dimension=2400)
    png, width, height = await adapter.capture_png()
    assert (png, width, height) == (TINY_PNG, 1920, 1080)
    assert accessor.calls == [
        ("screenshot", {"format": "png", "max_dimension": 2400}, None)
    ]


async def test_screenshot_rejects_missing_or_invalid_payload() -> None:
    """Empty image data, bad base64, or bad dims raise."""
    with pytest.raises(RuntimeError, match="image data"):
        await DesktopScreenshotAdapter(
            accessor=FakeDesktopAccessor({"image_b64": ""})
        ).capture_png()
    with pytest.raises(RuntimeError, match="invalid dimensions"):
        await DesktopScreenshotAdapter(
            accessor=FakeDesktopAccessor({"image_b64": "!!!"})
        ).capture_png()
    payload = base64.b64encode(TINY_PNG).decode("ascii")
    with pytest.raises(RuntimeError, match="invalid dimensions"):
        await DesktopScreenshotAdapter(
            accessor=FakeDesktopAccessor(
                {"image_b64": payload, "width": 0, "height": 0}
            )
        ).capture_png()


async def test_display_geometry_falls_back() -> None:
    """display_info failures fall back to 1920x1080."""

    class Failing:
        async def desktop_action(self, action, args=None, timeout=None):
            raise RuntimeError("desktop down")

    assert await DesktopScreenshotAdapter(accessor=Failing()).display_geometry() == (
        1920,
        1080,
    )
    assert await DesktopScreenshotAdapter(
        accessor=FakeDesktopAccessor({"ok": True, "width": 1600, "height": 900})
    ).display_geometry() == (1600, 900)
