"""Tests for LLM retry/format semantics, grounding/OCR, and the code agent."""

from __future__ import annotations

import pytest

from apps.harness.agent_s import code_agent as code_agent_mod
from apps.harness.agent_s import formatting as formatting_mod
from apps.harness.agent_s import grounding as grounding_mod
from apps.harness.agent_s import llm as llm_mod
from apps.harness.agent_s import prompts as prompts_mod
from apps.harness.agent_s.actions_parser import parse_action_line
from apps.harness.tests.agent_s_conftest import (
    TINY_PNG,
    ScriptedCompletion,
    StubCodeExecution,
    StubOcr,
)

pytestmark = pytest.mark.asyncio


async def _no_sleep(_delay: float) -> None:
    return None


def _ocr_rows_hello_world() -> list[dict]:
    """OCR rows whose cleaned words are exactly ``Hello`` / ``world!``.

    NOTE: Agent-S cleans with
    ``re.sub(r"^[^a-zA-Z\\s.,!?;:\\-\\+]+|[^a-zA-Z\\s.,!?;:\\-\\+]+$", "", word)``,
    which only strips *non-matching* runs at the edges: leading whitespace is
    kept (``\\s`` is in the allowed class) and ``.``/``!`` are kept. So
    ``"  ...Hello!!! "`` cleans to itself (truthy) rather than to ``Hello``,
    and even ``"   "`` stays truthy (kept as a row — quirk preserved).
    These rows use parens/digits, which the regex genuinely strips.
    """
    return [
        {
            "text": "(Hello)",
            "block_num": 0,
            "left": 10,
            "top": 20,
            "width": 30,
            "height": 10,
        },
        {
            "text": "(world!)",
            "block_num": 0,
            "left": 50,
            "top": 20,
            "width": 40,
            "height": 10,
        },
    ]


def _wire(text: str = "sys") -> list[dict]:
    return [{"role": "system", "content": [{"type": "text", "text": text}]}]


async def test_call_llm_safe_retries_then_succeeds():
    port = ScriptedCompletion(
        scripts={"worker": ["agent.wait(1.0)"]}, failures={"worker": 2}
    )
    sleeps: list = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    text, usages = await llm_mod.call_llm_safe(
        port, _wire(), purpose="worker", temperature=0.0, sleep=_sleep
    )
    assert text == "agent.wait(1.0)"
    # Only the successful attempt records usage (Agent-S breaks on success).
    assert len(usages) == 1
    assert len(port.calls_for("worker")) == 3
    assert [c["temperature"] for c in port.calls_for("worker")] == [0.0, 0.0, 0.0]
    # Sleeps after each failed attempt only — none after success.
    assert sleeps == [1.0, 1.0]


async def test_call_llm_safe_returns_empty_after_three_failures():
    port = ScriptedCompletion(failures={"worker": 10})
    sleeps: list = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    text, usages = await llm_mod.call_llm_safe(
        port, _wire(), purpose="worker", sleep=_sleep
    )
    assert text == ""
    assert usages == []
    assert len(port.calls_for("worker")) == 3
    # Reference sleeps after every catch — even the third/final failure.
    assert sleeps == [1.0, 1.0, 1.0]


async def test_call_llm_safe_no_sleep_on_immediate_success():
    port = ScriptedCompletion(scripts={"worker": ["agent.wait(1.0)"]})
    sleeps: list = []

    async def _sleep(delay: float) -> None:
        sleeps.append(delay)

    text, _ = await llm_mod.call_llm_safe(port, _wire(), purpose="worker", sleep=_sleep)
    assert text == "agent.wait(1.0)"
    assert sleeps == []


async def test_call_llm_formatted_feedback_messages_and_retry():
    bad = "oops no code block here"
    good = "```python\nagent.wait(1.0)\n```"
    port = ScriptedCompletion(scripts={"worker": [bad, good]})
    messages = _wire()
    seen: list[tuple[bool, str]] = []

    async def probe(response: str) -> tuple[bool, str]:
        ok, _ = formatting_mod.single_action_check(response)
        seen.append((ok, response))
        if not ok:
            return False, "SINGLE_ACTION feedback"
        return await formatting_mod.code_valid_check_async(response, _ok_materialize)

    async def _ok_materialize(action):
        return object()

    text, _ = await llm_mod.call_llm_formatted(
        port,
        messages,
        [probe],
        purpose="worker",
        formatting_template=prompts_mod.FORMATTING_FEEDBACK_PROMPT,
        sleep=_no_sleep,
    )
    assert text == good
    # Local feedback messages are appended to the working copy only.
    assert len(messages) == 1
    recorded = port.calls_for("worker")
    assert len(recorded) == 2
    second_try = recorded[1]["messages"]
    assert second_try[-2]["role"] == "assistant"
    assert second_try[-2]["content"][0]["text"] == bad
    assert second_try[-1]["role"] == "user"
    feedback_text = second_try[-1]["content"][0]["text"]
    assert feedback_text.startswith(
        "\nYour previous response was not formatted correctly."
    )
    assert "- SINGLE_ACTION feedback" in feedback_text
    assert [ok for ok, _ in seen] == [False, True]


async def test_call_llm_formatted_returns_last_after_three_bad_attempts():
    port = ScriptedCompletion(scripts={"worker": ["bad1", "bad2", "bad3", "good"]})
    messages = _wire()
    text, _ = await llm_mod.call_llm_formatted(
        port,
        messages,
        [lambda r: (False, "always bad")],
        purpose="worker",
        formatting_template=prompts_mod.FORMATTING_FEEDBACK_PROMPT,
        sleep=_no_sleep,
    )
    assert text == "bad3"
    assert len(port.calls_for("worker")) == 3


async def test_grounding_prompt_text_last_and_parse():
    port = ScriptedCompletion(scripts={"grounding": ["[123, 456]"]})
    coords, _ = await grounding_mod.generate_coords(
        port, TINY_PNG, "The blue Kiruna button", sleep=_no_sleep
    )
    assert coords == [123, 456]
    call = port.calls_for("grounding")[0]
    assert call["temperature"] == 0.0
    assert call["use_thinking"] is False
    # Agent-S never passes an explicit grounding system prompt, so LMMAgent
    # falls back to "You are a helpful assistant." (quirk pinned).
    assert call["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": "You are a helpful assistant."}],
    }
    assert grounding_mod.GROUNDING_DEFAULT_SYSTEM_PROMPT == (
        "You are a helpful assistant."
    )
    msg = call["messages"][-1]
    assert msg["role"] == "user"
    assert msg["content"][0]["type"] == "image_url"
    assert msg["content"][-1] == {
        "type": "text",
        "text": "Query:The blue Kiruna button\n"
        "Output only the coordinate of one point in your response.\n",
    }


async def test_resize_coordinates_exact_formula():
    assert grounding_mod.resize_coordinates(
        [500, 250], width=1920, height=1080, grounding_width=1000, grounding_height=1000
    ) == [round(500 * 1920 / 1000), round(250 * 1080 / 1000)]
    assert grounding_mod.resize_coordinates(
        [1, 1], width=1920, height=1080, grounding_width=1000, grounding_height=1000
    ) == [2, 1]


async def test_ocr_table_cleaning_grouping_and_text_span_coords():
    rows = _ocr_rows_hello_world()
    table, elems = grounding_mod.build_ocr_table(rows)
    assert table == "Text Table:\nWord id\tText\n0\tHello\n1\tworld!\n"
    assert [e["word_num"] for e in elems] == [1, 2]
    # Whitespace-only rows are KEPT by Agent-S (cleaned "   " is truthy):
    # document the quirk explicitly.
    table2, elems2 = grounding_mod.build_ocr_table(
        [{"text": "   ", "block_num": 0, "left": 0, "top": 0, "width": 1, "height": 1}]
    )
    assert table2 == "Text Table:\nWord id\tText\n0\t   \n"
    assert len(elems2) == 1
    # Alignment variants use integer halves, no resize.
    assert grounding_mod.text_span_coords(elems[0], "start") == [10, 25]
    assert grounding_mod.text_span_coords(elems[0], "end") == [40, 25]
    assert grounding_mod.text_span_coords(elems[0], "") == [25, 25]

    port = ScriptedCompletion(scripts={"text_span": ["thinking then id 1"]})
    coords, elem, _ = await grounding_mod.generate_text_coords(
        port, rows, TINY_PNG, "hello world", alignment="end", sleep=_no_sleep
    )
    assert elem["id"] == 1
    assert coords == [90, 25]
    first, second = port.calls_for("text_span")[0]["messages"][1:]
    assert first["role"] == "user" and len(first["content"]) == 1
    assert first["content"][0]["text"].startswith(
        "**Important**: Output the word id of the LAST word"
    )
    assert second["content"][0] == {"type": "text", "text": "Screenshot:\n"}
    assert second["content"][1]["type"] == "image_url"


async def test_materializer_forwards_grounding_text_and_code_usages_to_sink():
    """drag/highlight/code-agent must forward usages via usage_sink (like click)."""
    from apps.harness.agent_s.actions_parser import parse_action_line as _parse
    from apps.harness.agent_s.materializer import (
        DefaultActionMaterializer as _Mat,
    )

    port = ScriptedCompletion(
        scripts={
            "grounding": ["[10, 20]", "[30, 40]"],
            "text_span": ["0", "1"],
            "code": ["<thoughts>t</thoughts><answer>DONE</answer>"],
            "code_summary": ["summary text"],
        }
    )
    ocr = StubOcr(
        rows=[
            {
                "text": "(hello)",
                "block_num": 0,
                "left": 10,
                "top": 20,
                "width": 30,
                "height": 10,
            },
            {
                "text": "(world)",
                "block_num": 0,
                "left": 60,
                "top": 20,
                "width": 30,
                "height": 10,
            },
        ]
    )
    mat = _Mat(
        completion=port,
        ocr=ocr,
        code_execution=StubCodeExecution(),
    )
    seen: list = []
    mat.usage_sink = lambda p, u: seen.append(p)  # noqa: E731
    mat.current_task_instruction = "Full task instruction"

    await mat.materialize(
        _parse(
            'agent.drag_and_drop("The file icon on the desktop", "The folder window")'
        ),
        {"screenshot": TINY_PNG},
    )
    assert seen == ["grounding", "grounding"]

    seen.clear()
    await mat.materialize(
        _parse('agent.highlight_text_span("hello", "world")'),
        {"screenshot": TINY_PNG},
    )
    assert seen == ["text_span", "text_span"]

    seen.clear()
    await mat.materialize(_parse("agent.call_code_agent()"), {"screenshot": TINY_PNG})
    assert seen == ["code", "code_summary"]


async def test_code_agent_done_flow_and_result_shape():
    port = ScriptedCompletion(
        scripts={
            "code": [
                "<thoughts>t1</thoughts><answer>```python\nprint('hi')\n```</answer>",
                "<thoughts>t2</thoughts><answer>DONE</answer>",
            ],
            "code_summary": ["Did things."],
        }
    )
    executor = StubCodeExecution(
        results=[{"status": "ok", "output": "hi", "error": "", "return_code": 0}]
    )
    result, usages = await code_agent_mod.run_code_agent(
        port, executor, "Do the thing", TINY_PNG, budget=20, sleep=_no_sleep
    )
    assert result.completion_reason == "DONE"
    # Quirk: steps_executed excludes the terminal DONE turn.
    assert result.steps_executed == 1
    assert len(result.execution_history) == 2
    assert [p["purpose"] for p in [c for c in port.calls]] == [
        "code",
        "code",
        "code_summary",
    ]
    assert all(c["temperature"] == 1.0 for c in port.calls)
    assert executor.calls[0]["kind"] == "python"
    assert set(result.as_dict()) == {
        "task_instruction",
        "completion_reason",
        "summary",
        "execution_history",
        "steps_executed",
        "budget",
    }
    assert usages


async def test_code_agent_fail_budget_and_empty_history_summary():
    port = ScriptedCompletion(
        scripts={
            "code": ["<thoughts>t</thoughts><answer>```bash\necho hi\n```</answer>"]
            * 3,
            "code_summary": ["irrelevant"],
        }
    )
    result, _ = await code_agent_mod.run_code_agent(
        port, StubCodeExecution(), "task", TINY_PNG, budget=2, sleep=_no_sleep
    )
    assert result.completion_reason == "BUDGET_EXHAUSTED_AFTER_2_STEPS"
    assert result.steps_executed == 2

    port2 = ScriptedCompletion(scripts={"code": ["FAIL"]})
    result2, _ = await code_agent_mod.run_code_agent(
        port2, StubCodeExecution(), "task", TINY_PNG, budget=5, sleep=_no_sleep
    )
    assert result2.completion_reason == "FAIL"
    assert result2.steps_executed == 0

    summary, _ = await code_agent_mod.generate_summary(
        port2, [], "task", sleep=_no_sleep
    )
    assert summary == "No actions were executed."


async def test_code_agent_empty_response_raises_and_format_result():
    port = ScriptedCompletion(scripts={"code": ["   "]})
    with pytest.raises(RuntimeError, match="empty response"):
        await code_agent_mod.run_code_agent(
            port, StubCodeExecution(), "task", TINY_PNG, budget=3, sleep=_no_sleep
        )
    assert code_agent_mod.format_result(None, 0).startswith("\nStep 1 Error:")
    assert "Return Code: -1" in code_agent_mod.format_result({"status": "skipped"}, 2)
    code_type, code = code_agent_mod.extract_code_block("```bash\necho hi\n```")
    assert (code_type, code) == ("bash", "echo hi")


async def test_code_valid_check_materializes_and_rejects():
    async def ok(action):
        assert parse_action_line("agent.wait(1.0)").method == action.method
        return object()

    async def boom(action):
        raise ValueError("bad grounding")

    valid_code = "```python\nagent.wait(1.0)\n```"
    assert (await formatting_mod.code_valid_check_async(valid_code, ok))[0]
    assert not (await formatting_mod.code_valid_check_async(valid_code, boom))[0]
    assert not (await formatting_mod.code_valid_check_async("no code here", ok))[0]
    ok_single, _ = formatting_mod.single_action_check("```python\nagent.wait(1.0)\n```")
    assert ok_single
    bad_single, msg = formatting_mod.single_action_check("no code here")
    assert not bad_single and msg == prompts_mod.SINGLE_ACTION_ERROR_MSG
