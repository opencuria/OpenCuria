"""Standard harness tools: bash, glob, grep, list."""

from __future__ import annotations

import os
import re
import shlex

from pydantic import BaseModel, Field

from ..access.base import (
    BLOCKED_ENV_EXACT,
    BLOCKED_ENV_PREFIXES,
    HARNESS_WORKSPACE_ROOT,
    sanitize_harness_path,
    validate_harness_env,
)
from ..access.runner_accessor import RunnerAccessorError
from .base import Tool, ToolContext, ToolError, ToolResult
from .truncate import MAX_BYTES, MAX_LINES

__all__ = [
    "BashArgs",
    "BashTool",
    "GlobArgs",
    "GlobTool",
    "GrepArgs",
    "GrepTool",
    "ListArgs",
    "ListTool",
    "BLOCKED_ENV_EXACT",
    "BLOCKED_ENV_PREFIXES",
    "truncate_output",
    "detect_external_directory",
]

# Output truncation for bash, mirroring OpenCode's max_lines/max_bytes.
BASH_MAX_LINES = MAX_LINES
BASH_MAX_BYTES = MAX_BYTES
BASH_DEFAULT_TIMEOUT = 60.0

# Safety caps for search-style tools (OpenCode grep/glob limit).
GLOB_MAX_RESULTS = 100
GREP_MAX_RESULTS = 100

#: Max characters per grep output line (Pi GREP_MAX_LINE_LENGTH parity).
GREP_MAX_LINE_LENGTH = 500
_GREP_LINE_SUFFIX = "... [truncated]"


def truncate_output(text: str, direction: str = "tail") -> tuple[str, bool]:
    """Truncate *text* to BASH_MAX_LINES lines / BASH_MAX_BYTES bytes.

    ``direction="tail"`` keeps the last lines/bytes (build errors
    surface at the end); ``"head"`` keeps the first lines/bytes.
    """
    truncated = False
    lines = text.splitlines()
    if len(lines) > BASH_MAX_LINES:
        if direction == "tail":
            lines = lines[-BASH_MAX_LINES:]
        else:
            lines = lines[:BASH_MAX_LINES]
        truncated = True
    clipped = "\n".join(lines)
    encoded = clipped.encode("utf-8", errors="replace")
    if len(encoded) > BASH_MAX_BYTES:
        if direction == "tail":
            clipped = encoded[-BASH_MAX_BYTES:].decode("utf-8", errors="ignore")
        else:
            clipped = encoded[:BASH_MAX_BYTES].decode("utf-8", errors="ignore")
        truncated = True
    return clipped, truncated


#: Absolute paths that never count as external (device aliases).
SAFE_DEVICE_PATHS = frozenset(
    {
        "/dev/null",
        "/dev/stdin",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/zero",
        "/dev/urandom",
    }
)

#: Absolute-path token pattern inside shell command lines.
_EXTERNAL_PATH_RE = re.compile(r"(?<![\w.~-])/[\w.~-]+(?:/[\w.~-]+)*")


def _is_external_path(path: str) -> bool:
    """Return True when *path* escapes the /workspace sandbox."""
    if not path.startswith("/"):
        return False
    if path in SAFE_DEVICE_PATHS:
        return False
    if path == HARNESS_WORKSPACE_ROOT or path.startswith(
        HARNESS_WORKSPACE_ROOT + "/"
    ):
        normalized = os.path.normpath(path)
        return not (
            normalized == HARNESS_WORKSPACE_ROOT
            or normalized.startswith(HARNESS_WORKSPACE_ROOT + "/")
        )
    return True


def detect_external_directory(command: str) -> bool:
    """Return True when *command* references paths outside /workspace.

    Conservative: any absolute path outside ``/workspace`` counts,
    including quoted strings and ``/workspace/../..`` escapes (resolved
    via ``normpath``). Safe device aliases (``/dev/null`` et al.) and
    ``/workspace`` paths are ignored. Empty commands return False.
    """
    if not command or not command.strip():
        return False
    for match in _EXTERNAL_PATH_RE.finditer(command):
        if _is_external_path(match.group(0)):
            return True
    if ".." not in command:
        return False
    for match in _EXTERNAL_PATH_RE.finditer(command):
        candidate = match.group(0)
        if candidate.startswith(HARNESS_WORKSPACE_ROOT + "/"):
            if _is_external_path(candidate):
                return True
    return False


class BashArgs(BaseModel):
    """Arguments for the bash tool."""

    command: str = Field(description="Shell command to run.")
    workdir: str = Field(default=HARNESS_WORKSPACE_ROOT)
    timeout: float = Field(default=BASH_DEFAULT_TIMEOUT, gt=0, le=600)
    env: dict[str, str] = Field(default_factory=dict)


class GlobArgs(BaseModel):
    """Arguments for the glob tool."""

    pattern: str = Field(description="Glob pattern, e.g. '**/*.py'.")
    path: str = Field(default=HARNESS_WORKSPACE_ROOT)


class GrepArgs(BaseModel):
    """Arguments for the grep tool."""

    pattern: str = Field(description="Search pattern (regex for rg).")
    path: str = Field(default=HARNESS_WORKSPACE_ROOT)
    include: str | None = Field(default=None, description="Glob filter.")


class ListArgs(BaseModel):
    """Arguments for the list tool."""

    path: str = Field(default=HARNESS_WORKSPACE_ROOT)


class BashTool(Tool):
    """Run a shell command with timeout and truncated output."""

    name = "bash"
    description = (
        "Run a shell command in the workspace. Returns exit code, "
        "stdout, and stderr; non-zero exits raise a tool error. "
        "Default timeout is 60s (max 600s). Output is tail-truncated "
        "(last lines kept; full output spilled to a file when clipped). "
        "Independent bash calls in one step run in parallel."
    )
    args_schema: type[BaseModel] = BashArgs
    permission_key = "bash"
    truncate_direction = "tail"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a bash invocation."""
        assert isinstance(args, BashArgs)
        command = args.command.strip().splitlines()[0] if args.command else ""
        return f"$ {command[:80]}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Execute a shell command via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, BashArgs)
        args = validated
        if not args.command.strip():
            raise ToolError("command must not be empty", tool=self.name)
        workdir = sanitize_harness_path(args.workdir or ctx.directory)
        try:
            env = validate_harness_env(args.env or {})
        except ValueError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        try:
            result = await ctx.accessor.exec_wait(
                args.command,
                workdir=workdir,
                env=env,
                timeout=args.timeout,
            )
        except TimeoutError as exc:
            raise ToolError(
                f"Command timed out after {args.timeout:g}s: {exc} "
                "(retry with larger timeout)",
                tool=self.name,
            ) from exc
        except RunnerAccessorError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        combined = ""
        if result.stdout:
            combined += result.stdout
            if not combined.endswith("\n"):
                combined += "\n"
        if result.stderr:
            combined += result.stderr
        output, truncated = truncate_output(combined.rstrip("\n"))
        metadata = {
            "exit_code": result.exit_code,
            "workdir": workdir,
        }
        if result.exit_code != 0:
            raise ToolError(
                f"Command exited with code {result.exit_code}:\n{output}",
                tool=self.name,
            )
        return ToolResult(output=output, truncated=truncated, metadata=metadata)


class GlobTool(Tool):
    """Find files by glob pattern (rg --files, find fallback)."""

    name = "glob"
    description = (
        "Find files matching a glob pattern under a workspace path. "
        "Implemented via the workspace accessor (find on the runner)."
    )
    args_schema: type[BaseModel] = GlobArgs
    permission_key = "glob"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a glob invocation."""
        assert isinstance(args, GlobArgs)
        return f"Glob {args.pattern}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Search for files via rg --files (find fallback) on the runner."""
        validated = self.coerce_args(args)
        assert isinstance(validated, GlobArgs)
        args = validated
        base = sanitize_harness_path(args.path or ctx.directory)
        command = (
            "(command -v rg >/dev/null 2>&1 && "
            f"rg --files {shlex.quote(base)} "
            f"--glob {shlex.quote(args.pattern)} 2>/dev/null || "
            f"find {shlex.quote(base)} -type f -name "
            f"{shlex.quote(args.pattern.rsplit('/', 1)[-1])} 2>/dev/null) "
            f"| head -n {GLOB_MAX_RESULTS + 1}"
        )
        try:
            result = await ctx.accessor.exec_wait(
                command, workdir=HARNESS_WORKSPACE_ROOT, timeout=30.0
            )
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        if result.exit_code not in (0, 1):
            raise ToolError(
                f"glob failed with exit {result.exit_code}: {result.stderr.strip()}",
                tool=self.name,
            )
        paths = [line for line in result.stdout.splitlines() if line.strip()]
        truncated = len(paths) > GLOB_MAX_RESULTS
        paths = paths[:GLOB_MAX_RESULTS]
        return ToolResult(
            output="\n".join(paths),
            truncated=truncated,
            metadata={"pattern": args.pattern, "path": base},
        )


class GrepTool(Tool):
    """Search file contents with ripgrep (fallback: grep -r)."""

    name = "grep"
    description = (
        "Search file contents under a workspace path. Uses ripgrep when "
        "available (respects .gitignore by default), else grep -r."
    )
    args_schema: type[BaseModel] = GrepArgs
    permission_key = "grep"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a grep invocation."""
        assert isinstance(args, GrepArgs)
        return f"Grep {args.pattern[:60]}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Search contents via rg (or grep fallback) on the runner."""
        validated = self.coerce_args(args)
        assert isinstance(validated, GrepArgs)
        args = validated
        base = sanitize_harness_path(args.path or ctx.directory)
        include_glob = f" --glob {shlex.quote(args.include)}" if args.include else ""
        command = (
            "(command -v rg >/dev/null 2>&1 && "
            f"rg --no-heading --line-number --color never{include_glob} "
            f"{shlex.quote(args.pattern)} {shlex.quote(base)} "
            f"2>/dev/null || "
            f"grep -rn {shlex.quote(args.pattern)} {shlex.quote(base)} "
            "2>/dev/null) "
            f"| head -n {GREP_MAX_RESULTS + 1}"
        )
        try:
            result = await ctx.accessor.exec_wait(
                command, workdir=HARNESS_WORKSPACE_ROOT, timeout=30.0
            )
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        if result.exit_code not in (0, 1):
            raise ToolError(
                f"grep failed with exit {result.exit_code}: {result.stderr.strip()}",
                tool=self.name,
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        truncated = len(lines) > GREP_MAX_RESULTS
        lines = lines[:GREP_MAX_RESULTS]
        capped: list[str] = []
        for line in lines:
            if len(line) > GREP_MAX_LINE_LENGTH:
                line = f"{line[:GREP_MAX_LINE_LENGTH]}{_GREP_LINE_SUFFIX}"
            capped.append(line)
        lines = capped
        return ToolResult(
            output="\n".join(lines),
            truncated=truncated,
            metadata={"pattern": args.pattern, "path": base},
        )


class ListTool(Tool):
    """List directory entries via the workspace accessor."""

    name = "list"
    description = "List files and directories under a workspace path."
    args_schema: type[BaseModel] = ListArgs
    permission_key = "read"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a list invocation."""
        assert isinstance(args, ListArgs)
        return f"List {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """List a directory via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ListArgs)
        args = validated
        safe_path = sanitize_harness_path(args.path or ctx.directory)
        try:
            entries = await ctx.accessor.list_dir(safe_path)
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        lines = [
            f"{'dir' if entry.is_dir else 'file'} {entry.name}" for entry in entries
        ]
        return ToolResult(
            output="\n".join(lines),
            metadata={"path": safe_path, "count": len(entries)},
        )
