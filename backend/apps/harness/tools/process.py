"""Background-process tools for the agent harness.

The synchronous ``bash`` tool stays unchanged; long-running servers and
watchers are managed through these four thin tools instead. Monitoring
is status-only: agents read log content on demand with the ``read``
tool using the ``log_path`` returned by ``process_start``/``process_get``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..access.base import (
    HARNESS_WORKSPACE_ROOT,
    sanitize_harness_path,
    validate_harness_env,
)
from ..access.runner_accessor import RunnerAccessorError
from .base import Tool, ToolContext, ToolError, ToolResult


class ProcessStartArgs(BaseModel):
    """Arguments for the process_start tool."""

    command: str = Field(description="Shell command to run in the background.")
    workdir: str = Field(default=HARNESS_WORKSPACE_ROOT)
    env: dict[str, str] = Field(default_factory=dict)
    name: str = Field(default="")


class ProcessListArgs(BaseModel):
    """Arguments for the process_list tool (no arguments)."""


class ProcessGetArgs(BaseModel):
    """Arguments for the process_get tool."""

    process_id: str = Field(description="Background process UUID.")


class ProcessStopArgs(BaseModel):
    """Arguments for the process_stop tool."""

    process_id: str = Field(description="Background process UUID.")


def _status_line(record: dict) -> str:
    """Render one compact status line for a process record."""
    return (
        f"{record.get('process_id', '')} "
        f"{record.get('status', 'unknown')} "
        f"pid={record.get('pid')} "
        f"exit_code={record.get('exit_code')} "
        f"{record.get('command', '')}".rstrip()
    )


class ProcessStartTool(Tool):
    """Start a detached background process in the workspace."""

    name = "process_start"
    description = (
        "Start a background process in the workspace. Returns the process "
        "id, pid, and log path; check status with process_get, stop with "
        "process_stop, and read logs with the read tool."
    )
    args_schema: type[BaseModel] = ProcessStartArgs
    permission_key = "process"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a process_start invocation."""
        assert isinstance(args, ProcessStartArgs)
        command = args.command.strip().splitlines()[0] if args.command else ""
        return f"Start {command[:80]}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Start a background process via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ProcessStartArgs)
        args = validated
        if not args.command.strip():
            raise ToolError("command must not be empty", tool=self.name)
        try:
            workdir = sanitize_harness_path(args.workdir or ctx.directory)
        except ValueError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        try:
            env = validate_harness_env(args.env or {})
        except ValueError as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        try:
            record = await ctx.accessor.process_start(
                args.command,
                workdir=workdir,
                env=env,
                name=args.name or "",
            )
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        process_id = str(record.get("process_id", ""))
        pid = record.get("pid")
        log_path = str(record.get("log_path", ""))
        output = (
            f"Started background process {process_id} (pid {pid}). "
            "Status via process_get, stop via process_stop. "
            f"Logs: read {log_path}."
        )
        return ToolResult(
            output=output,
            metadata={
                "process_id": process_id,
                "pid": pid,
                "log_path": log_path,
                "status": str(record.get("status", "")),
            },
        )


class ProcessListTool(Tool):
    """List background processes of the workspace."""

    name = "process_list"
    description = (
        "List background processes of the workspace with status, pid, "
        "exit code, and command."
    )
    args_schema: type[BaseModel] = ProcessListArgs
    permission_key = "process"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a process_list invocation."""
        return "List processes"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """List background processes via the workspace accessor."""
        self.coerce_args(args)
        try:
            records = await ctx.accessor.process_list()
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        if not records:
            return ToolResult(
                output="No background processes running.",
                metadata={"count": 0, "processes": []},
            )
        lines = [_status_line(record) for record in records]
        return ToolResult(
            output="\n".join(lines),
            metadata={"count": len(records), "processes": records},
        )


class ProcessGetTool(Tool):
    """Return the status of one background process."""

    name = "process_get"
    description = (
        "Get the status of one background process (status, pid, exit "
        "code, log path)."
    )
    args_schema: type[BaseModel] = ProcessGetArgs
    permission_key = "process"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a process_get invocation."""
        assert isinstance(args, ProcessGetArgs)
        return f"Process {args.process_id}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Return one process status via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ProcessGetArgs)
        args = validated
        if not args.process_id.strip():
            raise ToolError("process_id must not be empty", tool=self.name)
        try:
            record = await ctx.accessor.process_get(args.process_id.strip())
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        return ToolResult(output=_status_line(record), metadata=dict(record))


class ProcessStopTool(Tool):
    """Stop a background process (SIGTERM, then SIGKILL after grace)."""

    name = "process_stop"
    description = (
        "Stop a background process (SIGTERM, then SIGKILL after a short "
        "grace period). Already-finished processes are returned unchanged."
    )
    args_schema: type[BaseModel] = ProcessStopArgs
    permission_key = "process"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a process_stop invocation."""
        assert isinstance(args, ProcessStopArgs)
        return f"Stop {args.process_id}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Stop a background process via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ProcessStopArgs)
        args = validated
        if not args.process_id.strip():
            raise ToolError("process_id must not be empty", tool=self.name)
        try:
            record = await ctx.accessor.process_stop(
                args.process_id.strip()
            )
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        process_id = str(record.get("process_id", args.process_id.strip()))
        status = str(record.get("status", ""))
        if status in ("exited", "killed", "failed"):
            output = (
                f"Process {process_id} already exited "
                f"(status {status}, exit_code {record.get('exit_code')})."
            )
        else:
            output = f"Stopped background process {process_id} (status {status})."
        return ToolResult(output=output, metadata=dict(record))
