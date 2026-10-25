"""Result shaping shared by all tool handlers: JSON safety, paging, clipping."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any


def json_safe(value: Any) -> Any:
    """Convert UUIDs, dates and decimals recursively into JSON values."""
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def timestamp(value: Any) -> Any:
    """Normalize a datetime or ISO string to second precision."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else value


def dump(data: Any) -> str:
    """Serialize a tool result compactly (readable, no ASCII escaping)."""
    return json.dumps(json_safe(data), ensure_ascii=False, separators=(",", ": "))


def compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose value is ``None``, ``""``, ``[]`` or ``{}``.

    Keeps responses short; ``False`` and ``0`` are meaningful and stay.
    """
    return {
        key: value
        for key, value in data.items()
        if value is not None and value != "" and value != [] and value != {}
    }


def paginate(
    items: list[Any], *, limit: int, offset: int
) -> tuple[list[Any], dict[str, Any]]:
    """Return one page of *items* and the paging metadata for the response."""
    total = len(items)
    page = items[offset : offset + limit]
    has_more = offset + len(page) < total
    meta: dict[str, Any] = {"total": total, "returned": len(page), "has_more": has_more}
    if has_more:
        meta["next_offset"] = offset + len(page)
    return page, meta


def clip(text: str | None, limit: int) -> str:
    """Truncate *text* to *limit* characters with an explicit marker."""
    value = text or ""
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return f"{value[:limit]}\n… [truncated {omitted} more characters]"


def preview(text: str | None, limit: int = 160) -> str:
    """Return a single-line preview of *text*."""
    line = " ".join((text or "").split())
    return line if len(line) <= limit else f"{line[: limit - 1]}…"
