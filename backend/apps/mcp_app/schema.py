"""Compact builders for strict JSON Schema tool inputs.

Every object rejects unknown properties so a mistyped argument fails with a
clear validation message instead of being silently ignored.
"""

from __future__ import annotations

from typing import Any


def obj(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return a closed object schema."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


def string(description: str, **extra: Any) -> dict[str, Any]:
    """Return a string property."""
    return {"type": "string", "description": description, **extra}


def integer(description: str, **extra: Any) -> dict[str, Any]:
    """Return an integer property (pass ``minimum``/``maximum``/``default``)."""
    return {"type": "integer", "description": description, **extra}


def boolean(description: str, **extra: Any) -> dict[str, Any]:
    """Return a boolean property."""
    return {"type": "boolean", "description": description, **extra}


def enum(values: list[str], description: str, **extra: Any) -> dict[str, Any]:
    """Return a string property restricted to *values*."""
    return {"type": "string", "enum": values, "description": description, **extra}


def array(items: dict[str, Any], description: str, **extra: Any) -> dict[str, Any]:
    """Return an array property."""
    return {"type": "array", "items": items, "description": description, **extra}


def string_list(description: str, **extra: Any) -> dict[str, Any]:
    """Return an array of non-empty strings."""
    return array({"type": "string", "minLength": 1}, description, **extra)


def limit(default: int, maximum: int) -> dict[str, Any]:
    """Return the shared page-size property."""
    return integer(
        f"Maximum number of items to return (default {default}).",
        minimum=1,
        maximum=maximum,
        default=default,
    )


OFFSET = integer(
    "Number of items to skip, for paging (use next_offset from the previous response).",
    minimum=0,
    default=0,
)

WAIT_SECONDS = integer(
    "Seconds to wait for the agent to finish its turn or ask for input before "
    "returning (0 = return immediately, max 120). Use 30-90 to get the reply in "
    "the same call; if the agent is still working afterwards, call chat_read with "
    "wait_seconds again.",
    minimum=0,
    maximum=120,
    default=0,
)
