"""Standard harness tools: read, write, edit."""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..access.base import sanitize_harness_path
from ..access.runner_accessor import RunnerAccessorError
from .base import Tool, ToolContext, ToolError, ToolResult

# Files larger than this are rejected by ``read`` (binary-safety guard).
READ_MAX_BYTES = 256 * 1024

# Binary detection: NUL byte in the probed prefix means "not text".
_BINARY_PROBE_BYTES = 4096


def _is_binary(content: bytes) -> bool:
    """Return True when *content* looks like a binary file."""
    return b"\x00" in content[:_BINARY_PROBE_BYTES]


class ReadArgs(BaseModel):
    """Arguments for the read tool."""

    path: str = Field(description="Workspace-relative or /workspace path.")
    offset: int = Field(default=0, ge=0, description="First line (0-based).")
    limit: int | None = Field(default=None, gt=0, description="Max lines to return.")
    max_size: int = Field(default=READ_MAX_BYTES, gt=0)


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
        "Read a text file from the workspace. Returns UTF-8 text for an "
        "optional line range. Rejects binary files and oversized files."
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
        """Read a file via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ReadArgs)
        args = validated
        safe_path = sanitize_harness_path(args.path)
        try:
            stored = await ctx.accessor.read_file(safe_path, max_size=args.max_size)
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        content = stored.content
        if stored.truncated or len(content) > args.max_size:
            raise ToolError(
                f"File too large ({len(content)} bytes, "
                f"limit {args.max_size}): {args.path}",
                tool=self.name,
            )
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
        sliced = (
            lines[args.offset : args.offset + args.limit]
            if args.limit is not None
            else lines[args.offset :]
        )
        return ToolResult(
            output="\n".join(sliced),
            truncated=stored.truncated,
            metadata={
                "path": safe_path,
                "lines": len(lines),
                "size": len(content),
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
        try:
            await ctx.accessor.write_file(safe_path, args.content.encode("utf-8"))
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        return ToolResult(
            output=f"Wrote {len(args.content.encode('utf-8'))} bytes to {safe_path}",
            metadata={"path": safe_path},
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
        return ToolResult(
            output=f"Replaced {occurrences if args.replace_all else 1} "
            f"occurrence(s) in {safe_path}",
            metadata={"path": safe_path, "replacements": occurrences},
        )
