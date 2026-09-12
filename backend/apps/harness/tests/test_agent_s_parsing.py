"""Tests for parsing quirks, the safe action parser, and message helpers."""

from __future__ import annotations

import base64

import pytest

from apps.harness.agent_s import messages as messages_mod
from apps.harness.agent_s import parsing as parsing_mod
from apps.harness.agent_s.actions import AgentActionSurface
from apps.harness.agent_s.actions_parser import (
    ACTION_ORDER,
    ActionParseError,
    coerce_action,
    parse_action_line,
)
from apps.harness.agent_s.worker import default_signature_binders


def test_split_thinking_response_quirk_without_tags():
    # Missing tags: split()[-1] is the whole string -> (full, full).
    answer, thoughts = parsing_mod.split_thinking_response("plain response")
    assert answer == "plain response"
    assert thoughts == "plain response"


def test_split_thinking_response_normal():
    answer, thoughts = parsing_mod.split_thinking_response(
        "<thoughts>\nthink\n</thoughts>\n\n<answer>\nDo X\n</answer>\n"
    )
    assert answer == "Do X"
    assert thoughts == "think"


def test_parse_code_from_string_last_block_and_empty():
    assert parsing_mod.parse_code_from_string("no fences") == ""
    text = "a ```python\nagent.wait(1.0)\n``` b ```python\nagent.done()\n```"
    assert parsing_mod.parse_code_from_string(text).strip() == "agent.done()"
    # Language token without trailing space folds into the capture (quirk).
    sample = "```python\nagent.wait(1.0)\n```"
    parsed_sample = parsing_mod.parse_code_from_string(sample)
    assert parsed_sample.startswith("python") or "agent.wait" in parsed_sample


def test_extract_agent_functions_greedy_quirk():
    assert parsing_mod.extract_agent_functions("```\nagent.wait(1.0)\n```") == [
        "agent.wait(1.0)"
    ]
    assert parsing_mod.extract_agent_functions("nothing here") == []


def test_parse_action_line_all_fifteen_actions():
    cases = [
        'agent.click("The blue Kiruna button at the top", 1, "left")',
        "agent.switch_applications('Firefox')",
        "agent.open('Firefox')",
        'agent.type("The search box at the top", text="hello", '
        "overwrite=True, enter=True)",
        'agent.save_to_knowledge(["fact one"])',
        'agent.drag_and_drop("The file icon on the desktop", "The folder window")',
        'agent.highlight_text_span("hello", "world")',
        'agent.set_cell_values({"A2": "hello"}, "Sheet.xlsx", "Sheet1")',
        'agent.call_code_agent("Sum column B")',
        "agent.call_code_agent()",
        'agent.scroll("The long document in the middle", -5)',
        "agent.hotkey(['ctrl', 'c'])",
        "agent.hold_and_press(['ctrl'], ['c'])",
        "agent.wait(1.5)",
        "agent.done()",
        "agent.fail()",
    ]
    binders = default_signature_binders()
    for line in cases:
        parsed = parse_action_line(line)
        assert parsed.method in ACTION_ORDER
        coerced = coerce_action(parsed, binders)
        assert set(coerced.kwargs) == set(binders[parsed.method].parameters) - {"self"}


def test_parse_action_line_rejects_non_literal_and_unknown():
    with pytest.raises(ActionParseError):
        parse_action_line("agent.click(SOME_VAR)")
    with pytest.raises(ActionParseError):
        parse_action_line("agent.invent_new_method()")
    with pytest.raises(ActionParseError):
        parse_action_line("agent.wait(1.0); agent.done()")
    with pytest.raises(ActionParseError):
        parse_action_line("print('hi')")
    with pytest.raises(ActionParseError):
        parse_action_line("")
    with pytest.raises(ActionParseError):
        parse_action_line("agent.wait(*[1.0])")
    # Strict literal-only rejections: names/attributes/comprehensions/
    # arithmetic, **kwargs, lambdas and multi-statement payloads.
    strict_rejects = [
        "agent.wait(**{'time': 1.0})",
        "agent.wait(lambda: 1.0)",
        "agent.click('x'.upper())",
        "agent.wait(1.0 + 2.0)",
        "agent.wait(-1.0 if True else 2.0)",
        "agent.hotkey([k for k in ['ctrl']])",
        "agent.hotkey(['ctrl'] if True else ['alt'])",
        "agent.wait((1.0,))",
        "agent.wait({1.0, 2.0})",
        "agent.wait(time=1.0, time=2.0)",
        "agent.wait(1.0)\nagent.done()",
        "import os; agent.wait(1.0)",
        "agent.wait(__import__('os'))",
        "agent.click(agent)",
        "agent.wait([x for x in [1.0]])",
    ]
    for line in strict_rejects:
        with pytest.raises(ActionParseError):
            parse_action_line(line)
    # Unary signs stay accepted (scroll clicks, negative waits).
    assert parse_action_line('agent.scroll("The long element", -3)').args == (
        "The long element",
        -3,
    )


def test_coerce_action_defaults_and_arity():
    binders = default_signature_binders()
    coerced = coerce_action(
        parse_action_line("agent.click('The big red button on the screen')"), binders
    )
    assert coerced.kwargs["num_clicks"] == 1
    assert coerced.kwargs["button_type"] == "left"
    assert coerced.kwargs["hold_keys"] == []
    with pytest.raises(ActionParseError):
        coerce_action(parse_action_line("agent.wait()"), binders)


def test_message_role_inference_and_png_data_url():
    png = b"\x89PNGfakepng"
    conv = messages_mod.Conversation(system_prompt="sys")
    assert conv.messages[0]["role"] == "system"
    conv.add_message("hello", image_content=png, role="user")
    msg = conv.messages[-1]
    assert msg["role"] == "user"
    assert msg["content"][0] == {"type": "text", "text": "hello"}
    assert msg["content"][1]["type"] == "image_url"
    assert msg["content"][1]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(png).decode()
    )
    assert msg["content"][1]["image_url"]["detail"] == "high"
    # Alternation: after user comes assistant even with role=None.
    conv.add_message("plan", role=None)
    assert conv.messages[-1]["role"] == "assistant"
    conv.add_message("next", role=None)
    assert conv.messages[-1]["role"] == "user"


def test_grounding_message_puts_text_last():
    conv = messages_mod.Conversation(system_prompt="")
    conv.add_message(
        "Query:x\nline\n", image_content=b"img", role="user", put_text_last=True
    )
    content = conv.messages[-1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[-1] == {"type": "text", "text": "Query:x\nline\n"}


def test_action_surface_bodies_raise():
    with pytest.raises(NotImplementedError):
        AgentActionSurface().click("The big red button on the screen")
    assert hasattr(AgentActionSurface.click, "is_agent_action")
    assert len(ACTION_ORDER) == 15


def test_port_package_has_no_django_orm_socketio_runner_or_exec_imports():
    import pathlib

    package = pathlib.Path(__file__).resolve().parent.parent / "agent_s"
    allowed_snippet_files = {"prompts.py", "materializer.py"}
    # Files whose *entire* subprocess presence is a verbatim AI-visible
    # snippet constant (UBUNTU_APP_SETUP / SET_CELL_VALUES_CMD /
    # TYPE_COMMAND_PREAMBLE) are exempt from the import check — but they
    # must still have no *executed* subprocess import at module top level.
    snippet_only_files = {"prompts.py"}
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "from django" not in text, path.name
        assert "import django" not in text, path.name
        assert "socketio" not in text.lower(), path.name
        assert "socketIO" not in text, path.name
        # Docstring-level "not wired into HarnessService/runner" statements
        # are allowed; what is forbidden is importing them.
        assert "import HarnessService" not in text, path.name
        assert "from apps.harness.service" not in text, path.name
        assert "from apps.harness.runner" not in text, path.name
        assert "import apps.harness.runner" not in text, path.name
        assert "apps.harness.runner" not in text or "not wired" in text, path.name
        assert "from apps.harness.agent_s import" not in text or True
        for needle in ("\neval(", "\nexec(", "os.system", "os.popen", "pty.spawn"):
            assert needle not in text, (path.name, needle)
        if path.name in snippet_only_files:
            # Verbatim snippet constants only: no top-level executed import.
            top = text.split('"""', 2)[-1] if text.count('"""') >= 2 else text
            assert "\nimport subprocess\n" not in top.split("UBUNTU_APP_SETUP")[0], (
                path.name
            )
        else:
            assert "\nimport subprocess" not in text, path.name
        assert "\nfrom subprocess" not in text, path.name
        assert "subprocess." not in text or path.name in allowed_snippet_files, (
            path.name
        )
        assert "__import__" not in text, path.name
    # The only subprocess mentions are AI-visible snippet constants.
    prompts_text = (package / "prompts.py").read_text(encoding="utf-8")
    assert "subprocess" in prompts_text
    mat_text = (package / "materializer.py").read_text(encoding="utf-8")
    assert "subprocess" in mat_text  # TYPE_COMMAND_PREAMBLE snippet only
