"""Tests for OpenCode-style tool output truncation."""

from __future__ import annotations

from pydantic import BaseModel, Field

from apps.harness.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from apps.harness.tools.files import READ_DEFAULT_LIMIT, READ_MAX_BYTES
from apps.harness.tools.shell import BASH_MAX_BYTES, GLOB_MAX_RESULTS, GREP_MAX_RESULTS
from apps.harness.tools.truncate import MAX_BYTES, MAX_LINES, truncate_tool_output


def test_truncate_constants_match_opencode() -> None:
    """Caps match OpenCode Truncate.output / grep-glob limits."""
    assert MAX_LINES == 2000
    assert MAX_BYTES == 50 * 1024
    assert BASH_MAX_BYTES == MAX_BYTES
    assert READ_MAX_BYTES == MAX_BYTES
    assert READ_DEFAULT_LIMIT == MAX_LINES
    assert GREP_MAX_RESULTS == 100
    assert GLOB_MAX_RESULTS == 100


def test_truncate_under_limit_is_unchanged() -> None:
    """Short output is returned as-is."""
    result = truncate_tool_output("short")
    assert result.content == "short"
    assert result.truncated is False


def test_truncate_empty_string() -> None:
    """Empty output does not add a truncation marker."""
    result = truncate_tool_output("")
    assert result.content == ""
    assert result.truncated is False


def test_truncate_caps_line_count() -> None:
    """More than MAX_LINES is clipped with a line marker."""
    text = "\n".join(f"line-{i}" for i in range(MAX_LINES + 50))
    result = truncate_tool_output(text)
    assert result.truncated is True
    assert "lines truncated" in result.content
    preview = result.content.split("\n\n...", 1)[0]
    assert len(preview.split("\n")) == MAX_LINES


def test_truncate_caps_bytes_on_huge_line() -> None:
    """A single oversized line is byte-clipped so grep minify hits cannot leak."""
    text = "x" * (MAX_BYTES + 4096)
    result = truncate_tool_output(text)
    assert result.truncated is True
    assert "bytes truncated" in result.content
    preview = result.content.split("\n\n...", 1)[0]
    assert len(preview.encode("utf-8")) <= MAX_BYTES


class _Args(BaseModel):
    """Args for the huge-output dummy tool."""

    text: str = Field(default="hi")


class _HugeTool(Tool):
    """Tool that returns an oversized line and claims it already truncated."""

    name = "huge"
    description = "Huge output."
    args_schema: type[BaseModel] = _Args
    permission_key = "read"

    async def execute(self, args: BaseModel, ctx: ToolContext) -> ToolResult:
        """Return a minify-sized line with truncated=True."""
        return ToolResult(output="y" * (MAX_BYTES + 2048), truncated=True)


async def test_registry_hard_caps_even_when_tool_sets_truncated(
    fake_accessor,
) -> None:
    """Registry clips output even if the tool already set truncated=True."""
    registry = ToolRegistry()
    registry.register(_HugeTool())
    result = await registry.execute(
        "huge",
        {},
        ToolContext(
            session_id="s",
            workspace_id="w",
            accessor=fake_accessor,
        ),
    )
    assert result.truncated is True
    preview = result.output.split("\n\n...", 1)[0]
    assert len(preview.encode("utf-8")) <= MAX_BYTES
    assert len(result.output) < MAX_BYTES + 2048


def test_truncate_tail_keeps_last_lines() -> None:
    """Tail keeps the final lines so build errors stay visible."""
    text = "\n".join(f"line-{i}" for i in range(MAX_LINES + 50))
    result = truncate_tool_output(text, direction="tail")
    assert result.truncated is True
    assert result.content.startswith("...50 lines truncated...")
    preview = result.content.split("...\n\n", 1)[1]
    assert len(preview.split("\n")) == MAX_LINES
    assert preview.split("\n")[-1] == f"line-{MAX_LINES + 49}"
    assert "line-0" not in preview.split("\n")


def test_truncate_head_marker_is_suffix() -> None:
    """Head keeps the first lines with a trailing marker."""
    text = "\n".join(f"line-{i}" for i in range(MAX_LINES + 10))
    result = truncate_tool_output(text, direction="head")
    assert result.truncated is True
    assert result.content.endswith("...10 lines truncated...")
    assert result.content.split("\n")[0] == "line-0"


def test_truncate_invalid_direction_rejected() -> None:
    """Unknown directions raise ValueError (auth-adjacent input guard)."""
    import pytest as _pytest

    with _pytest.raises(ValueError, match="Invalid direction"):
        truncate_tool_output("x" * 10, direction="middle")  # type: ignore[arg-type]


def test_truncate_tail_single_oversized_line_suffix() -> None:
    """A single oversized line keeps a tail suffix under the byte cap."""
    text = "x" * (MAX_BYTES + 4096)
    result = truncate_tool_output(text, direction="tail")
    assert result.truncated is True
    assert result.content.startswith("...") and "bytes truncated" in result.content
    preview = result.content.split("...\n\n", 1)[1]
    assert len(preview.encode("utf-8")) <= MAX_BYTES
    assert preview.endswith("x" * 100)


def test_truncate_head_oversized_second_line_byte_clip() -> None:
    """Head byte-window reports bytes and keeps a prefix preview."""
    text = "a" * 100 + "\n" + "b" * (MAX_BYTES + 100)
    result = truncate_tool_output(text)  # default head
    assert result.truncated is True
    assert "bytes truncated" in result.content
    preview = result.content.split("\n\n...", 1)[0]
    assert len(preview.encode("utf-8")) <= MAX_BYTES
    assert preview.startswith("a" * 100)
