"""Golden tests: Agent-S3 prompt texts match the pinned upstream commit.

Strategy (CI-safe):

- Checked-in SHA-256/length expectations pin every verbatim upstream text
  (prompt constants, snippet constants, worker prompt, action signatures and
  docstrings), so these tests pass with *no* external checkout.
- When the pinned Agent-S checkout is present at /workspace/Agent-S *and*
  importable, an additional live-parity test compares byte-for-byte against
  it (with heavy third-party imports stubbed). That test skips otherwise.

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.
"""

from __future__ import annotations

import hashlib
import inspect
import os
import sys
import types

import pytest

from apps.harness.agent_s import prompts as prompts_mod
from apps.harness.agent_s.actions import AgentActionSurface
from apps.harness.agent_s.worker_prompt import build_worker_prompt

# Checked-in golden expectations, captured from the pinned upstream commit
# 3aa272d23d2994c7bbde1acbbe0ef8e8d06b8693. Any change to the verbatim texts
# (or to signature/docstring parity) fails loudly without needing /workspace/Agent-S.
GOLDEN_TEXTS = {
    "FORMATTING_FEEDBACK_PROMPT": (
        272,
        "7db2e0b111fdae608f9adaf2784954b37c0f79bef7f8a11fa520ed6274fa5e1a",
    ),
    "REFLECTION_ON_TRAJECTORY": (
        2348,
        "c55a351b30396a257f945cc1ee8b9352003fd906dffbae169df8019eaef05773",
    ),
    "PHRASE_TO_WORD_COORDS_PROMPT": (
        1070,
        "4debbaf65372059ba9771ed3e8c92002d5c20d4f6c8732be837191fb45a490bc",
    ),
    "CODE_AGENT_PROMPT": (
        5861,
        "ba1ff0f57cf8f23636a06cd0eae4da26feeaa9703ba3f824c2baf94f34dd2b8b",
    ),
    "CODE_SUMMARY_AGENT_PROMPT": (
        979,
        "367fb0982cd972680d33024c79cc41c7c341799ef56beabf48644b908182031e",
    ),
    "UBUNTU_APP_SETUP": (
        669,
        "5743bbf33d83b07bde6fa68c8e0cb25f0c4495baeffdcc85068c2429dabce3f4",
    ),
    "SET_CELL_VALUES_CMD": (
        4323,
        "bcb2128ed5abf48a1680e8a2ef337c6b7952c37b3a07ce17cad3f285a6602f5b",
    ),
    "WORKER_PROMPT_HEAD": (
        3855,
        "bc2c69e92c223d155fc5ca0224af0871e2605698c6a7fcb2222e40571cebdb7d",
    ),
    "WORKER_PROMPT_TAIL": (
        1973,
        "ae10db8f62c7c914c23f7c291d0d0ab0d12a5c1703bc1ad015a069c68aefab83",
    ),
}

GOLDEN_WORKER_PROMPT = (
    12911,
    "ea9ca3f58c376c8059aee08c9be1a90e67b93e5e7a001d8082bcd48757073c27",
)

# (name, str(signature)) pinned for all 15 AI-visible actions.
GOLDEN_SIGNATURES = [
    ("call_code_agent", "(self, task: str = None)"),
    (
        "click",
        "(self, element_description: str, num_clicks: int = 1, "
        "button_type: str = 'left', hold_keys: List = [])",
    ),
    ("done", "(self)"),
    (
        "drag_and_drop",
        "(self, starting_description: str, ending_description: str, "
        "hold_keys: List = [])",
    ),
    ("fail", "(self)"),
    (
        "highlight_text_span",
        "(self, starting_phrase: str, ending_phrase: str, button: str = 'left')",
    ),
    ("hold_and_press", "(self, hold_keys: List, press_keys: List)"),
    ("hotkey", "(self, keys: List)"),
    ("open", "(self, app_or_filename: str)"),
    ("save_to_knowledge", "(self, text: List[str])"),
    ("scroll", "(self, element_description: str, clicks: int, shift: bool = False)"),
    (
        "set_cell_values",
        "(self, cell_values: Dict[str, Any], app_name: str, sheet_name: str)",
    ),
    ("switch_applications", "(self, app_code)"),
    (
        "type",
        "(self, element_description: Optional[str] = None, text: str = '', "
        "overwrite: bool = False, enter: bool = False)",
    ),
    ("wait", "(self, time: float)"),
]

# SHA-256 (first 16 hex chars) of each action docstring, in dir() order.
GOLDEN_DOCSTRING_HASHES = [
    ("call_code_agent", "95937d5604660b65"),
    ("click", "628404f8643f9c4a"),
    ("done", "cf307d3c1719bb2b"),
    ("drag_and_drop", "bb331bf1cf04d5a3"),
    ("fail", "7ed0d963ca0f92a0"),
    ("highlight_text_span", "4941f6f29c294697"),
    ("hold_and_press", "550aa4cf60775b09"),
    ("hotkey", "d1b21949a919a31e"),
    ("open", "064f22a84c4b46d4"),
    ("save_to_knowledge", "ec40faa2ddf0616c"),
    ("scroll", "c84f7c342ee39d9d"),
    ("set_cell_values", "49396cbad06a5050"),
    ("switch_applications", "111bc43ce1dd3c09"),
    ("type", "fb9c3098a11a2cf3"),
    ("wait", "0c6d9c1007723cfe"),
]


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_verbatim_texts_match_checked_in_goldens():
    for name, (length, digest) in GOLDEN_TEXTS.items():
        text = getattr(prompts_mod, name)
        assert len(text) == length, name
        assert _sha256(text) == digest, name


def test_worker_prompt_matches_checked_in_golden():
    prompt = build_worker_prompt(AgentActionSurface, skipped_actions=[])
    assert len(prompt) == GOLDEN_WORKER_PROMPT[0]
    assert _sha256(prompt) == GOLDEN_WORKER_PROMPT[1]
    # The typo "alxl" in PHRASE_TO_WORD_COORDS_PROMPT is upstream verbatim.
    assert "alxl the text" in prompts_mod.PHRASE_TO_WORD_COORDS_PROMPT


def test_action_order_signatures_docstrings_match_checked_in_goldens():
    names = [
        n
        for n in dir(AgentActionSurface)
        if callable(getattr(AgentActionSurface, n))
        and hasattr(getattr(AgentActionSurface, n), "is_agent_action")
    ]
    assert names == [name for name, _ in GOLDEN_SIGNATURES]
    for name, expected_sig in GOLDEN_SIGNATURES:
        assert str(inspect.signature(getattr(AgentActionSurface, name))) == (
            expected_sig
        ), name
    for name, expected_hash in GOLDEN_DOCSTRING_HASHES:
        doc = getattr(AgentActionSurface, name).__doc__ or ""
        assert _sha256(doc)[:16] == expected_hash, name


def _try_load_reference():
    """Import the pinned Agent-S checkout if present; else return None.

    Never fails: returns None when /workspace/Agent-S is absent (normal in
    CI) or cannot be imported with stubs. Restores ``sys.modules`` and
    ``sys.path`` in ``finally`` so the stub modules (pytesseract, PIL,
    numpy, gui_agents, ...) never pollute the test process — a leftover
    stub ``numpy`` without ``isscalar`` breaks ``pytest.approx`` in other
    test files running in the same process.
    """
    ref_root = "/workspace/Agent-S"
    if not os.path.isdir(ref_root):
        return None
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    try:
        pt = types.ModuleType("pytesseract")
        pt.Output = types.SimpleNamespace(DICT="dict")
        pt.image_to_data = lambda *a, **k: {}
        sys.modules["pytesseract"] = pt
        pil = types.ModuleType("PIL")
        pil_img = types.ModuleType("PIL.Image")

        class _Img:
            @staticmethod
            def open(*a, **k):  # pragma: no cover
                raise RuntimeError("stub")

        pil.Image = pil_img
        pil_img.Image = _Img
        pil_img.open = _Img.open
        sys.modules["PIL"] = pil
        sys.modules["PIL.Image"] = pil_img
        np = types.ModuleType("numpy")
        np.ndarray = type("ndarray", (), {})
        sys.modules["numpy"] = np
        mllm = types.ModuleType("gui_agents.s3.core.mllm")

        class LMMAgent:
            def __init__(self, *a, **k):
                pass

        mllm.LMMAgent = LMMAgent
        sys.modules["gui_agents.s3.core.mllm"] = mllm
        ca = types.ModuleType("gui_agents.s3.agents.code_agent")

        class CodeAgent:
            def __init__(self, *a, **k):
                pass

        ca.CodeAgent = CodeAgent
        sys.modules["gui_agents.s3.agents.code_agent"] = ca
        if ref_root not in sys.path:
            sys.path.insert(0, ref_root)
        from gui_agents.s3.agents.grounding import (  # noqa: E402
            SET_CELL_VALUES_CMD,
            UBUNTU_APP_SETUP,
            OSWorldACI,
        )
        from gui_agents.s3.memory.procedural_memory import (  # noqa: E402
            PROCEDURAL_MEMORY,
        )

        return OSWorldACI, PROCEDURAL_MEMORY, UBUNTU_APP_SETUP, SET_CELL_VALUES_CMD
    except Exception:
        return None
    finally:
        for key in [k for k in sys.modules if k not in saved_modules]:
            del sys.modules[key]
        for key, mod in saved_modules.items():
            if sys.modules.get(key) is not mod:
                sys.modules[key] = mod
        sys.path[:] = saved_path


REF = _try_load_reference()

needs_reference = pytest.mark.skipif(
    REF is None, reason="pinned Agent-S checkout not available"
)


@needs_reference
def test_static_prompts_byte_identical_to_reference():
    ref_aci, pm, ubuntu_setup, set_cell_cmd = REF
    assert prompts_mod.FORMATTING_FEEDBACK_PROMPT == pm.FORMATTING_FEEDBACK_PROMPT
    assert prompts_mod.REFLECTION_ON_TRAJECTORY == pm.REFLECTION_ON_TRAJECTORY
    assert prompts_mod.PHRASE_TO_WORD_COORDS_PROMPT == pm.PHRASE_TO_WORD_COORDS_PROMPT
    assert prompts_mod.CODE_AGENT_PROMPT == pm.CODE_AGENT_PROMPT
    assert prompts_mod.CODE_SUMMARY_AGENT_PROMPT == pm.CODE_SUMMARY_AGENT_PROMPT
    assert prompts_mod.UBUNTU_APP_SETUP == ubuntu_setup
    assert prompts_mod.SET_CELL_VALUES_CMD == set_cell_cmd


@needs_reference
def test_worker_prompt_byte_identical_to_reference():
    ref_aci, pm, _, _ = REF
    expected = pm.construct_simple_worker_procedural_memory(ref_aci, skipped_actions=[])
    assert build_worker_prompt(AgentActionSurface, skipped_actions=[]) == expected
    for skipped in (["set_cell_values"], ["call_code_agent"]):
        expected = pm.construct_simple_worker_procedural_memory(
            ref_aci, skipped_actions=skipped
        )
        assert (
            build_worker_prompt(AgentActionSurface, skipped_actions=skipped) == expected
        )


@needs_reference
def test_action_signatures_docstrings_byte_identical_to_reference():
    ref_aci, _, _, _ = REF
    ref_names = [
        n
        for n in dir(ref_aci)
        if callable(getattr(ref_aci, n))
        and hasattr(getattr(ref_aci, n), "is_agent_action")
    ]
    our_names = [
        n
        for n in dir(AgentActionSurface)
        if callable(getattr(AgentActionSurface, n))
        and hasattr(getattr(AgentActionSurface, n), "is_agent_action")
    ]
    assert our_names == ref_names
    for name in ref_names:
        assert str(inspect.signature(getattr(AgentActionSurface, name))) == str(
            inspect.signature(getattr(ref_aci, name))
        )
        assert (
            getattr(AgentActionSurface, name).__doc__ == getattr(ref_aci, name).__doc__
        )


def test_claude_thinking_model_list():
    assert prompts_mod.CLAUDE_THINKING_MODELS == [
        "claude-opus-4-20250514",
        "claude-sonnet-4-20250514",
        "claude-3-7-sonnet-20250219",
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5-20251101",
    ]


def test_no_task_description_or_os_placeholders_left_unexpanded_in_prompts_module():
    # The stored templates keep placeholders; expansion happens per-turn.
    assert "TASK_DESCRIPTION" in prompts_mod.WORKER_PROMPT_HEAD
    assert "CURRENT_OS" in prompts_mod.WORKER_PROMPT_HEAD
