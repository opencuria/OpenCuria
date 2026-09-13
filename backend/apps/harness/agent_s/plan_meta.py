"""Safe summary of Agent-S worker plans (no raw execution data).

The worker prompt asks for fixed sections (see
``WORKER_PROMPT_TAIL``)::

    (Previous action verification)
    ...
    (Screenshot Analysis)
    ...
    (Next Action)
    ...
    (Grounded Action)
    ```python
    agent.click(...)
    ```

This module parses that shape into small, human-readable, non-executable
metadata for the step/timeline UI (persisted on the ``agent``
``HarnessPart.meta`` as ``agent_meta`` and forwarded on the existing live
``delta.agent`` event). Only safe summaries are kept:

- ``verification``/``analysis``/``next_action``: plain text sections;
- ``action``: a human-readable one-line summary such as ``click "Save"``
  — never the raw ``exec_code``. For ``type`` actions the summary is
  just ``type`` (no quoted argument): typed text may carry secrets and
  must never be persisted in ``agent_meta``;
- ``action_kind``: the bare ``agent.<method>`` name when a fenced
  ``agent.*(...)`` call is present (``""`` otherwise).

Raw ``exec_code`` and materialized screen coordinates are never
persisted. Parsing is defensive: free-form or legacy plans yield empty
fields so the UI can fall back to the full plan text.
"""

from __future__ import annotations

import re
from typing import Any

#: Fixed worker-prompt sections, in order. Matching is tolerant
#: (case-insensitive, optional surrounding whitespace/parens).
_SECTION_TITLES = (
    "previous action verification",
    "screenshot analysis",
    "next action",
    "grounded action",
)

_META_KEYS = ("verification", "analysis", "next_action")

_FENCE_PATTERN = re.compile(r"```(?:python)?\s*(.*?)\s*```", re.DOTALL)
_AGENT_CALL_PATTERN = re.compile(
    r"agent\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)",
    re.DOTALL,
)
_STRING_ARG_PATTERN = re.compile(r""""([^"]{0,120})"|'([^']{0,120})""")


def _split_sections(plan: str) -> dict[str, str]:
    """Split *plan* into the fixed worker sections (defensive)."""
    sections: dict[str, str] = {}
    text = plan or ""
    if not text.strip():
        return sections
    heading = re.compile(
        r"\(\s*(previous action verification|screenshot analysis|"
        r"next action|grounded action)\s*\)",
        re.IGNORECASE,
    )
    matches = list(heading.finditer(text))
    if not matches:
        return sections
    for index, match in enumerate(matches):
        title = re.sub(r"\s+", " ", match.group(1)).strip().lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _summarize_action(section: str, plan: str) -> tuple[str, str]:
    """Return ``(action, action_kind)`` for a grounded-action section.

    Only the fenced snippet is inspected; the summary keeps the bare
    method name plus at most the first short quoted argument (an element
    description) — except for ``type`` actions, where no string argument
    is persisted at all (the typed text may carry secrets/passwords and
    the first quoted argument can be that text). Coordinates, flags, and
    code are dropped.
    """
    scope = section or plan or ""
    fence = _FENCE_PATTERN.search(scope)
    candidate = fence.group(1) if fence else scope
    call = _AGENT_CALL_PATTERN.search(candidate or "")
    if call is None and fence is None:
        call = _AGENT_CALL_PATTERN.search(plan or "")
    if call is None:
        return "", ""
    kind = call.group(1).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", kind):
        return "", ""
    args = call.group(2) or ""
    first: str | None = None
    if kind != "type":
        string_match = _STRING_ARG_PATTERN.search(args)
        if string_match is not None:
            first = string_match.group(1)
            if first is None:
                first = string_match.group(2)
            first = (first or "").strip()
            first = re.sub(r"\s+", " ", first)
    if kind in ("done", "fail"):
        return kind, kind
    if kind == "type":
        return "type", "type"
    if first:
        return f'{kind} "{first}"', kind
    return kind, kind


def parse_plan_meta(plan: str) -> dict[str, Any]:
    """Parse *plan* into safe step/timeline metadata.

    Always returns the keys ``verification``, ``analysis``,
    ``next_action``, ``action`` and ``action_kind`` (empty strings when
    the plan does not carry the fixed sections). Never raises and never
    includes raw code or coordinates.
    """
    meta: dict[str, Any] = {
        "verification": "",
        "analysis": "",
        "next_action": "",
        "action": "",
        "action_kind": "",
    }
    text = plan or ""
    if not text.strip():
        return meta
    try:
        sections = _split_sections(text)
        if not sections:
            # Free-form/legacy plans without the fixed worker sections
            # yield empty structured fields; the UI falls back to the
            # full plan text.
            return meta
        values = {
            "verification": sections.get("previous action verification", ""),
            "analysis": sections.get("screenshot analysis", ""),
            "next_action": sections.get("next action", ""),
        }
        for key in _META_KEYS:
            cleaned = re.sub(r"\s+", " ", values[key]).strip()
            meta[key] = cleaned[:2000]
        grounded = sections.get("grounded action", "")
        action, kind = _summarize_action(grounded, text)
        meta["action"] = action[:500]
        meta["action_kind"] = kind[:64]
        return meta
    except Exception:
        return dict(meta)


__all__ = ["parse_plan_meta"]
