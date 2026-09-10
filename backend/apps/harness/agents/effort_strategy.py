"""Resolve model effort from an effort strategy and catalog efforts.

``inherit`` is handled by the caller (parent effort); ``fixed`` means the
effort is stored directly on the config row. Both return ``""`` here.
"""

from __future__ import annotations


def resolve_strategy_effort(catalog_efforts: list[str], strategy: str) -> str:
    """Return the catalog effort for *strategy* or ``""`` when not applicable."""
    efforts = [str(e).strip() for e in (catalog_efforts or []) if str(e).strip()]
    if not efforts:
        return ""
    if strategy == "lowest":
        return efforts[0]
    if strategy == "medium":
        return efforts[len(efforts) // 2]
    if strategy == "highest":
        return efforts[-1]
    return ""
