"""Chat tools: read conversations with agents and drive them.

A chat is a harness session: a conversation with an agent that runs inside
one workspace. Runs are asynchronous; tools that start work can optionally
wait (bounded) until the agent finishes its turn or needs input, so a model
gets the reply without polling.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from apps.harness.conversation_service import ConversationService
from apps.harness.harness_service import get_harness_service
from apps.harness.schemas import part_to_out, timeline_to_out
from apps.harness.session_access_service import SessionAccessService
from apps.harness.session_settings_service import SessionSettingsService
from apps.runners.enums import WorkspaceStatus
from apps.runners.services import get_runner_service
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .. import resolve
from ..context import ToolContext
from ..errors import ToolError
from ..registry import ToolSpec, registrar
from ..results import clip, compact, paginate, preview, timestamp
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

log = structlog.get_logger(__name__)

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

ENGINES = ["native", "claude", "codex"]
_POLL_SECONDS = 1.0
_TEXT_LIMIT = {"concise": 6_000, "full": 30_000}
_ACTIVITY_LIMIT = 15
_PART_OUTPUT_LIMIT = 20_000
#: Finish reasons of a turn that ended normally (others are reported).
_NORMAL_FINISH = frozenset(
    {"", "stop", "end_turn", "done", "completed", "tool_calls", "tool-calls"}
)

CHAT_ID = string("Chat ID (UUID) from chat_list or chat_start.")


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def _owned_chat(ctx: ToolContext, chat_id: str) -> tuple[Any, Any]:
    """Return ``(session, workspace)`` for a chat in one of the caller's workspaces."""
    session_id = resolve.parse_uuid(chat_id)
    if session_id is None:
        raise ToolError(
            f"'{chat_id}' is not a chat ID. Chat IDs are UUIDs; find them with "
            "chat_list."
        )
    not_found = ToolError(f"Chat {session_id} not found. Find chats with chat_list.")
    try:
        session = get_harness_service().get_session(session_id)
        workspace = get_runner_service().get_workspace_for_user(
            session.workspace_id, user=ctx.user, organization_id=ctx.organization_id
        )
    except NotFoundError:
        raise not_found from None
    return session, workspace


def _require_running_workspace(workspace: Any) -> None:
    status = str(workspace.status)
    if status == WorkspaceStatus.RUNNING:
        return
    name = workspace.name or workspace.id
    if status == WorkspaceStatus.STOPPED:
        raise ToolError(
            f"Workspace '{name}' is stopped. Start it with workspace_start "
            "(wait_seconds=60), then retry."
        )
    raise ToolError(
        f"Workspace '{name}' is {status}; chats need a running workspace. Check "
        "it with workspace_get."
    )


# ---------------------------------------------------------------------------
# Presenters
# ---------------------------------------------------------------------------


def _is_running(session: Any) -> bool:
    return get_harness_service().is_running(session.id) or session.status == "busy"


def _pending_inputs(session: Any) -> list[dict[str, Any]]:
    """Questions and permission requests the agent (or a subagent) waits on."""
    service = get_harness_service()
    pending: list[dict[str, Any]] = []
    for request in service.interactions.list_pending_permissions(
        session.id, include_descendants=True
    ):
        pending.append(
            compact(
                {
                    "type": "permission",
                    "request_id": request["request_id"],
                    "tool": request["tool"],
                    "action": request["pattern"],
                    "title": request["title"],
                    "agent": request["agent_name"],
                    "subagent_chat_id": request["session_id"]
                    if request["session_id"] != str(session.id)
                    else None,
                    "answer_with": "chat_respond with permission once|always|reject",
                }
            )
        )
    for request in service.interactions.list_pending_questions(
        session.id, include_descendants=True
    ):
        pending.append(
            compact(
                {
                    "type": "question",
                    "request_id": request["request_id"],
                    "agent": request["agent_name"],
                    "questions": [
                        compact(
                            {
                                "header": question.get("header"),
                                "question": question.get("question"),
                                "options": [
                                    option.get("label")
                                    if isinstance(option, dict)
                                    else option
                                    for option in question.get("options") or []
                                ],
                                "multiple": bool(question.get("multiple")) or None,
                            }
                        )
                        for question in request["questions"]
                        if isinstance(question, dict)
                    ],
                    "subagent_chat_id": request["session_id"]
                    if request["session_id"] != str(session.id)
                    else None,
                    "answer_with": "chat_respond with answers (one per question)",
                }
            )
        )
    return pending


def _state(session: Any, pending: list[dict[str, Any]]) -> str:
    if pending:
        return "waiting_for_input"
    return "running" if _is_running(session) else "idle"


def _header(ctx: ToolContext, session: Any, workspace: Any) -> dict[str, Any]:
    return compact(
        {
            "chat_id": session.id,
            "title": session.title,
            "workspace": {"workspace_id": workspace.id, "name": workspace.name},
            "engine": getattr(session, "harness_id", "native") or "native",
            "model": session.model or "default",
            "reasoning_effort": session.reasoning_effort,
            "mode": session.mode,
            "skills": resolve.skill_names(ctx, session.skill_ids),
            "parent_chat_id": session.parent_id,
            "cost_usd": round(float(session.cost or 0.0), 4) or None,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
    )


def _tool_label(name: str, title: str) -> str:
    """Name a tool step once: 'bash: Run tests', 'Playwright / browser_click'."""
    segments: list[str] = []
    for segment in (s.strip() for s in title.split(" / ")):
        if segment and (not segments or segments[-1] != segment):
            segments.append(segment)
    cleaned = " / ".join(segments)
    if not cleaned:
        return name
    if segments[-1].casefold() in name.casefold():
        return cleaned  # the title already names the (MCP) tool
    return f"{name}: {cleaned}"


def _collapse_repeats(entries: list[Any]) -> list[Any]:
    """Fold consecutive identical one-line steps into 'step (×n)'."""
    folded: list[Any] = []
    counts: list[int] = []
    for entry in entries:
        if folded and isinstance(entry, str) and entry == folded[-1]:
            counts[-1] += 1
            continue
        folded.append(entry)
        counts.append(1)
    return [
        f"{entry} (×{count})" if count > 1 else entry
        for entry, count in zip(folded, counts, strict=True)
    ]


def _activity_entry(part: dict[str, Any], detail: str) -> Any:
    """One tool/patch/subagent step of an assistant turn."""
    kind = part["type"]
    display = part.get("display") or {}
    state = part.get("state") or ""
    if kind == "tool":
        label = _tool_label(
            display.get("tool") or part.get("tool") or "tool",
            part.get("title") or display.get("summary") or "",
        )
    elif kind == "patch":
        label = (
            f"edited {display.get('path') or 'file'} "
            f"(+{display.get('additions', 0)} -{display.get('deletions', 0)})"
        )
    elif kind == "subtask":
        label = f"subagent {display.get('agent') or ''}: {display.get('summary') or ''}"
        state = display.get("status") or state
    elif kind == "compaction":
        label = "conversation context was compacted"
    elif kind == "reasoning":
        if detail != "full":
            return None
        label = f"thinking: {display.get('summary') or ''}"
    else:
        return None
    label = preview(label, 200)
    if detail != "full":
        return label if state in ("", "completed") else f"{label} [{state}]"
    return compact(
        {
            "part_id": part["id"],
            "kind": kind,
            "summary": label,
            "state": state,
            "subagent_chat_id": display.get("child_session_id") or None,
        }
    )


def _message(index: int, message: dict[str, Any], detail: str) -> dict[str, Any]:
    parts = message.get("parts") or []
    texts = [p.get("output") or "" for p in parts if p["type"] == "text"]
    text = "\n\n".join(t for t in texts if t.strip()) or message.get("content") or ""
    steered = [
        p.get("output") or "" for p in parts if p["type"] == "steer" and p.get("output")
    ]
    activity = _collapse_repeats(
        [
            entry
            for entry in (_activity_entry(part, detail) for part in parts)
            if entry is not None
        ]
    )
    omitted = 0
    if detail != "full" and len(activity) > _ACTIVITY_LIMIT:
        omitted = len(activity) - _ACTIVITY_LIMIT
        activity = activity[-_ACTIVITY_LIMIT:]
    finish = message.get("finish") or ""
    return compact(
        {
            "index": index,
            "role": message["role"],
            "text": clip(text, _TEXT_LIMIT[detail]),
            "messages_sent_during_turn": steered,
            "activity": activity,
            "earlier_steps_omitted": omitted or None,
            "finish": None if finish in _NORMAL_FINISH else finish,
            "error": message.get("error") or None,
            "model": message.get("model") if detail == "full" else None,
            "created_at": timestamp(message.get("created_at")),
        }
    )


def _timeline(session: Any) -> dict[str, Any]:
    return timeline_to_out(
        get_harness_service(), session, conversations=ConversationService()
    )


def _transcript(
    ctx: ToolContext,
    session: Any,
    workspace: Any,
    *,
    limit: int,
    before: int | None,
    detail: str,
) -> dict[str, Any]:
    timeline = _timeline(session)
    messages = timeline["messages"]
    end = len(messages) if before is None else max(0, min(before, len(messages)))
    start = max(0, end - limit)
    pending = _pending_inputs(session)
    data: dict[str, Any] = {
        "chat": _header(ctx, session, workspace),
        "state": _state(session, pending),
    }
    if pending:
        data["waiting_for_input"] = pending
    queued = [steer.get("content") for steer in timeline.get("steers") or []]
    if queued:
        data["queued_messages"] = queued
    data["messages"] = [
        _message(index, messages[index], detail) for index in range(start, end)
    ]
    data["total_messages"] = len(messages)
    if start > 0:
        data["older_messages"] = start
        data["next_before"] = start
    return data


def reply_since(
    ctx: ToolContext, session_id: uuid.UUID, first_index: int
) -> dict[str, Any]:
    """Current state plus assistant messages created at or after *first_index*."""
    session = get_harness_service().get_session(session_id)
    messages = _timeline(session)["messages"]
    pending = _pending_inputs(session)
    replies = [
        _message(index, messages[index], "concise")
        for index in range(len(messages))
        if index >= first_index and messages[index]["role"] == "assistant"
    ]
    if not replies:
        last = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index]["role"] == "assistant"
            ),
            None,
        )
        if last is not None:
            replies = [_message(last, messages[last], "concise")]
    state = _state(session, pending)
    data: dict[str, Any] = {"state": state, "reply": replies}
    if pending:
        data["waiting_for_input"] = pending
    if state == "running":
        data["next_step"] = (
            "The agent is still working. Call chat_read with wait_seconds to get "
            "the result later."
        )
    elif state == "waiting_for_input":
        data["next_step"] = "Answer the request(s) with chat_respond."
    return data


async def _discard_empty_chat(session_id: uuid.UUID) -> None:
    """Remove a just-created chat whose first run never started."""
    service = get_harness_service()
    try:
        if not await sync_to_async(service.list_timeline_messages)(session_id):
            await service.delete_session(session_id)
    except Exception:
        log.warning("mcp_empty_chat_cleanup_failed", session_id=str(session_id))


async def wait_for_chat(ctx: ToolContext, session_id: uuid.UUID, seconds: int) -> None:
    """Block until the chat is idle or waiting for input, or *seconds* pass."""
    if seconds <= 0:
        return

    def busy() -> bool:
        session = get_harness_service().get_session(session_id)
        return _state(session, _pending_inputs(session)) == "running"

    deadline = time.monotonic() + seconds
    while await sync_to_async(busy)():
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(_POLL_SECONDS)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if args.get("workspace"):
        workspaces = [resolve.workspace(ctx, args["workspace"])]
    else:
        workspaces = resolve.list_workspaces(ctx)
    rows = ConversationService().list_conversations(
        organization_id=ctx.organization_id,
        workspace_ids=[w.id for w in workspaces],
    )
    rows.sort(key=lambda row: row["last_message_at"], reverse=True)
    wanted = args.get("state")
    query = (args.get("query") or "").strip().casefold()
    chats = []
    for row in rows:
        state = (
            "waiting_for_input"
            if row["needs_attention"]
            else "running"
            if row["status"] == "busy"
            else "idle"
        )
        if wanted == "unread":
            if not row["unread"]:
                continue
        elif wanted and wanted != state:
            continue
        if query and query not in (row["title"] or "").casefold():
            continue
        chats.append(
            compact(
                {
                    "chat_id": row["session_id"],
                    "title": row["title"],
                    "workspace": row["workspace_name"],
                    "workspace_id": row["workspace_id"],
                    "state": state,
                    "unread": row["unread"] or None,
                    "engine": row["harness_id"],
                    "model": row["model"] or "default",
                    "last_message_at": timestamp(row["last_message_at"]),
                }
            )
        )
    page, meta = paginate(
        chats, limit=args.get("limit", 20), offset=args.get("offset", 0)
    )
    return {"chats": page, **meta}


@tool(
    "chat_list",
    title="List chats",
    read_only=True,
    schema=obj(
        {
            "workspace": string(
                "Optional workspace ID or name to list only its chats."
            ),
            "state": enum(
                ["running", "waiting_for_input", "idle", "unread"],
                "Optional filter: agents still working, agents waiting for an "
                "answer or permission, finished chats, or chats with unseen "
                "replies.",
            ),
            "query": string("Optional case-insensitive filter on the chat title."),
            "limit": limit(20, 100),
            "offset": OFFSET,
        }
    ),
    description="""
        List chats (conversations with agents) across the user's workspaces,
        most recently active first. state tells whether the agent is running,
        waiting_for_input (it asked a question or needs a permission; answer
        with chat_respond) or idle. Read a chat with chat_read.
    """,
)
async def chat_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(_list)(ctx, args)


def _part_detail(session: Any, part_id: str) -> dict[str, Any]:
    wanted = resolve.parse_uuid(part_id)
    if wanted is None:
        raise ToolError("part_id must be a UUID from chat_read with detail='full'.")
    try:
        part = part_to_out(get_harness_service().get_part(session.id, wanted))
    except NotFoundError:
        raise ToolError(
            f"Step {wanted} does not belong to chat {session.id}. Use the part_id "
            "values from chat_read with detail='full'."
        ) from None
    tool_input = part.get("input") or {}
    return {
        "part": compact(
            {
                "part_id": part["id"],
                "kind": part["type"],
                "tool": (part.get("display") or {}).get("tool"),
                "title": part.get("title"),
                "state": part.get("state"),
                "input": clip(json.dumps(tool_input, ensure_ascii=False), 4_000)
                if tool_input
                else None,
                "output": clip(part.get("output"), _PART_OUTPUT_LIMIT),
            }
        )
    }


@tool(
    "chat_read",
    title="Read chat",
    read_only=True,
    schema=obj(
        {
            "chat_id": CHAT_ID,
            "limit": integer(
                "Number of most recent messages to return (default 10).",
                minimum=1,
                maximum=50,
                default=10,
            ),
            "before": integer(
                "Return messages before this index, for paging back (use "
                "next_before from the previous response).",
                minimum=0,
            ),
            "detail": enum(
                ["concise", "full"],
                "'concise' (default): message text plus one-line summaries of the "
                "agent's steps. 'full': longer text, step IDs (part_id), "
                "reasoning summaries and models.",
                default="concise",
            ),
            "part_id": string(
                "Load the full input and output of one step (a part_id from "
                "detail='full') instead of the transcript."
            ),
            "wait_seconds": WAIT_SECONDS,
        },
        ("chat_id",),
    ),
    description="""
        Read a chat transcript: the most recent messages (user prompts and
        agent replies with the steps it took), the chat state (running,
        waiting_for_input, idle) and any questions or permission requests the
        agent is waiting on. Pass wait_seconds to wait for a running agent to
        finish first. Page back with before; inspect one step's full output
        with part_id.
    """,
)
async def chat_read(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    session, _workspace = await sync_to_async(_owned_chat)(ctx, args["chat_id"])
    if args.get("part_id"):
        return await sync_to_async(_part_detail)(session, args["part_id"])
    await wait_for_chat(ctx, session.id, args.get("wait_seconds", 0))

    def build() -> dict[str, Any]:
        fresh, workspace = _owned_chat(ctx, args["chat_id"])
        return _transcript(
            ctx,
            fresh,
            workspace,
            limit=args.get("limit", 10),
            before=args.get("before"),
            detail=args.get("detail", "concise"),
        )

    return await sync_to_async(build)()


@tool(
    "chat_start",
    title="Start chat",
    schema=obj(
        {
            "workspace": string("Workspace ID or exact name the agent works in."),
            "prompt": string(
                "The task for the agent, written like a message to a colleague: "
                "goal, context, constraints and the expected result.",
                minLength=1,
            ),
            "title": string("Optional chat title (derived from the prompt otherwise)."),
            "engine": enum(
                ENGINES,
                "Agent engine: 'native' (default, OpenCuria agent), 'claude' "
                "(Claude Agent) or 'codex'. External engines must be connected "
                "(see opencuria_context).",
                default="native",
            ),
            "model": string("Model ID from model_list; omit for the engine default."),
            "reasoning_effort": string(
                "Reasoning effort from model_list.reasoning_efforts, e.g. 'high' "
                "or 'ultra' when supported. Omit for the engine/agent default."
            ),
            "mode": enum(
                ["build", "plan"],
                "'build' (default) lets the agent change files and run commands; "
                "'plan' only investigates and proposes a plan.",
                default="build",
            ),
            "skills": string_list(
                "Skill IDs or names whose instructions the agent should follow."
            ),
            "wait_seconds": WAIT_SECONDS,
        },
        ("workspace", "prompt"),
    ),
    description="""
        Start a new chat: an agent begins working on the prompt inside the
        workspace (which must be running). The agent works asynchronously;
        pass wait_seconds to get its reply in this call, otherwise follow up
        with chat_read (wait_seconds) and chat_respond if it asks something.
    """,
)
async def chat_start(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = get_harness_service()

    def prepare() -> tuple[Any, Any]:
        workspace = resolve.workspace(ctx, args["workspace"])
        _require_running_workspace(workspace)
        skill_ids = resolve.skill_ids(ctx, args.get("skills"))
        mode = args.get("mode", "build")
        session = service.create_session(
            workspace_id=workspace.id,
            organization_id=ctx.organization_id,
            prompt=args["prompt"],
            agent_name=mode,
            harness_id=args.get("engine", "native"),
            mode=mode,
            model=args.get("model", ""),
            reasoning_effort=args.get("reasoning_effort", ""),
            title=(args.get("title") or "").strip(),
            skill_ids=skill_ids,
            user_id=ctx.user.id,
        )
        return session, workspace

    session, workspace = await sync_to_async(prepare)()
    try:
        await service.start_run(
            session,
            args["prompt"],
            organization_id=ctx.organization_id,
            workspace_id=str(workspace.id),
            user_id=ctx.user.id,
            skill_ids=list(session.skill_ids or []),
        )
    except Exception:
        await _discard_empty_chat(session.id)
        raise
    await wait_for_chat(ctx, session.id, args.get("wait_seconds", 0))

    def result() -> dict[str, Any]:
        fresh = service.get_session(session.id)
        data = {"chat": _header(ctx, fresh, workspace)}
        if args.get("wait_seconds"):
            data.update(reply_since(ctx, session.id, 0))
        else:
            data["state"] = _state(fresh, _pending_inputs(fresh))
            data["next_step"] = (
                "The agent is working. Call chat_read with wait_seconds to get "
                "its reply."
            )
        return data

    return await sync_to_async(result)()


@tool(
    "chat_send",
    title="Send message",
    schema=obj(
        {
            "chat_id": CHAT_ID,
            "prompt": string("The message to the agent.", minLength=1),
            "model": string(
                "Switch the model for this and later turns (idle chats only)."
            ),
            "reasoning_effort": string(
                "Switch reasoning effort (idle chats only). Use a value from "
                "model_list.reasoning_efforts; omit to keep the current effort, "
                "or pass an empty string to restore the engine/agent default."
            ),
            "mode": enum(["build", "plan"], "Switch the mode (idle chats only)."),
            "skills": string_list(
                "Replace the selected skills for this and later turns (idle chats "
                "only)."
            ),
            "wait_seconds": WAIT_SECONDS,
        },
        ("chat_id", "prompt"),
    ),
    description="""
        Send a follow-up message to a chat. If the agent is idle, this starts
        a new turn. If it is still working, the message is delivered into its
        current turn and picked up at its next step (use this to correct or
        redirect it). Pass wait_seconds to get the reply in this call. To
        answer a question or permission request use chat_respond instead.
    """,
)
async def chat_send(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = get_harness_service()
    settings = {"model", "reasoning_effort", "mode", "skills"} & set(args)

    def prepare() -> tuple[Any, Any, int, list[str] | None]:
        session, workspace = _owned_chat(ctx, args["chat_id"])
        try:
            SessionAccessService.require_user_promptable(session.parent_id)
        except ValueError:
            raise ToolError(
                "This is a subagent chat started by another agent; send messages to "
                f"its parent chat {session.parent_id} instead."
            ) from None
        try:
            SessionAccessService.require_external_owner(
                engine_id=getattr(session, "harness_id", "native"),
                owner_id=session.created_by_id,
                user_id=ctx.user.id,
            )
        except AuthenticationError:
            raise ToolError(
                "Only the user who started this chat can use its external engine "
                "connection."
            ) from None
        skill_ids = resolve.skill_ids(ctx, args["skills"]) if "skills" in args else None
        count = len(service.list_timeline_messages(session.id))
        return session, workspace, count, skill_ids

    session, workspace, before_count, skill_ids = await sync_to_async(prepare)()
    delivery = "new_turn"
    if service.is_running(session.id):
        if settings:
            raise ToolError(
                "The agent is still working, so model, mode, effort and skills "
                "cannot change now. Send the message without them (it is "
                "delivered into the running turn) or call chat_stop first."
            )
        try:
            await service.steer_run(session, args["prompt"], user_id=ctx.user.id)
            delivery = "delivered_into_running_turn"
        except ConflictError as exc:
            if exc.code != "no_active_run":
                raise
    if delivery == "new_turn":

        def configure() -> Any:
            _require_running_workspace(resolve.workspace(ctx, str(workspace.id)))
            settings_service = SessionSettingsService()
            current = session
            if args.get("mode"):
                current = settings_service.set_mode(current.id, args["mode"])
            if args.get("model"):
                current = settings_service.set_model(current.id, args["model"])
            if "reasoning_effort" in args:
                current = settings_service.set_reasoning_effort(
                    current.id, args["reasoning_effort"]
                )
            return current

        current = await sync_to_async(configure)()
        await service.start_run(
            current,
            args["prompt"],
            organization_id=ctx.organization_id,
            workspace_id=str(current.workspace_id),
            user_id=ctx.user.id,
            skill_ids=skill_ids,
        )
    await wait_for_chat(ctx, session.id, args.get("wait_seconds", 0))

    def result() -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": session.id, "delivery": delivery}
        if args.get("wait_seconds"):
            data.update(reply_since(ctx, session.id, before_count))
        else:
            fresh = service.get_session(session.id)
            data["state"] = _state(fresh, _pending_inputs(fresh))
            data["next_step"] = (
                "Call chat_read with wait_seconds to get the agent's reply."
            )
        return data

    return await sync_to_async(result)()


@tool(
    "chat_respond",
    title="Answer agent request",
    schema=obj(
        {
            "chat_id": CHAT_ID,
            "request_id": string(
                "request_id of the pending request from chat_read waiting_for_input."
            ),
            "permission": enum(
                ["once", "always", "reject"],
                "For permission requests: allow this once, always allow this "
                "action in the chat, or reject it.",
            ),
            "answers": array(
                {"type": "string"},
                "For questions: one answer per question, in order. Use an "
                "option label or free text; for multi-select questions join the "
                "chosen labels with ', '.",
            ),
            "decline": boolean(
                "For questions: decline to answer; the agent continues without "
                "an answer."
            ),
            "wait_seconds": WAIT_SECONDS,
        },
        ("chat_id", "request_id"),
    ),
    description="""
        Answer what an agent is waiting for: a question (pass answers or
        decline) or a permission request to run a tool or command (pass
        permission). Pending requests and their request_id are listed under
        waiting_for_input by chat_read and chat_list. The agent resumes right
        away; pass wait_seconds to get its next reply.
    """,
)
async def chat_respond(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = get_harness_service()

    def locate() -> tuple[Any, dict[str, Any], Any, int]:
        session, _workspace = _owned_chat(ctx, args["chat_id"])
        try:
            SessionAccessService.require_external_owner(
                engine_id=getattr(session, "harness_id", "native"),
                owner_id=session.created_by_id,
                user_id=ctx.user.id,
            )
        except AuthenticationError:
            raise ToolError(
                "Only the user who started this chat can use its external engine "
                "connection."
            ) from None
        request_id = str(args["request_id"]).strip()
        pending = {item["request_id"]: item for item in _pending_inputs(session)}
        request = pending.get(request_id)
        if request is None:
            open_ids = ", ".join(pending) or "none"
            raise ToolError(
                f"No pending request {request_id} in this chat (open requests: "
                f"{open_ids}). It may have been answered already or the run "
                "ended; call chat_read to see the current state."
            )
        target = (
            service.get_session(uuid.UUID(request["subagent_chat_id"]))
            if request.get("subagent_chat_id")
            else session
        )
        count = len(service.list_timeline_messages(session.id))
        return session, request, target, count

    session, request, target, before_count = await sync_to_async(locate)()
    request_uuid = uuid.UUID(request["request_id"])
    if request["type"] == "permission":
        if not args.get("permission"):
            raise ToolError(
                "This is a permission request; pass permission as once, always "
                "or reject."
            )
        outcome = await service.interactions.resolve_permission(
            session=target, request_id=request_uuid, response=args["permission"]
        )
        resolved = {"type": "permission", "decision": outcome["decision"]}
    else:
        if not args.get("decline") and not args.get("answers"):
            raise ToolError(
                "This is a question; pass answers (one per question) or decline=true."
            )
        outcome = await service.interactions.resolve_question(
            session=target,
            question_id=request_uuid,
            answers=list(args.get("answers") or []),
            reject=bool(args.get("decline")),
        )
        resolved = {"type": "question", "status": outcome["status"]}
    await wait_for_chat(ctx, session.id, args.get("wait_seconds", 0))

    def result() -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": session.id, "resolved": resolved}
        if args.get("wait_seconds"):
            data.update(reply_since(ctx, session.id, before_count))
        else:
            fresh = service.get_session(session.id)
            data["state"] = _state(fresh, _pending_inputs(fresh))
        return data

    return await sync_to_async(result)()


@tool(
    "chat_stop",
    title="Stop agent",
    idempotent=True,
    schema=obj({"chat_id": CHAT_ID}, ("chat_id",)),
    description="""
        Stop the agent's current turn in a chat (including its subagents);
        pending questions are rejected and queued messages dropped. The chat
        and its history stay; continue later with chat_send. Stopping an idle
        chat is a no-op.
    """,
)
async def chat_stop(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    service = get_harness_service()
    session, _workspace = await sync_to_async(_owned_chat)(ctx, args["chat_id"])
    was_running = await sync_to_async(_is_running)(session)
    if was_running:
        await service.abort_run(session.id)
    return compact(
        {
            "chat_id": session.id,
            "stopped": was_running,
            "state": "idle",
            "note": None if was_running else "The agent was not running.",
        }
    )


@tool(
    "chat_delete",
    title="Delete chat",
    destructive=True,
    schema=obj({"chat_id": CHAT_ID}, ("chat_id",)),
    description="""
        Permanently delete a chat and its history; a running agent is stopped
        first. Files the agent changed in the workspace are not reverted.
    """,
)
async def chat_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    session, _workspace = await sync_to_async(_owned_chat)(ctx, args["chat_id"])
    await get_harness_service().delete_session(session.id)
    return {"chat_id": session.id, "title": session.title, "deleted": True}
