"""Background-process tools for the agent harness.

The synchronous ``bash`` tool stays unchanged; long-running servers and
watchers are managed through these six thin tools instead. Monitoring
is status-only: agents read log content on demand with the ``read``
tool using the ``log_path`` returned by ``process_start``/``process_get``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..access.base import (
    HARNESS_WORKSPACE_ROOT,
    sanitize_exec_workdir,
    validate_harness_env,
)
from ..access.runner_accessor import RunnerAccessorError
from .base import Tool, ToolContext, ToolError, ToolResult


class ProcessStartArgs(BaseModel):
    """Arguments for the process_start tool."""

    command: str = Field(description="Shell command to run in the background.")
    workdir: str = Field(default=HARNESS_WORKSPACE_ROOT)
    env: dict[str, str] = Field(default_factory=dict)
    name: str = Field(
        description=(
            "Process name: the identity per workspace. Reusing an existing "
            "name restarts the same application in place (stable id, new "
            "log, run count +1, command/workdir overwritten)."
        )
    )


class ProcessListArgs(BaseModel):
    """Arguments for the process_list tool (no arguments)."""


class ProcessGetArgs(BaseModel):
    """Arguments for the process_get tool."""

    process_id: str = Field(description="Background process UUID or exact name.")


class ProcessStopArgs(BaseModel):
    """Arguments for the process_stop tool."""

    process_id: str = Field(description="Background process UUID or exact name.")


class ProcessRestartArgs(BaseModel):
    """Arguments for the process_restart tool."""

    process_id: str = Field(description="Background process UUID or exact name.")


class ProcessDeleteArgs(BaseModel):
    """Arguments for the process_delete tool."""

    process_id: str = Field(description="Background process UUID or exact name.")


def _status_line(record: dict) -> str:
    """Render one compact status line for a process record."""
    name = str(record.get("name") or "")
    process_id = str(record.get("process_id", ""))
    head = f"{name} {process_id}".strip() if name else process_id
    parts = [
        head,
        str(record.get("status", "unknown")),
        f"pid={record.get('pid')}",
        f"exit_code={record.get('exit_code')}",
    ]
    run_count = record.get("run_count")
    if run_count is not None:
        parts.append(f"run={run_count}")
    command = str(record.get("command", "") or "")
    if command:
        parts.append(command)
    return " ".join(part for part in parts if part).rstrip()


def _run_count(record: dict) -> int:
    """Return the record's run count (defaults to 1 when missing)."""
    try:
        return int(record.get("run_count") or 1)
    except (TypeError, ValueError):
        return 1


class ProcessStartTool(Tool):
    """Start a detached background process in the workspace."""

    name = "process_start"
    description = (
        "Start a background process in the workspace. The name is the "
        "identity per workspace: the same name restarts the same "
        "application in place (new log, run count +1, command/workdir "
        "overwritten); a stopped process is restarted via the same name. "
        "Returns the process id, pid, and log path; check status with "
        "process_get, stop with process_stop, and read logs with the "
        "read tool."
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
        try:
            validated = self.coerce_args(args)
        except Exception as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        assert isinstance(validated, ProcessStartArgs)
        args = validated
        if not args.command.strip():
            raise ToolError("command must not be empty", tool=self.name)
        name = (args.name or "").strip()
        if not name:
            raise ToolError("name must not be empty", tool=self.name)
        try:
            workdir = sanitize_exec_workdir(args.workdir or ctx.directory)
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
                name=name,
            )
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        process_id = str(record.get("process_id", ""))
        pid = record.get("pid")
        log_path = str(record.get("log_path", ""))
        run_count = _run_count(record)
        if run_count > 1:
            output = (
                f"Restarted background process '{name}' (run {run_count}, "
                f"id {process_id}, pid {pid}). "
                "Status via process_get, stop via process_stop. "
                f"Logs: read {log_path}."
            )
        else:
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
                "name": str(record.get("name", "") or name),
                "run_count": run_count,
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
        "code, log path). Takes the process UUID or its exact name."
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
        try:
            validated = self.coerce_args(args)
        except Exception as exc:
            raise ToolError(str(exc), tool=self.name) from exc
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
        "grace period). Takes the process UUID or its exact name. "
        "Already-finished processes are returned unchanged."
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
        try:
            validated = self.coerce_args(args)
        except Exception as exc:
            raise ToolError(str(exc), tool=self.name) from exc
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


class ProcessRestartTool(Tool):
    """Restart a background process on the same row (stable id, new log)."""

    name = "process_restart"
    description = (
        "Restart a background process on the same row (stable id, new "
        "log). Takes the process UUID or its exact name and reuses the "
        "stored command/workdir. Returns the new pid, run count, and "
        "log path; check status with process_get."
    )
    args_schema: type[BaseModel] = ProcessRestartArgs
    permission_key = "process"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a process_restart invocation."""
        assert isinstance(args, ProcessRestartArgs)
        return f"Restart {args.process_id}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Restart a background process via the workspace accessor."""
        try:
            validated = self.coerce_args(args)
        except Exception as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        assert isinstance(validated, ProcessRestartArgs)
        args = validated
        if not args.process_id.strip():
            raise ToolError("process_id must not be empty", tool=self.name)
        key = args.process_id.strip()
        try:
            record = await ctx.accessor.process_restart(key)
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        process_id = str(record.get("process_id", key))
        name = str(record.get("name", "") or key)
        pid = record.get("pid")
        log_path = str(record.get("log_path", ""))
        run_count = _run_count(record)
        output = (
            f"Restarted background process '{name}' (run {run_count}, "
            f"id {process_id}, pid {pid}). "
            "Status via process_get, stop via process_stop. "
            f"Logs: read {log_path}."
        )
        return ToolResult(output=output, metadata=dict(record))


class ProcessDeleteTool(Tool):
    """Delete a background process from the workspace list."""

    name = "process_delete"
    description = (
        "Delete a background process from the list (stops it first if "
        "running). Takes the process UUID or its exact name."
    )
    args_schema: type[BaseModel] = ProcessDeleteArgs
    permission_key = "process"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a process_delete invocation."""
        assert isinstance(args, ProcessDeleteArgs)
        return f"Delete {args.process_id}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Delete a background process via the workspace accessor."""
        try:
            validated = self.coerce_args(args)
        except Exception as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        assert isinstance(validated, ProcessDeleteArgs)
        args = validated
        if not args.process_id.strip():
            raise ToolError("process_id must not be empty", tool=self.name)
        key = args.process_id.strip()
        try:
            record = await ctx.accessor.process_delete(key)
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ToolError(str(exc), tool=self.name) from exc
        process_id = str(record.get("process_id", key))
        return ToolResult(
            output=f"Deleted background process {process_id}.",
            metadata=dict(record),
        )
