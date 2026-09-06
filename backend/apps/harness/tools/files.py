"""Standard harness tools: read, write, edit."""

from __future__ import annotations

import difflib

from pydantic import BaseModel, Field

from ..access.base import sanitize_harness_path
from ..access.runner_accessor import RunnerAccessorError
from .base import Tool, ToolContext, ToolError, ToolResult
from .truncate import MAX_BYTES, MAX_LINES

# Output page cap sent to the model (OpenCode ``MAX_BYTES``).
READ_MAX_BYTES = MAX_BYTES

# Bytes fetched from the runner so later pages can still be sliced.
READ_FETCH_MAX_BYTES = 5 * 1024 * 1024

# Default page size, mirroring OpenCode ``DEFAULT_READ_LIMIT``.
READ_DEFAULT_LIMIT = MAX_LINES

# OpenCode ``MAX_LINE_LENGTH`` — minify/lockfile lines stay bounded.
READ_MAX_LINE_LENGTH = 2000
_READ_LINE_SUFFIX = f"... (line truncated to {READ_MAX_LINE_LENGTH} chars)"
_READ_FOOTER_RESERVE = 256

# Binary detection: NUL byte in the probed prefix means "not text".
_BINARY_PROBE_BYTES = 4096


def _is_binary(content: bytes) -> bool:
    """Return True when *content* looks like a binary file."""
    return b"\x00" in content[:_BINARY_PROBE_BYTES]


def _clip_line(line: str) -> str:
    """Truncate one line to OpenCode's per-line character cap."""
    if len(line) <= READ_MAX_LINE_LENGTH:
        return line
    return f"{line[:READ_MAX_LINE_LENGTH]}{_READ_LINE_SUFFIX}"


def paginate_read(
    lines: list[str],
    *,
    offset: int,
    limit: int,
    max_bytes: int = READ_MAX_BYTES,
) -> tuple[list[str], bool, bool]:
    """Return a page of *lines* capped by *limit* and *max_bytes*.

    The third flag is True when the byte budget stopped the page early
    (OpenCode ``cut``), as opposed to only the line limit.
    """
    if offset < 0:
        offset = 0
    budget = max(1, max_bytes - _READ_FOOTER_RESERVE)
    raw: list[str] = []
    bytes_used = 0
    cut = False
    end = min(len(lines), offset + limit)
    for index in range(offset, end):
        line = _clip_line(lines[index])
        size = len(line.encode("utf-8")) + (1 if raw else 0)
        if bytes_used + size > budget:
            cut = True
            break
        raw.append(line)
        bytes_used += size
    more = cut or (offset + len(raw) < len(lines))
    return raw, more, cut


def _patch_metadata(
    path: str,
    *,
    old_text: str,
    new_text: str,
) -> dict[str, str]:
    """Build unified-diff patch metadata for write/edit tools."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path.lstrip('/')}",
        tofile=f"b/{path.lstrip('/')}",
        lineterm="",
    )
    unified = "\n".join(diff_lines)
    if not unified and old_text != new_text:
        unified = f"--- a/{path}\n+++ b/{path}\n(content changed)"
    return {
        "path": path,
        "old_content": old_text,
        "new_content": new_text,
        "unified_diff": unified,
    }


class ReadArgs(BaseModel):
    """Arguments for the read tool."""

    path: str = Field(description="Workspace-relative or /workspace path.")
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "First line to return (0-based). "
            "Use to continue a large file."
        ),
    )
    limit: int = Field(
        default=READ_DEFAULT_LIMIT,
        gt=0,
        description="Max lines to return (defaults to 2000).",
    )


class WriteArgs(BaseModel):
    """Arguments for the write tool."""

    path: str = Field(description="Workspace-relative or /workspace path.")
    content: str = Field(description="Full file content (replaces file).")


class EditArgs(BaseModel):
    """Arguments for the edit tool."""

    path: str = Field(description="Workspace-relative or /workspace path.")
    old_string: str = Field(description="Exact string to replace.")
    new_string: str = Field(default="", description="Replacement string.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class ReadTool(Tool):
    """Read a text file with an optional line range."""

    name = "read"
    description = (
        "Read a text file from the workspace. Returns up to 2000 lines "
        "(and at most 50 KB) from offset. Use offset to continue in large "
        "files. Rejects binary files. Lines longer than 2000 characters "
        "are truncated."
    )
    args_schema: type[BaseModel] = ReadArgs
    permission_key = "read"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a read invocation."""
        assert isinstance(args, ReadArgs)
        return f"Read {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Read a file via the workspace accessor, paginated like OpenCode."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ReadArgs)
        args = validated
        safe_path = sanitize_harness_path(args.path)
        try:
            stored = await ctx.accessor.read_file(
                safe_path, max_size=READ_FETCH_MAX_BYTES
            )
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        content = stored.content
        if _is_binary(content):
            raise ToolError(
                f"Refusing to read binary file: {args.path}",
                tool=self.name,
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(
                f"File is not valid UTF-8: {args.path}",
                tool=self.name,
            ) from exc
        lines = text.splitlines()
        if args.offset > 0 and args.offset >= len(lines):
            raise ToolError(
                f"Offset {args.offset} is out of range for this file "
                f"({len(lines)} lines): {args.path}",
                tool=self.name,
            )
        page, more, cut = paginate_read(
            lines, offset=args.offset, limit=args.limit
        )
        output = "\n".join(page)
        last = args.offset + len(page)
        next_offset = last
        if cut:
            output += (
                f"\n\n(Output capped at {READ_MAX_BYTES // 1024} KB. "
                f"Showing lines {args.offset}-{last - 1}. "
                f"Use offset={next_offset} to continue.)"
            )
        elif more:
            output += (
                f"\n\n(Showing lines {args.offset}-{last - 1} of "
                f"{len(lines)}. Use offset={next_offset} to continue.)"
            )
        elif stored.truncated:
            output += (
                f"\n\n(Fetched the first {len(content)} bytes; the file "
                f"continues on disk. Use Grep or raise offset to inspect "
                "later sections.)"
            )
        return ToolResult(
            output=output,
            truncated=more or cut or stored.truncated,
            metadata={
                "path": safe_path,
                "lines": len(lines),
                "size": stored.size or len(content),
                "offset": args.offset,
                "next_offset": next_offset if (more or cut) else None,
            },
        )


class WriteTool(Tool):
    """Create or overwrite a file with the given content."""

    name = "write"
    description = (
        "Create or overwrite a file with the given content. Parent "
        "directories are created implicitly by the runner."
    )
    args_schema: type[BaseModel] = WriteArgs
    permission_key = "edit"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a write invocation."""
        assert isinstance(args, WriteArgs)
        return f"Write {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Write file content via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, WriteArgs)
        args = validated
        safe_path = sanitize_harness_path(args.path)
        old_text = ""
        try:
            stored = await ctx.accessor.read_file(safe_path)
            if not _is_binary(stored.content):
                old_text = stored.content.decode("utf-8", errors="replace")
        except RunnerAccessorError:
            old_text = ""
        try:
            await ctx.accessor.write_file(safe_path, args.content.encode("utf-8"))
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        patch = _patch_metadata(safe_path, old_text=old_text, new_text=args.content)
        return ToolResult(
            output=f"Wrote {len(args.content.encode('utf-8'))} bytes to {safe_path}",
            metadata=patch,
        )


class EditTool(Tool):
    """Exact string replacement inside a text file."""

    name = "edit"
    description = (
        "Replace an exact string in a text file. Fails unless the target "
        "occurs exactly once, unless replace_all is true."
    )
    args_schema: type[BaseModel] = EditArgs
    permission_key = "edit"

    def title(self, args: BaseModel) -> str:
        """Return a short title for an edit invocation."""
        assert isinstance(args, EditArgs)
        return f"Edit {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Apply an exact string replacement via the accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, EditArgs)
        args = validated
        if not args.old_string:
            raise ToolError("old_string must not be empty", tool=self.name)
        safe_path = sanitize_harness_path(args.path)
        try:
            stored = await ctx.accessor.read_file(safe_path)
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        if _is_binary(stored.content):
            raise ToolError(
                f"Refusing to edit binary file: {args.path}",
                tool=self.name,
            )
        try:
            text = stored.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(
                f"File is not valid UTF-8: {args.path}",
                tool=self.name,
            ) from exc
        occurrences = text.count(args.old_string)
        if occurrences == 0:
            raise ToolError(
                f"old_string not found in {args.path}",
                tool=self.name,
            )
        if occurrences > 1 and not args.replace_all:
            raise ToolError(
                f"old_string occurs {occurrences}x in {args.path}; "
                "set replace_all=true to replace all",
                tool=self.name,
            )
        if args.replace_all:
            updated = text.replace(args.old_string, args.new_string)
        else:
            updated = text.replace(args.old_string, args.new_string, 1)
        try:
            await ctx.accessor.write_file(safe_path, updated.encode("utf-8"))
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        patch = _patch_metadata(safe_path, old_text=text, new_text=updated)
        patch["replacements"] = occurrences if args.replace_all else 1
        return ToolResult(
            output=f"Replaced {occurrences if args.replace_all else 1} "
            f"occurrence(s) in {safe_path}",
            metadata=patch,
        )
