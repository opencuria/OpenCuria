"""Small persisted display projections for fast harness timelines.

Timeline patch preview contract: ``display.preview`` is at most four objects
``{type, oldNo, newNo, content}``, matching the frontend collapsed diff window;
the browser should render these lines directly and never parse full diffs here.
Question calls expose bounded ``display.question_rows`` so the timeline
need not send potentially large tool input/output fields.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: Part types whose full text travels with the lightweight timeline.
TIMELINE_TEXT_PART_TYPES = ("text", "steer")
DISPLAY_TEXT_LIMIT = 240
QUESTION_TEXT_LIMIT = 500
PATCH_PREVIEW_LINES = 4
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _short(value: object, limit: int = DISPLAY_TEXT_LIMIT) -> str:
    """Normalize and cap one-line display text."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def project_question_definitions(source: object) -> list[dict[str, Any]]:
    """Bound structured question fields for compact timeline/gate responses."""
    if not isinstance(source, list):
        return []
    rows = []
    for item in source[:8]:
        if not isinstance(item, dict):
            continue
        options = item.get("options", [])
        safe_options = []
        if isinstance(options, list):
            for option in options[:8]:
                if isinstance(option, dict):
                    safe_options.append(
                        {
                            "label": _short(
                                option.get("label", ""), QUESTION_TEXT_LIMIT
                            ),
                            "description": _short(
                                option.get("description", ""), QUESTION_TEXT_LIMIT
                            ),
                        }
                    )
                elif isinstance(option, str):
                    safe_options.append(
                        {
                            "label": _short(option, QUESTION_TEXT_LIMIT),
                            "description": "",
                        }
                    )
        rows.append(
            {
                "header": _short(item.get("header", ""), QUESTION_TEXT_LIMIT),
                "question": _short(item.get("question", ""), QUESTION_TEXT_LIMIT),
                "options": safe_options,
                "multiple": bool(item.get("multiple", False)),
            }
        )
    return rows


def _question_rows(
    args: dict[str, Any], meta: dict[str, Any], output: str
) -> list[dict[str, Any]]:
    """Project structured questions and their answers safely."""
    arguments = _json_object(args.get("arguments"))
    source = meta.get("questions") or arguments.get("questions")
    definitions = project_question_definitions(source)
    result = _json_object(output)
    answers = meta.get("answers")
    if not isinstance(answers, list):
        answers = result.get("answers")
    if not isinstance(answers, list):
        answers = []

    rows: list[dict[str, Any]] = []
    for index, definition in enumerate(definitions):
        rows.append(
            {
                **definition,
                "answer": _short(
                    answers[index] if index < len(answers) else "",
                    QUESTION_TEXT_LIMIT,
                ),
            }
        )

    return rows


def _patch_projection(output: str) -> tuple[list[dict[str, Any]], int, int]:
    """Count a diff once, retaining only the bounded collapsed-window candidates."""
    leading: list[dict[str, Any]] = []
    window: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    old_no = new_no = 1
    additions = deletions = line_count = 0
    first_change = last_change = -1
    cursor = 0
    while cursor < len(output):
        # StringIO copies the whole diff. Slice one physical line at a time,
        # retaining the existing LF/CRLF handling, including bare internal CRs.
        end = output.find("\n", cursor)
        if end < 0:
            end = len(output)
        raw = output[cursor:end].rstrip("\r")
        cursor = end + 1
        if not raw or raw.startswith(("--- ", "+++ ", "\\")):
            continue
        if raw.startswith("@@ "):
            match = _HUNK_RE.match(raw)
            if match:
                old_no, new_no = int(match.group(1)), int(match.group(3))
            continue
        prefix = raw[0]
        if prefix == "+":
            kind, line_old, line_new = "add", None, new_no
            new_no += 1
            additions += 1
        elif prefix == "-":
            kind, line_old, line_new = "del", old_no, None
            old_no += 1
            deletions += 1
        elif prefix == " ":
            kind, line_old, line_new = "context", old_no, new_no
            old_no += 1
            new_no += 1
        else:
            continue

        index = line_count
        line_count += 1
        if kind != "context":
            if first_change < 0:
                first_change = index
                # Before the first change, every parsed line is context.
                if previous is not None:
                    window.append(previous)
            last_change = index
        if (
            index < PATCH_PREVIEW_LINES
            or first_change < 0
            or len(window) < PATCH_PREVIEW_LINES
        ):
            line = {
                "type": kind,
                "oldNo": line_old,
                "newNo": line_new,
                "content": raw[1:501],
            }
            if index < PATCH_PREVIEW_LINES:
                leading.append(line)
            if first_change < 0:
                previous = line
            elif len(window) < PATCH_PREVIEW_LINES:
                window.append(line)

    if line_count <= PATCH_PREVIEW_LINES or first_change < 0:
        return leading, additions, deletions
    if additions <= 1 and deletions <= 1:
        # Single-line changes end after their first trailing context line.
        start = max(0, first_change - 1)
        window = window[: last_change + 2 - start]
    return window, additions, deletions


def build_part_display(
    *,
    part_type: str,
    title: str = "",
    call_id: str = "",
    input_data: object = None,
    output: str = "",
    meta_data: object = None,
) -> dict[str, Any]:
    """Build a safe, bounded timeline projection from the full part fields."""
    args = input_data if isinstance(input_data, dict) else {}
    meta = meta_data if isinstance(meta_data, dict) else {}
    display: dict[str, Any] = {}

    if part_type == "tool":
        tool_name = str(args.get("tool") or meta.get("tool") or "")
        if not tool_name and title:
            tool_name = str(title).split(" ", 1)[0].strip().lower()
        display.update(tool=tool_name, summary=_short(title), step=meta.get("step"))
        if tool_name:
            display["tool"] = tool_name
        if tool_name == "question":
            rows = _question_rows(args, meta, output or "")
            display["question_rows"] = rows
    elif part_type == "reasoning":
        normalized = (output or "").replace("\r\n", "\n").replace("\r", "\n")
        final_line = normalized.strip().splitlines()[-1] if normalized.strip() else ""
        display["summary"] = _short(final_line)
        display["_reasoning_line"] = normalized.split("\n")[-1][:DISPLAY_TEXT_LIMIT]
        display["step"] = meta.get("step")
    elif part_type == "patch":
        preview, additions, deletions = _patch_projection(output or "")
        display.update(
            path=_short(meta.get("path") or "", 500),
            preview=preview,
            additions=additions,
            deletions=deletions,
            step=meta.get("step"),
            tool=_short(meta.get("tool") or "", 100),
        )
    elif part_type in ("step-start", "step-finish"):
        display.update(
            step=meta.get("step"),
            cost=meta.get("cost", 0),
            tokens=(
                meta.get("tokens", {})
                if isinstance(meta.get("tokens", {}), dict)
                else {}
            ),
        )
        if isinstance(meta.get("context"), dict):
            display["context"] = meta["context"]
    elif part_type == "subtask":
        display.update(
            summary=_short(title),
            subtask_id=str(meta.get("subtask_id") or ""),
            child_session_id=str(meta.get("child_session_id") or ""),
            agent=str(meta.get("agent") or ""),
            status=str(meta.get("status") or ""),
            step=meta.get("step"),
        )
    elif part_type == "compaction":
        display.update(
            summary=_short(output, 600),
            auto=bool(meta.get("auto", True)),
            overflow=bool(meta.get("overflow", False)),
        )
    elif part_type == "text":
        display["step"] = meta.get("step")
    elif part_type == "steer":
        display["steer_id"] = str(meta.get("steer_id") or call_id or "")
    else:
        display["summary"] = _short(title)
    return display


def append_part_display(
    display: object, *, part_type: str, delta: str
) -> dict[str, Any]:
    """Update the stream summary from the new delta, not the growing body."""
    result = dict(display) if isinstance(display, dict) else {}
    if part_type == "reasoning":
        previous_summary = str(result.get("summary", "") or "")
        previous_line = str(result.get("_reasoning_line", "") or "")
        addition = (delta or "").replace("\r\n", "\n").replace("\r", "\n")
        joined = f"{previous_line}{addition}"
        lines = joined.split("\n")
        current = lines[-1]
        completed = [line for line in lines[:-1] if line.strip()]
        summary_line = (
            current
            if current.strip()
            else (completed[-1] if completed else previous_summary)
        )
        result["summary"] = _short(summary_line)
        result["_reasoning_line"] = current[:DISPLAY_TEXT_LIMIT]
    return result


def timeline_part_payload(
    part: object, *, text_output: str | None = None
) -> dict[str, Any]:
    """Map a lightweight part row to the timeline API contract."""
    part_type = str(getattr(part, "type", ""))
    display = dict(getattr(part, "display", {}) or {})
    # Internal incremental reasoning state is persisted only to update summaries.
    display.pop("_reasoning_line", None)
    if not display:
        # A safe fallback for rows created by older/custom repositories: only
        # selected lightweight columns are read on the timeline query.
        display = build_part_display(
            part_type=part_type,
            title=str(getattr(part, "title", "") or ""),
            call_id=str(getattr(part, "call_id", "") or ""),
        )
    omitted_detail = part_type in {"tool", "patch", "reasoning", "compaction"}
    payload: dict[str, Any] = {
        "id": getattr(part, "id"),
        "message_id": getattr(part, "message_id"),
        "type": part_type,
        "state": getattr(part, "state", ""),
        "call_id": getattr(part, "call_id", "") or "",
        "title": getattr(part, "title", "") or "",
        "output": (
            "" if omitted_detail else (text_output if text_output is not None else "")
        ),
        "input": {"tool": display.get("tool", "")} if part_type == "tool" else {},
        "meta": {},
        "position": int(getattr(part, "position", 0) or 0),
        "message_position": int(getattr(part, "message_position", 0) or 0),
        "display": display,
        "detail_loaded": not omitted_detail,
    }
    if part_type == "tool":
        payload["tool"] = display.get("tool", "")
    return payload
