"""Deterministic namespacing for MCP tools (max 64 chars, fail-closed)."""

from __future__ import annotations

import hashlib
import re

#: Provider-compatible tool names: at most 64 chars, only these chars.
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

MAX_TOOL_NAME_LEN = 64


def slugify_tool_part(value: str, *, fallback: str) -> str:
    """Normalize one name part to ``[A-Za-z0-9_-]+`` (never empty)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("_") or fallback
    return cleaned


def mcp_tool_name(plugin_slug: str, server_slug: str, tool_name: str) -> str:
    """Return the namespaced tool name for one MCP tool.

    Shape: ``mcp_<plugin>_<server>_<tool-or-hash>``, truncated with a
    stable hash suffix so names stay unique within 64 chars.
    """
    plugin = slugify_tool_part(plugin_slug, fallback="plugin")
    server = slugify_tool_part(server_slug, fallback="server")
    tool = slugify_tool_part(tool_name, fallback="tool")
    digest = hashlib.sha256(tool_name.encode("utf-8")).hexdigest()[:8]
    candidate = f"mcp_{plugin}_{server}_{tool}"
    if len(candidate) <= MAX_TOOL_NAME_LEN and TOOL_NAME_RE.match(candidate):
        return candidate
    short_tool = slugify_tool_part(tool_name, fallback="tool")[:16].strip("_") or "tool"
    candidate = f"mcp_{plugin}_{server}_{short_tool}_{digest}"
    if len(candidate) > MAX_TOOL_NAME_LEN:
        # Trim plugin/server parts (keep tool+hash for uniqueness).
        keep = MAX_TOOL_NAME_LEN - len(f"mcp___{short_tool}_{digest}")
        head = f"{plugin}_{server}"[: max(1, keep)].strip("_") or "srv"
        candidate = f"mcp_{head}_{short_tool}_{digest}"
    candidate = candidate[:MAX_TOOL_NAME_LEN]
    if not TOOL_NAME_RE.match(candidate):  # pragma: no cover - defensive
        candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", candidate)[:MAX_TOOL_NAME_LEN]
    return candidate
