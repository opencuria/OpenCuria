"""Tests for materializer command strings, worker steps, and sessions."""

from __future__ import annotations

import asyncio

import pytest

from apps.harness.agent_s import prompts as prompts_mod
from apps.harness.agent_s import worker as worker_mod
from apps.harness.agent_s.actions_parser import parse_action_line
from apps.harness.agent_s.materializer import (
    DefaultActionMaterializer,
    MaterializerConfig,
)
from apps.harness.agent_s.ports import (
    CallPurpose,
    Completion,
    CompletionPort,
    MaterializationResult,
)
from apps.harness.agent_s.session import AgentSession, create_session
from apps.harness.agent_s.worker import create_state, default_signature_binders
from apps.harness.tests.agent_s_conftest import (
    TINY_PNG,
    ScriptedCompletion,
    StubCodeExecution,
    StubOcr,
)

pytestmark = pytest.mark.asyncio


async def _no_sleep(_delay: float) -> None:
    return None


def _obs():
    return {"screenshot": TINY_PNG}


def _materializer(port, **kw):
    cfg = MaterializerConfig(
        platform=kw.pop("platform", "linux"),
        width=kw.pop("width", 1920),
        height=kw.pop("height", 1080),
        grounding_width=kw.pop("grounding_width", 1000),
        grounding_height=kw.pop("grounding_height", 1000),
        code_agent_budget=kw.pop("code_agent_budget", 20),
    )
    return DefaultActionMaterializer(
        completion=port,
        ocr=kw.pop("ocr", None),
        code_execution=kw.pop("code_execution", None),
        config=cfg,
    )


async def test_click_command_exact_strings():
    port = ScriptedCompletion(scripts={"grounding": ["[100, 200]"]})
    mat = _materializer(port)
    out = await mat.materialize(
        parse_action_line(
            'agent.click("The blue Kiruna button at the top of the window", 2, "right")'
        ),
        _obs(),
    )
    x, y = round(100 * 1920 / 1000), round(200 * 1080 / 1000)
    assert out.exec_code == (
        "import pyautogui; "
        f"""import pyautogui; pyautogui.click({x}, {y}, clicks=2, button='right'); """
    )
    assert out.terminal is None


async def test_click_hold_keys_and_type_unicode_branches():
    port = ScriptedCompletion(scripts={"grounding": ["[10, 10]", "[10, 10]"]})
    mat = _materializer(port)
    out = await mat.materialize(
        parse_action_line(
            'agent.click("The blue Kiruna button at the top of the window", '
            '1, "left", ["shift"])'
        ),
        _obs(),
    )
    assert "pyautogui.keyDown('shift'); " in out.exec_code
    assert "pyautogui.keyUp('shift'); " in out.exec_code

    ascii_out = await mat.materialize(
        parse_action_line(
            'agent.type("The search box at the top of the window", '
            'text="hello", overwrite=True, enter=True)'
        ),
        _obs(),
    )
    assert (
        "pyautogui.hotkey('ctrl', 'a'); pyautogui.press('backspace'); "
        in ascii_out.exec_code
    )
    assert "pyautogui.write('hello'); " in ascii_out.exec_code
    assert ascii_out.exec_code.endswith("pyautogui.press('enter'); ")

    uni_out = await mat.materialize(
        parse_action_line('agent.type(text="héllo")'), _obs()
    )
    assert "pyperclip.copy('héllo'); " in uni_out.exec_code
    assert "pyautogui.hotkey('ctrl', 'v'); " in uni_out.exec_code


async def test_switch_open_hotkey_hold_wait_done_fail_strings():
    port = ScriptedCompletion()
    mat = _materializer(port)
    out = await mat.materialize(
        parse_action_line("agent.switch_applications('Firefox')"), _obs()
    )
    assert out.exec_code == prompts_mod.UBUNTU_APP_SETUP.replace("APP_NAME", "Firefox")
    mat.config.platform = "darwin"
    out = await mat.materialize(parse_action_line("agent.open('Notes')"), _obs())
    assert out.exec_code == (
        "import pyautogui; import time; "
        "pyautogui.hotkey('command', 'space', interval=0.5); "
        "pyautogui.typewrite('Notes'); pyautogui.press('enter'); time.sleep(1.0)"
    )
    mat.config.platform = "linux"
    out = await mat.materialize(
        parse_action_line("agent.hotkey(['ctrl', 'c'])"), _obs()
    )
    assert out.exec_code == "import pyautogui; pyautogui.hotkey('ctrl', 'c')"
    out = await mat.materialize(
        parse_action_line("agent.hold_and_press(['ctrl'], ['c', 'v'])"), _obs()
    )
    assert out.exec_code == (
        "import pyautogui; pyautogui.keyDown('ctrl'); "
        "pyautogui.press(['c', 'v']); pyautogui.keyUp('ctrl'); "
    )
    out = await mat.materialize(parse_action_line("agent.wait(2.5)"), _obs())
    assert out.exec_code == "import time; time.sleep(2.5)"
    done = await mat.materialize(parse_action_line("agent.done()"), _obs())
    assert (done.exec_code, done.terminal) == ("DONE", "DONE")
    fail = await mat.materialize(parse_action_line("agent.fail()"), _obs())
    assert (fail.exec_code, fail.terminal) == ("FAIL", "FAIL")
    with pytest.raises(AssertionError):
        mat.config.platform = "plan9"
        await mat.materialize(parse_action_line("agent.open('X')"), _obs())


async def test_save_to_knowledge_wait_and_scroll_variants():
    port = ScriptedCompletion(scripts={"grounding": ["[100, 100]", "[100, 100]"]})
    mat = _materializer(port)
    out = await mat.materialize(
        parse_action_line('agent.save_to_knowledge(["a", "b"])'), _obs()
    )
    assert (out.exec_code, out.terminal) == ("WAIT", "WAIT")
    assert mat.notes == ["a", "b"]
    v = await mat.materialize(
        parse_action_line('agent.scroll("The long document in the middle", -3)'), _obs()
    )
    x, y = round(100 * 1920 / 1000), round(100 * 1080 / 1000)
    assert v.exec_code.endswith("pyautogui.vscroll(-3)")
    assert f"pyautogui.moveTo({x}, {y})" in v.exec_code
    h = await mat.materialize(
        parse_action_line('agent.scroll("The long document in the middle", 4, True)'),
        _obs(),
    )
    assert h.exec_code.endswith("pyautogui.hscroll(4)")


async def test_drag_highlight_setcell_codeagent_commands():
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
    mat = _materializer(port, ocr=ocr, code_execution=StubCodeExecution())
    drag = await mat.materialize(
        parse_action_line(
            'agent.drag_and_drop("The file icon on the desktop", "The folder window")'
        ),
        _obs(),
    )
    assert "pyautogui.moveTo(19, 22); " in drag.exec_code
    assert (
        "pyautogui.dragTo(58, 43, duration=1., button='left'); pyautogui.mouseUp(); "
        in drag.exec_code
    )

    hl = await mat.materialize(
        parse_action_line('agent.highlight_text_span("hello", "world")'), _obs()
    )
    assert hl.exec_code == (
        "import pyautogui; pyautogui.moveTo(10, 25); "
        "pyautogui.dragTo(90, 25, duration=1., button='left'); pyautogui.mouseUp(); "
    )

    cell = await mat.materialize(
        parse_action_line(
            'agent.set_cell_values({"A2": "hello"}, "Book.xlsx", "Sheet1")'
        ),
        _obs(),
    )
    assert cell.exec_code == prompts_mod.SET_CELL_VALUES_CMD.format(
        cell_values={"A2": "hello"}, app_name="Book.xlsx", sheet_name="Sheet1"
    )

    mat.current_task_instruction = "Full task instruction"
    code_out = await mat.materialize(
        parse_action_line("agent.call_code_agent()"), _obs()
    )
    assert code_out.exec_code == "import time; time.sleep(2.222)"
    assert code_out.code_agent_result["completion_reason"] == "DONE"
    assert mat.last_code_agent_result is not None
    mat.last_code_agent_result = None
    mat.current_task_instruction = None
    code_out2 = await mat.materialize(
        parse_action_line("agent.call_code_agent()"), _obs()
    )
    assert code_out2.exec_code == "import time; time.sleep(1.111)"


async def test_worker_step_turn_zero_message_order_and_single_materialize_count():
    plan = (
        "verification\n```python\n"
        'agent.click("The blue Kiruna button at the top of the window")\n'
        "```"
    )
    port = ScriptedCompletion(
        scripts={"worker": [plan], "grounding": ["[50, 50]", "[50, 50]"]}
    )
    mat = _materializer(port)
    state = create_state(
        platform="linux",
        worker_engine_params={"model": "gpt-4o"},
        code_execution_available=False,
    )
    out = await worker_mod.run_step(
        state,
        port,
        mat,
        "Do the thing",
        _obs(),
        sleep=_no_sleep,
        signature_binders=default_signature_binders(),
    )
    # CODE_VALID probe + final materialization = 2 grounding calls, and both
    # usages flow into the step via the temporary usage_sink.
    assert len(port.calls_for("grounding")) == 2
    assert out.plan == plan
    assert (
        out.plan_code.strip()
        == 'agent.click("The blue Kiruna button at the top of the window")'
    )
    assert out.exec_code.startswith("import pyautogui; ")
    assert out.reflection is None
    assert state.turn_count == 1
    assert [u.purpose for u in out.usages] == ["worker", "grounding", "grounding"]
    # The previous sink is restored so later materializations stay silent.
    assert mat.usage_sink is None

    worker_call = port.calls_for("worker")[0]
    user_msg = worker_call["messages"][-1]
    assert user_msg["role"] == "user"
    assert user_msg["content"][0] == {
        "type": "text",
        "text": "The initial screen is provided. No action has been taken yet.\n"
        "Current Text Buffer = []\n",
    }
    assert user_msg["content"][1]["type"] == "image_url"
    # TASK_DESCRIPTION expanded on turn 0.
    assert "TASK_DESCRIPTION" not in state.generator_messages[0]["content"][0]["text"]
    assert "Do the thing" in state.generator_messages[0]["content"][0]["text"]


async def test_worker_step_restores_previous_usage_sink_and_captures_all():
    """A pre-existing sink is restored; grounding/text/code/summary land in usages."""
    plan = "```python\nagent.call_code_agent()\n```"
    done = "<thoughts>t</thoughts><answer>DONE</answer>"
    port = ScriptedCompletion(
        scripts={
            "worker": [plan],
            # CODE_VALID probe + final materialization each run a full
            # code-agent turn; every turn must answer DONE immediately or
            # the agent burns its whole 20-step budget on defaults.
            "code": [done] * 4,
            "code_summary": ["summary text", "summary text"],
        }
    )
    mat = _materializer(port, code_execution=StubCodeExecution())
    mat.current_task_instruction = "Full task instruction"
    seen: list = []
    previous = lambda p, u: seen.append((p, u))  # noqa: E731
    mat.usage_sink = previous
    state = create_state(
        platform="linux",
        worker_engine_params={"model": "gpt-4o"},
        code_execution_available=True,
    )
    out = await worker_mod.run_step(
        state,
        port,
        mat,
        "Do the thing",
        _obs(),
        sleep=_no_sleep,
        signature_binders=default_signature_binders(),
    )
    purposes = [u.purpose for u in out.usages]
    # worker + CODE_VALID code-agent (code, code_summary) + final code-agent.
    assert purposes[0] == "worker"
    assert purposes.count("code") == 2
    assert purposes.count("code_summary") == 2
    assert out.exec_code == "import time; time.sleep(2.222)"
    # Previous sink object restored (identity), step usages did not leak into it.
    assert mat.usage_sink is previous and seen == []
    # A later materialization routes to the restored sink again.
    mat.current_task_instruction = "Full task instruction"
    port.scripts.setdefault("code", []).append(
        "<thoughts>t</thoughts><answer>DONE</answer>"
    )
    port.scripts.setdefault("code_summary", []).append("summary text")
    from apps.harness.agent_s.actions_parser import parse_action_line as _parse

    await mat.materialize(_parse("agent.call_code_agent()"), _obs())
    assert [p for p, _ in seen] == ["code", "code_summary"]


async def test_worker_reflection_notes_code_block_and_double_materialize():
    plan = "```python\nagent.save_to_knowledge(['note-a'])\n```"
    port = ScriptedCompletion(
        scripts={
            "worker": [plan],
            "reflection": ["<thoughts>rt</thoughts><answer>keep going</answer>"],
            "code": ["<thoughts>t</thoughts><answer>DONE</answer>"],
            "code_summary": ["did stuff"],
        }
    )
    mat = _materializer(port, code_execution=StubCodeExecution())
    mat.last_code_agent_result = {
        "task_instruction": "task",
        "steps_executed": 1,
        "budget": 20,
        "completion_reason": "DONE",
        "summary": "did stuff",
        "execution_history": [{"action": "```python\nprint(1)\n```"}],
    }
    state = create_state(platform="linux", worker_engine_params={})
    state.turn_count = 1
    state.worker_history.append("previous plan text")
    state.generator_messages.append(
        {"role": "user", "content": [{"type": "text", "text": "prev"}]}
    )
    state.generator_messages.append(
        {"role": "assistant", "content": [{"type": "text", "text": "prev-a"}]}
    )
    out = await worker_mod.run_step(
        state,
        port,
        mat,
        "Do the thing",
        _obs(),
        sleep=_no_sleep,
        signature_binders=default_signature_binders(),
    )
    assert out.reflection == "keep going"
    assert out.reflection_thoughts == "rt"
    assert state.reflections == ["keep going"]
    # CODE_VALID probe materialized save_to_knowledge once, final once more.
    assert mat.notes == ["note-a", "note-a"]
    assert out.exec_code == "WAIT" and out.terminal == "WAIT"
    user_text = port.calls_for("worker")[0]["messages"][-1]["content"][0]["text"]
    assert user_text.startswith(
        "REFLECTION: You may use this reflection on the previous action "
        "and overall trajectory:\nkeep going\n"
    )
    assert "\nCODE AGENT RESULT:\n" in user_text
    assert "Steps Completed: 1\n" in user_text
    assert "Step 1: \n```python\nprint(1)\n```\n" in user_text
    assert mat.last_code_agent_result is None  # consumed after use
    assert [u.purpose for u in out.usages][0] == "reflection"


async def test_worker_fallback_wait_and_session_predict_and_flush():
    port = ScriptedCompletion(scripts={"worker": ["no code fences at all"]})
    mat = _materializer(port)
    state = create_state(platform="linux", worker_engine_params={})
    out = await worker_mod.run_step(
        state,
        port,
        mat,
        "task",
        _obs(),
        sleep=_no_sleep,
        signature_binders=default_signature_binders(),
    )
    assert out.plan_code == ""
    assert out.exec_code == "import time; time.sleep(1.333)"

    session = create_session(
        ScriptedCompletion(scripts={"worker": ["```python\nagent.done()\n```"]}),
        platform="linux",
    )
    info, actions = await session.predict("task", _obs(), sleep=_no_sleep)
    assert actions == ["DONE"]
    assert info["exec_code"] == "DONE"
    assert session.state.turn_count == 1

    # Exact Agent-S quirk: pop(1) twice from [system,u1,a1,u2,a2].
    # The first pop removes u1 -> [system,a1,u2,a2]; the second pop removes
    # a1 (now at index 1) -> [system,u2,a2]. Index 1 is popped twice.
    st = create_state(
        platform="linux",
        worker_engine_params={"engine_type": "other"},
        max_trajectory_length=1,
    )
    st.generator_messages += [
        {"role": "user", "content": [{"type": "text", "text": "u1"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        {"role": "user", "content": [{"type": "text", "text": "u2"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a2"}]},
    ]
    worker_mod.flush_messages(st)
    assert len(st.generator_messages) == 3
    assert [m["content"][0]["text"] for m in st.generator_messages] == [
        st.generator_messages[0]["content"][0]["text"],
        "u2",
        "a2",
    ]
    assert st.generator_messages[1]["content"][0]["text"] == "u2"
    assert st.generator_messages[2]["content"][0]["text"] == "a2"

    # Long-context flush keeps only the newest image.
    st2 = create_state(
        platform="linux",
        worker_engine_params={"engine_type": "openai"},
        max_trajectory_length=1,
    )
    st2.generator_messages = [
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
    worker_mod.flush_messages(st2)
    assert len(st2.generator_messages[1]["content"]) == 1  # image stripped
    assert len(st2.generator_messages[2]["content"]) == 2  # newest kept


def _worker_system_text(state) -> str:
    return state.generator_messages[0]["content"][0]["text"]


async def test_create_state_linux_without_code_hides_code_action_keeps_set_cell():
    state = create_state(platform="linux", code_execution_available=False)
    system = _worker_system_text(state)
    assert "def call_code_agent" not in system
    assert "call_code_agent" in system  # prose mention stays (tail/head)
    assert "def set_cell_values" in system


async def test_create_state_linux_with_code_shows_both_actions():
    state = create_state(platform="linux", code_execution_available=True)
    system = _worker_system_text(state)
    assert "def call_code_agent" in system
    assert "def set_cell_values" in system


async def test_create_state_non_linux_without_code_hides_both_actions():
    state = create_state(platform="darwin", code_execution_available=False)
    system = _worker_system_text(state)
    assert "def call_code_agent" not in system
    assert "def set_cell_values" not in system


async def test_create_session_without_code_hides_code_action():
    session = create_session(
        ScriptedCompletion(scripts={"worker": ["```python\nagent.done()\n```"]}),
        platform="linux",
    )
    system = _worker_system_text(session.state)
    assert "def call_code_agent" not in system
    assert "def set_cell_values" in system


async def test_create_session_with_code_shows_code_action():
    session = create_session(
        ScriptedCompletion(scripts={"worker": ["```python\nagent.done()\n```"]}),
        platform="linux",
        code_execution=StubCodeExecution(),
    )
    system = _worker_system_text(session.state)
    assert "def call_code_agent" in system
    assert "def set_cell_values" in system


async def test_agent_session_custom_materializer_requires_explicit_availability():
    port = ScriptedCompletion(scripts={"worker": ["```python\nagent.done()\n```"]})

    class _CustomMaterializer:
        async def materialize(self, action, obs):
            return MaterializationResult(exec_code="DONE", terminal="DONE")

    with pytest.raises(ValueError, match="code_execution_available"):
        AgentSession(completion=port, materializer=_CustomMaterializer())

    session = AgentSession(
        completion=port,
        materializer=_CustomMaterializer(),
        code_execution_available=True,
    )
    assert "def call_code_agent" in _worker_system_text(session.state)
    hidden = AgentSession(
        completion=port,
        materializer=_CustomMaterializer(),
        code_execution_available=False,
    )
    assert "def call_code_agent" not in _worker_system_text(hidden.state)


async def test_session_reset_preserves_notes_but_resets_worker_turn_history():
    port = ScriptedCompletion(scripts={"worker": ["```python\nagent.done()\n```"]})
    session = create_session(port, platform="linux")
    mat = session.materializer
    assert isinstance(mat, DefaultActionMaterializer)
    out = await worker_mod.run_step(
        session.state,
        port,
        mat,
        "task",
        _obs(),
        sleep=_no_sleep,
        signature_binders=default_signature_binders(),
    )
    assert out.exec_code == "DONE"
    # Grounding-agent analogue: Worker.reset clears turn/history but never
    # touched grounding_agent.notes (notes live on the materializer).
    mat.notes.append("kept-fact")
    stale_history = list(session.state.worker_history)
    assert stale_history and session.state.turn_count == 1
    session.reset()
    assert mat.notes == ["kept-fact"]
    assert session.state.turn_count == 0
    assert session.state.worker_history == []
    assert session.state.reflections == []
    assert session.state.screenshot_inputs == []


class _BaseExceptionCompletion(CompletionPort):
    """CompletionPort raising a BaseException `call_llm_safe` must not swallow.

    `call_llm_safe` catches only `Exception`; a `BaseException` (custom blowup
    or `asyncio.CancelledError`) propagates through `run_step`, exercising the
    outer finally that restores `usage_sink`.
    """

    def __init__(self, exc: BaseException):
        self.exc = exc

    async def complete(
        self,
        messages: list[dict],
        *,
        purpose: CallPurpose,
        temperature: float,
        use_thinking: bool,
    ) -> Completion:
        if purpose == "worker":
            raise self.exc
        return Completion(text="```python\nagent.wait(1.0)\n```")


class _CompletionBlowup(BaseException):
    pass


async def test_run_step_restores_usage_sink_on_completion_exception():
    port = _BaseExceptionCompletion(_CompletionBlowup("boom"))
    mat = DefaultActionMaterializer(completion=port)
    previous = lambda p, u: None  # noqa: E731
    mat.usage_sink = previous
    state = create_state(platform="linux", code_execution_available=False)
    with pytest.raises(_CompletionBlowup, match="boom"):
        await worker_mod.run_step(
            state,
            port,
            mat,
            "task",
            _obs(),
            sleep=_no_sleep,
            signature_binders=default_signature_binders(),
        )
    # True outer-finally path: the pre-existing sink object is restored
    # even though the worker completion raised past `call_llm_safe`.
    assert mat.usage_sink is previous


async def test_run_step_restores_usage_sink_on_cancelled_error():
    port = _BaseExceptionCompletion(asyncio.CancelledError("cancelled"))
    mat = DefaultActionMaterializer(completion=port)
    previous = lambda p, u: None  # noqa: E731
    mat.usage_sink = previous
    state = create_state(platform="linux", code_execution_available=False)
    with pytest.raises(asyncio.CancelledError):
        await worker_mod.run_step(
            state,
            port,
            mat,
            "task",
            _obs(),
            sleep=_no_sleep,
            signature_binders=default_signature_binders(),
        )
    # CancelledError is BaseException (not Exception) in modern Python, so
    # call_llm_safe does not swallow it; the step's finally still restores.
    assert mat.usage_sink is previous
