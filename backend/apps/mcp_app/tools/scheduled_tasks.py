"""Scheduled task tools: prompts that start a new chat on a schedule."""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission
from apps.scheduled_tasks.services import ScheduledTaskService

from .. import resolve
from ..context import ToolContext
from ..registry import ToolSpec, registrar
from ..results import compact, paginate, preview
from ..schema import (
    OFFSET,
    WAIT_SECONDS,
    array,
    boolean,
    enum,
    integer,
    limit,
    obj,
    string,
    string_list,
)
from .chats import reply_since, wait_for_chat

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
TASK_REF = string("Scheduled task ID or exact name.")

SCHEDULE_FIELDS = {
    "recurrence": enum(
        ["once", "daily", "weekly", "cron"],
        "'once': a single run at run_date + time. 'daily': every day at time. "
        "'weekly': at time on weekdays. 'cron': cron_expression (time unused).",
    ),
    "run_date": string(
        "For 'once': the local date YYYY-MM-DD; with time it must lie in the future.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    ),
    "time": string(
        "Local time of day HH:MM (24h) in timezone, default 09:00.",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    ),
    "weekdays": array(
        {"type": "string", "enum": WEEKDAYS},
        "For 'weekly': days to run, e.g. ['mon', 'thu'].",
        minItems=1,
        uniqueItems=True,
    ),
    "cron_expression": string(
        "For 'cron': five-field cron in local time, e.g. '0 9 * * 1-5' (09:00 on "
        "weekdays) or '*/30 8-18 * * *'; separate several expressions with ';'."
    ),
    "timezone": string(
        "IANA time zone for time/run_date/cron, e.g. 'Europe/Berlin' (default "
        "UTC). Use the user's time zone."
    ),
}

RUN_FIELDS = {
    "engine": enum(
        ["native", "claude", "codex"],
        "Agent engine (default native); external engines must be connected.",
    ),
    "model": string("Model ID from model_list; omit for the engine default."),
    "reasoning_effort": string("Optional effort level, e.g. 'low' or 'high'."),
    "mode": enum(["build", "plan"], "'build' (default) or 'plan' (read-only)."),
    "skills": string_list("Skill IDs or names the agent should follow."),
}


# ---------------------------------------------------------------------------
# Presenters
# ---------------------------------------------------------------------------


def _schedule_text(task: Any) -> str:
    """Describe a task's cadence the way a user would."""
    at = f"{task.local_time.strftime('%H:%M')} {task.timezone_name}"
    if task.recurrence == "once":
        return f"once on {task.run_date} at {at}"
    if task.recurrence == "daily":
        return f"daily at {at}"
    if task.recurrence == "weekly":
        days = ", ".join(WEEKDAYS[day].capitalize() for day in task.weekdays or [])
        return f"weekly on {days} at {at}"
    return f"cron '{task.cron_expression}' ({task.timezone_name})"


def _status(task: Any) -> str:
    if task.enabled:
        return "active"
    return "ended" if task.recurrence == "once" else "paused"


def _summary(task: Any) -> dict[str, Any]:
    return compact(
        {
            "scheduled_task_id": task.id,
            "name": task.name,
            "workspace": task.workspace.name,
            "workspace_id": task.workspace_id,
            "schedule": _schedule_text(task),
            "status": _status(task),
            "next_run_at": task.next_run_at if task.enabled else None,
            "prompt_preview": preview(task.prompt, 120),
        }
    )


def _run(run: Any) -> dict[str, Any]:
    message = run.assistant_message
    return compact(
        {
            "run_id": run.id,
            "scheduled_for": run.scheduled_for,
            "trigger": run.trigger,
            "status": run.status,
            "chat_id": run.session_id,
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "error": run.error or (message.error if message else ""),
            "reason": run.reason,
        }
    )


def _full(ctx: ToolContext, task: Any, runs: int) -> dict[str, Any]:
    data = _summary(task)
    data.pop("prompt_preview", None)
    data.update(
        compact(
            {
                "prompt": task.prompt,
                "recurrence": task.recurrence,
                "run_date": task.run_date,
                "time": task.local_time.strftime("%H:%M"),
                "weekdays": [WEEKDAYS[day] for day in task.weekdays or []],
                "cron_expression": task.cron_expression,
                "timezone": task.timezone_name,
                "engine": task.harness_id,
                "model": task.model or "default",
                "reasoning_effort": task.reasoning_effort,
                "mode": task.mode,
                "skills": resolve.skill_names(ctx, task.skill_ids),
                "created_at": task.created_at,
            }
        )
    )
    if runs:
        history = ScheduledTaskService().list_runs(task)
        data["recent_runs"] = [_run(run) for run in history[:runs]]
        data["total_runs"] = len(history)
    return data


def _values(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Map tool arguments onto the scheduled task service fields."""
    renames = {
        "time": "local_time",
        "timezone": "timezone_name",
        "engine": "harness_id",
    }
    values: dict[str, Any] = {}
    for key in (
        "name",
        "prompt",
        "mode",
        "model",
        "reasoning_effort",
        "engine",
        "recurrence",
        "run_date",
        "time",
        "cron_expression",
        "timezone",
        "enabled",
    ):
        if key in args:
            values[renames.get(key, key)] = args[key]
    if "weekdays" in args:
        values["weekdays"] = [WEEKDAYS.index(day) for day in args["weekdays"]]
    if "skills" in args:
        values["skill_ids"] = resolve.skill_ids(ctx, args["skills"])
    return values


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "scheduled_task_list",
    title="List scheduled tasks",
    permission=APIKeyPermission.HARNESS_READ,
    read_only=True,
    schema=obj(
        {
            "workspace": string("Optional workspace ID or name to filter by."),
            "status": enum(
                ["active", "paused", "ended"],
                "Optional filter: active, paused (disabled recurring) or ended "
                "(one-time task that already ran).",
            ),
            "limit": limit(50, 200),
            "offset": OFFSET,
        }
    ),
    description="""
        List the user's scheduled tasks, ordered by next run: each starts a new
        chat with a fixed prompt in a workspace on a schedule (once, daily,
        weekly or cron). Use scheduled_task_get for the full prompt and the
        run history.
    """,
)
async def scheduled_task_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        tasks = ScheduledTaskService().list(
            organization_id=ctx.organization_id, owner_id=ctx.user.id
        )
        if args.get("workspace"):
            workspace = resolve.workspace(ctx, args["workspace"])
            tasks = [task for task in tasks if task.workspace_id == workspace.id]
        rows = [
            _summary(task)
            for task in tasks
            if not args.get("status") or _status(task) == args["status"]
        ]
        page, meta = paginate(
            rows, limit=args.get("limit", 50), offset=args.get("offset", 0)
        )
        return {"scheduled_tasks": page, **meta}

    return await sync_to_async(build)()


@tool(
    "scheduled_task_get",
    title="Get scheduled task",
    permission=APIKeyPermission.HARNESS_READ,
    read_only=True,
    schema=obj(
        {
            "scheduled_task": TASK_REF,
            "runs": integer(
                "Number of most recent runs to include (default 5, 0 = none).",
                minimum=0,
                maximum=50,
                default=5,
            ),
        },
        ("scheduled_task",),
    ),
    description="""
        Get one scheduled task: prompt, schedule, engine/model/skills and its
        most recent runs with status and the chat_id each run created (read
        the outcome with chat_read).
    """,
)
async def scheduled_task_get(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        task = resolve.scheduled_task(ctx, args["scheduled_task"])
        return _full(ctx, task, args.get("runs", 5))

    return await sync_to_async(build)()


@tool(
    "scheduled_task_create",
    title="Create scheduled task",
    permission=APIKeyPermission.HARNESS_RUN,
    schema=obj(
        {
            "name": string("Short name, e.g. 'Nightly dependency check'.", minLength=1),
            "workspace": string("Workspace ID or exact name the agent runs in."),
            "prompt": string(
                "The task the agent receives on every run; make it self-contained "
                "(it starts a fresh chat without earlier context).",
                minLength=1,
            ),
            **SCHEDULE_FIELDS,
            **RUN_FIELDS,
            "enabled": boolean("Whether the schedule is active (default true)."),
        },
        ("name", "workspace", "prompt", "recurrence"),
    ),
    description="""
        Schedule a prompt: on each occurrence OpenCuria starts a new chat in
        the workspace (starting a stopped workspace first). Examples:
        recurrence='once' with run_date and time; recurrence='weekly' with
        weekdays=['mon'] and time='08:30'; recurrence='cron' with
        cron_expression='0 9 * * 1-5'. Always pass the user's timezone.
    """,
)
async def scheduled_task_create(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    def create() -> dict[str, Any]:
        workspace = resolve.workspace(ctx, args["workspace"])
        task = ScheduledTaskService().create(
            organization_id=ctx.organization_id,
            owner_id=ctx.user.id,
            workspace=workspace,
            values=_values(ctx, args),
        )
        return _full(ctx, task, 0)

    return await sync_to_async(create)()


@tool(
    "scheduled_task_update",
    title="Update scheduled task",
    permission=APIKeyPermission.HARNESS_RUN,
    idempotent=True,
    schema=obj(
        {
            "scheduled_task": TASK_REF,
            "name": string("New name.", minLength=1),
            "workspace": string("Move the task to another workspace (ID or name)."),
            "prompt": string("New prompt.", minLength=1),
            **SCHEDULE_FIELDS,
            **RUN_FIELDS,
            "enabled": boolean(
                "false pauses the schedule, true resumes it (a one-time task "
                "needs a future run_date and time to be re-enabled)."
            ),
        },
        ("scheduled_task",),
    ),
    description="""
        Change a scheduled task; only the fields you pass change. Pause or
        resume it with enabled. When changing the cadence pass recurrence
        together with its fields (run_date, weekdays or cron_expression).
    """,
)
async def scheduled_task_update(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    def update() -> dict[str, Any]:
        service = ScheduledTaskService()
        task = resolve.scheduled_task(ctx, args["scheduled_task"])
        workspace = (
            resolve.workspace(ctx, args["workspace"]) if args.get("workspace") else None
        )
        updated = service.update(task, _values(ctx, args), workspace=workspace)
        return _full(ctx, updated, 0)

    return await sync_to_async(update)()


@tool(
    "scheduled_task_delete",
    title="Delete scheduled task",
    permission=APIKeyPermission.HARNESS_RUN,
    destructive=True,
    schema=obj({"scheduled_task": TASK_REF}, ("scheduled_task",)),
    description="""
        Delete a scheduled task so it never runs again. Chats created by past
        runs are kept. To stop it only temporarily use scheduled_task_update
        with enabled=false.
    """,
)
async def scheduled_task_delete(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    def delete() -> dict[str, Any]:
        task = resolve.scheduled_task(ctx, args["scheduled_task"])
        ScheduledTaskService().delete(task)
        return {"scheduled_task_id": task.id, "name": task.name, "deleted": True}

    return await sync_to_async(delete)()


@tool(
    "scheduled_task_run_now",
    title="Run scheduled task now",
    permission=APIKeyPermission.HARNESS_RUN,
    schema=obj(
        {"scheduled_task": TASK_REF, "wait_seconds": WAIT_SECONDS},
        ("scheduled_task",),
    ),
    description="""
        Run a scheduled task immediately, independent of its schedule (also
        works for paused tasks). Starts a new chat; the result includes its
        chat_id and, with wait_seconds, the agent's reply.
    """,
)
async def scheduled_task_run_now(
    ctx: ToolContext, args: dict[str, Any]
) -> dict[str, Any]:
    task = await sync_to_async(resolve.scheduled_task)(ctx, args["scheduled_task"])
    run = await ScheduledTaskService().run_now(task)
    data: dict[str, Any] = {"scheduled_task": task.name}
    data["run"] = await sync_to_async(_run)(run)
    if run.session_id and args.get("wait_seconds"):
        await wait_for_chat(ctx, run.session_id, args["wait_seconds"])
        data.update(await sync_to_async(reply_since)(ctx, run.session_id, 0))
    elif run.session_id:
        data["next_step"] = (
            "Read the agent's progress with chat_read (chat_id from run)."
        )
    return data
