"""Worker-prompt builder (mirrors Agent-S ``dir()`` introspection).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Sequence

from . import prompts as _prompts


def iter_agent_action_names(action_cls: type) -> list[str]:
    """Return AI-visible action names in ``dir()`` order (as Agent-S does)."""
    return [
        name
        for name in dir(action_cls)
        if callable(getattr(action_cls, name))
        and hasattr(getattr(action_cls, name), "is_agent_action")
    ]


def render_action_block(action_fn) -> str:
    """Render one ``def ... '''doc'''`` block exactly like Agent-S."""
    signature = inspect.signature(action_fn)
    return (
        f"\n    def {action_fn.__name__}{signature}:\n"
        f"    '''{action_fn.__doc__}'''\n"
        "        "
    )


def build_worker_prompt(action_cls: type, skipped_actions: Sequence[str] = ()) -> str:
    """Build the worker system prompt (byte-identical to Agent-S).

    Mirrors ``PROCEDURAL_MEMORY.construct_simple_worker_procedural_memory``:
    head + one rendered block per non-skipped ``@agent_action`` in
    ``dir()`` order + tail, then ``.strip()``.
    """
    skipped = set(skipped_actions)
    procedural_memory = _prompts.WORKER_PROMPT_HEAD
    for attr_name in dir(action_cls):
        if attr_name in skipped:
            continue
        attr = getattr(action_cls, attr_name)
        if callable(attr) and hasattr(attr, "is_agent_action"):
            signature = inspect.signature(attr)
            procedural_memory += f"""
    def {attr_name}{signature}:
    '''{attr.__doc__}'''
        """
    procedural_memory += _prompts.WORKER_PROMPT_TAIL
    return procedural_memory.strip()


def default_skipped_actions(platform: str, has_code_execution: bool) -> list[str]:
    """Mirror ``Worker.reset`` skip logic.

    - Non-``linux`` platforms hide ``set_cell_values``.
    - Without an execution backend the code action is hidden entirely
      (Agent-S checks ``env``/``controller`` availability).
    """
    skipped: list[str] = []
    if platform != "linux":
        skipped.append("set_cell_values")
    if not has_code_execution:
        skipped.append("call_code_agent")
    return skipped


def apply_task_description(system_prompt: str, instruction: str) -> str:
    """Replace the ``TASK_DESCRIPTION`` placeholder (turn 0, exactly once)."""
    return system_prompt.replace("TASK_DESCRIPTION", instruction)


def apply_platform(system_prompt: str, platform: str) -> str:
    """Replace the ``CURRENT_OS`` placeholder (``Worker.reset`` behaviour)."""
    return system_prompt.replace("CURRENT_OS", platform)


def build_initial_system_prompt(
    action_cls: type,
    platform: str,
    skipped_actions: Iterable[str] | None = None,
) -> str:
    """Build the generator system prompt template (placeholders intact)."""
    if skipped_actions is None:
        skipped_actions = default_skipped_actions(platform, has_code_execution=True)
    return apply_platform(
        build_worker_prompt(action_cls, tuple(skipped_actions)), platform
    )
