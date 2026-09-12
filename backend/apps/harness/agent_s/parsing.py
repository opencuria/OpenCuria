"""Response-parsing quirks (verbatim Agent-S behaviour).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.
"""

from __future__ import annotations

import re

__all__ = [
    "split_thinking_response",
    "parse_code_from_string",
    "extract_agent_functions",
]


def split_thinking_response(full_response: str) -> tuple[str, str]:
    """Split ``<thoughts>``/``<answer>`` exactly like Agent-S.

    Quirk: uses ``split(tag)[-1].split(close)[0]`` so missing tags yield the
    *whole* response as both answer and thoughts (never raises; on a truly
    unexpected exception falls back to ``(full_response, "")``).
    """
    try:
        thoughts = full_response.split("<thoughts>")[-1].split("</thoughts>")[0].strip()
        answer = full_response.split("<answer>")[-1].split("</answer>")[0].strip()
        return answer, thoughts
    except Exception:
        return full_response, ""


# Matches ```code``` and ```python code``` (non-greedy); language token is
# ``(?:\\w+\\s+)?`` so e.g. "```python\\n..." (no trailing space) matches with
# the language folded into the capture — hence Agent-S returns the *last*
# block including any language prefix quirks downstream.
_CODE_PATTERN = re.compile(r"```(?:\w+\s+)?(.*?)```", re.DOTALL)


def parse_code_from_string(input_string) -> str:
    """Return the last fenced code snippet (``""`` when none)."""
    input_string = input_string.strip()
    matches = _CODE_PATTERN.findall(input_string)
    if len(matches) == 0:
        return ""
    return matches[-1]


_AGENT_FN_PATTERN = re.compile(r"(agent\.\w+\(\s*.*\))")


def extract_agent_functions(code) -> list:
    """Extract ``agent.xxx(...)`` call strings (greedy ``.*`` quirk)."""
    return _AGENT_FN_PATTERN.findall(code)
