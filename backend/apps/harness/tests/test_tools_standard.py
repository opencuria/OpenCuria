"""Tests for the standard file/shell/search tools."""

from __future__ import annotations

import base64

import pytest

from apps.harness.access.base import ExecResult
from apps.harness.access.runner_accessor import RunnerAccessorError
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import (
    BashTool,
    EditTool,
    GlobTool,
    GrepTool,
    ListTool,
    ReadTool,
    TaskTool,
    TodoWriteTool,
    WebfetchTool,
    WriteTool,
    default_tool_registry,
)
from apps.harness.tools.base import ToolContext, ToolError
from apps.harness.tools.files import ReadArgs
from apps.harness.tools.shell import truncate_output
from apps.harness.tools.todos import InMemoryTodoRepository


def _ctx(accessor: FakeAccessor) -> ToolContext:
    return ToolContext(session_id="sess-1", workspace_id="ws-1", accessor=accessor)


async def test_default_registry_has_standard_tools_plus_webfetch() -> None:
    """The default registry exposes all standard tools."""
    registry = default_tool_registry()
    assert registry.names() == [
        "read",
        "write",
        "edit",
        "bash",
        "glob",
        "grep",
        "list",
        "todowrite",
        "question",
        "task",
        "webfetch",
        "process_start",
        "process_list",
        "process_get",
        "process_stop",
    ]


async def test_read_happy_path(fake_accessor) -> None:
    """Read returns file text with metadata."""
    tool = ReadTool()
    result = await tool.execute({"path": "a.txt"}, _ctx(fake_accessor))
    assert result.output == "hello\nworld"
    assert result.metadata["lines"] == 2
    assert tool.title(ReadArgs(path="a.txt")) == "Read a.txt"


async def test_read_line_range(fake_accessor) -> None:
    """Offset/limit slice the file lines."""
    tool = ReadTool()
    result = await tool.execute(
        {"path": "/workspace/a.txt", "offset": 1, "limit": 1},
        _ctx(fake_accessor),
    )
    assert result.output == "world"


async def test_read_rejects_binary() -> None:
    """NUL bytes mark a file as binary and are rejected."""
    tool = ReadTool()
    accessor = FakeAccessor(files={"/workspace/b.bin": b"a\x00b"})
    with pytest.raises(ToolError, match="binary"):
        await tool.execute({"path": "/workspace/b.bin"}, _ctx(accessor))


async def test_read_paginates_large_file_instead_of_rejecting() -> None:
    """Files over the default page size return a continue hint, not an error."""
    tool = ReadTool()
    lines = [f"line-{i}" for i in range(3000)]
    accessor = FakeAccessor(
        files={"/workspace/big.py": ("\n".join(lines)).encode()}
    )
    result = await tool.execute({"path": "/workspace/big.py"}, _ctx(accessor))
    assert "File too large" not in result.output
    assert "Use offset=" in result.output
    assert result.truncated is True
    assert result.output.startswith("line-0\n")
    next_offset = result.metadata["next_offset"]
    assert isinstance(next_offset, int) and next_offset > 0
    page2 = await tool.execute(
        {"path": "/workspace/big.py", "offset": next_offset},
        _ctx(accessor),
    )
    assert f"line-{next_offset}" in page2.output
    assert "line-0\n" not in page2.output


async def test_read_caps_page_bytes_with_continue_hint() -> None:
    """A byte-heavy page is capped at 50 KB, not rejected."""
    tool = ReadTool()
    chunk = "x" * 2000
    body = "\n".join(chunk for _ in range(30))
    accessor = FakeAccessor(files={"/workspace/wide.py": body.encode()})
    result = await tool.execute({"path": "/workspace/wide.py"}, _ctx(accessor))
    assert "File too large" not in result.output
    assert "Output capped at 50 KB" in result.output
    assert "Use offset=" in result.output
    preview = result.output.split("\n\n(", 1)[0]
    assert len(preview.encode("utf-8")) <= 50 * 1024


async def test_read_truncates_long_lines() -> None:
    """Lines longer than 2000 characters are clipped with a marker."""
    tool = ReadTool()
    accessor = FakeAccessor(
        files={"/workspace/min.js": ("y" * 5000).encode()}
    )
    result = await tool.execute({"path": "/workspace/min.js"}, _ctx(accessor))
    assert "line truncated to 2000 chars" in result.output
    assert "y" * 2500 not in result.output.split("...", 1)[0]


async def test_read_offset_out_of_range() -> None:
    """Offsets past the last line raise ToolError."""
    tool = ReadTool()
    accessor = FakeAccessor(files={"/workspace/a.txt": b"hello\nworld"})
    with pytest.raises(ToolError, match="out of range"):
        await tool.execute(
            {"path": "/workspace/a.txt", "offset": 5}, _ctx(accessor)
        )


async def test_read_sandbox_violation_rejected(fake_accessor) -> None:
    """Paths outside /workspace raise ValueError (sandbox)."""
    with pytest.raises(ValueError, match="under /workspace"):
        await ReadTool().execute({"path": "/etc/passwd"}, _ctx(fake_accessor))


async def test_read_runner_error_propagates() -> None:
    """Runner failures surface as ToolError."""
    tool = ReadTool()
    accessor = FakeAccessor(error=RunnerAccessorError("read_file failed"))
    with pytest.raises(ToolError, match="read_file failed"):
        await tool.execute({"path": "a.txt"}, _ctx(accessor))


async def test_write_happy_path(fake_accessor) -> None:
    """Write stores content through the accessor."""
    result = await WriteTool().execute(
        {"path": "new.txt", "content": "hi"}, _ctx(fake_accessor)
    )
    assert fake_accessor.files["/workspace/new.txt"] == b"hi"
    assert "Wrote" in result.output


async def test_write_sandbox_violation_rejected(fake_accessor) -> None:
    """Write outside /workspace is rejected before any runner call."""
    with pytest.raises(ValueError, match="under /workspace"):
        await WriteTool().execute(
            {"path": "../evil.txt", "content": "x"}, _ctx(fake_accessor)
        )
    assert fake_accessor.written == {}


async def test_write_runner_error_propagates() -> None:
    """Runner write failures surface as ToolError."""
    accessor = FakeAccessor(error=RunnerAccessorError("write failed"))
    with pytest.raises(ToolError, match="write failed"):
        await WriteTool().execute({"path": "a.txt", "content": "x"}, _ctx(accessor))


async def test_edit_happy_path(fake_accessor) -> None:
    """Exact single replacement rewrites the file."""
    result = await EditTool().execute(
        {
            "path": "a.txt",
            "old_string": "world",
            "new_string": "there",
        },
        _ctx(fake_accessor),
    )
    assert fake_accessor.files["/workspace/a.txt"] == b"hello\nthere\n"
    assert "1" in result.output


async def test_edit_no_match_fails(fake_accessor) -> None:
    """Missing old_string raises a ToolError."""
    with pytest.raises(ToolError, match="not found"):
        await EditTool().execute(
            {"path": "a.txt", "old_string": "zzz", "new_string": "y"},
            _ctx(fake_accessor),
        )


async def test_edit_ambiguous_without_replace_all() -> None:
    """Multiple hits require replace_all=true."""
    accessor = FakeAccessor(files={"/workspace/a.txt": b"a a a"})
    with pytest.raises(ToolError, match="replace_all"):
        await EditTool().execute(
            {"path": "a.txt", "old_string": "a", "new_string": "b"},
            _ctx(accessor),
        )
    result = await EditTool().execute(
        {
            "path": "a.txt",
            "old_string": "a",
            "new_string": "b",
            "replace_all": True,
        },
        _ctx(accessor),
    )
    assert accessor.files["/workspace/a.txt"] == b"b b b"
    assert "3" in result.output


async def test_edit_sandbox_violation_rejected(fake_accessor) -> None:
    """Edit outside /workspace is rejected."""
    with pytest.raises(ValueError, match="under /workspace"):
        await EditTool().execute(
            {"path": "/etc/x", "old_string": "a", "new_string": "b"},
            _ctx(fake_accessor),
        )


async def test_bash_happy_path() -> None:
    """Bash returns combined output on exit 0."""
    accessor = FakeAccessor(
        exec_result=ExecResult(exit_code=0, stdout="out", stderr="err")
    )
    result = await BashTool().execute({"command": "echo hi"}, _ctx(accessor))
    assert result.output == "out\nerr"
    assert result.metadata["exit_code"] == 0


async def test_bash_nonzero_raises() -> None:
    """Non-zero exits raise ToolError with the output attached."""
    accessor = FakeAccessor(exec_result=ExecResult(exit_code=2, stdout="o", stderr="e"))
    with pytest.raises(ToolError, match="exit.*2"):
        await BashTool().execute({"command": "false"}, _ctx(accessor))


async def test_bash_timeout_propagates() -> None:
    """Accessor timeouts surface as ToolError."""
    accessor = FakeAccessor(error=TimeoutError("slow"))
    with pytest.raises(ToolError, match="timed out"):
        await BashTool().execute({"command": "sleep 9"}, _ctx(accessor))


async def test_bash_runner_error_propagates() -> None:
    """Runner errors surface as ToolError."""
    accessor = FakeAccessor(error=RunnerAccessorError("exec failed"))
    with pytest.raises(ToolError, match="exec failed"):
        await BashTool().execute({"command": "ls"}, _ctx(accessor))


def test_truncate_output_caps_lines_and_bytes() -> None:
    """Truncation keeps output within OpenCode-style limits."""
    text = "\n".join(f"line-{i}" for i in range(3000))
    clipped, truncated = truncate_output(text)
    assert truncated is True
    assert len(clipped.splitlines()) == 2000
    clipped2, truncated2 = truncate_output("short")
    assert (clipped2, truncated2) == ("short", False)


async def test_glob_happy_path(fake_accessor) -> None:
    """Glob lists matching files via the fake find output."""
    result = await GlobTool().execute({"pattern": "**/*.txt"}, _ctx(fake_accessor))
    assert "/workspace/a.txt" in result.output


async def test_glob_sandbox_violation_rejected(fake_accessor) -> None:
    """Glob outside /workspace is rejected."""
    with pytest.raises(ValueError, match="under /workspace"):
        await GlobTool().execute({"pattern": "**", "path": "/etc"}, _ctx(fake_accessor))


async def test_grep_happy_path() -> None:
    """Grep returns runner search lines."""
    accessor = FakeAccessor(
        exec_result=ExecResult(exit_code=0, stdout="/workspace/a.txt:1:hello")
    )
    result = await GrepTool().execute({"pattern": "hello"}, _ctx(accessor))
    assert "hello" in result.output


async def test_grep_runner_error_propagates() -> None:
    """Runner failures surface as ToolError."""
    accessor = FakeAccessor(error=RunnerAccessorError("grep failed"))
    with pytest.raises(ToolError, match="grep failed"):
        await GrepTool().execute({"pattern": "x"}, _ctx(accessor))


async def test_list_happy_path(fake_accessor) -> None:
    """List returns directory entries."""
    result = await ListTool().execute({"path": "/workspace"}, _ctx(fake_accessor))
    assert "file a.txt" in result.output
    assert result.metadata["count"] == 1


async def test_list_sandbox_violation_rejected(fake_accessor) -> None:
    """List outside /workspace is rejected."""
    with pytest.raises(ValueError, match="under /workspace"):
        await ListTool().execute({"path": "/etc"}, _ctx(fake_accessor))


async def test_todowrite_happy_path(fake_accessor) -> None:
    """Todos validate and persist behind the repository seam."""
    repo = InMemoryTodoRepository()
    tool = TodoWriteTool(repository=repo)
    result = await tool.execute(
        {
            "todos": [
                {"content": "first", "status": "in_progress"},
                {"content": "second", "status": "pending"},
            ]
        },
        _ctx(fake_accessor),
    )
    assert "[in_progress] first" in result.output
    stored = repo.list("sess-1")
    assert [item.content for item in stored.items] == ["first", "second"]
    assert stored.items[0].order == 0


async def test_todowrite_invalid_status_rejected(fake_accessor) -> None:
    """Unknown statuses raise a ToolError."""
    tool = TodoWriteTool(repository=InMemoryTodoRepository())
    with pytest.raises(ToolError, match="Invalid status"):
        await tool.execute(
            {"todos": [{"content": "x", "status": "bogus"}]},
            _ctx(fake_accessor),
        )


async def test_task_tool_without_wiring_rejected(fake_accessor) -> None:
    """TaskTool without provider/registry in ctx fails loudly (no wiring)."""
    ctx = _ctx(fake_accessor)
    with pytest.raises(ToolError, match="not wired"):
        await TaskTool().execute(
            {"description": "d", "prompt": "p"}, ctx
        )


async def test_webfetch_rejects_non_https(fake_accessor) -> None:
    """Only https:// URLs are fetched."""
    with pytest.raises(ToolError, match="https"):
        await WebfetchTool().execute({"url": "http://example.com"}, _ctx(fake_accessor))


async def test_read_missing_file_is_tool_error(fake_accessor) -> None:
    """Unknown files raise ToolError (runner 404 propagated)."""
    with pytest.raises(ToolError, match="not found"):
        await ReadTool().execute({"path": "missing.txt"}, _ctx(fake_accessor))


def test_base64_roundtrip_helper() -> None:
    """Sanity: runner file payloads survive base64 transport."""
    assert base64.b64decode(base64.b64encode(b"hi")) == b"hi"
