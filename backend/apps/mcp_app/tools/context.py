"""Orientation tools: who am I, what is connected, which models exist."""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission
from apps.harness.engines.service import EngineCatalogService
from apps.harness.harness_service import get_harness_service
from apps.harness.providers.base import ProviderError
from apps.organizations.services import OrganizationService
from apps.scheduled_tasks.services import ScheduledTaskService
from apps.skills.services import SkillService
from common.exceptions import AuthenticationError, NotFoundError

from .. import resolve
from ..context import ToolContext
from ..errors import ToolError
from ..registry import ToolSpec, registrar
from ..results import compact, paginate
from ..schema import OFFSET, enum, limit, obj, string

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

ENGINES = ["native", "claude", "codex"]


def _engines(ctx: ToolContext) -> list[dict[str, Any]]:
    """Return the selectable chat engines and their connection state."""
    rows = EngineCatalogService().list_engines(ctx.organization_id, ctx.user.id)
    return [
        compact(
            {
                "engine": row["id"],
                "name": row["name"],
                "connected": bool(row["connected"]),
                "default_model": row.get("default_model")
                or ("organization default" if row["id"] == "native" else None),
            }
        )
        for row in rows
    ]


def _context(ctx: ToolContext) -> dict[str, Any]:
    user = ctx.user
    data: dict[str, Any] = {
        "user": compact({"email": user.email, "name": user.get_full_name()}),
        "organization": {
            "organization_id": ctx.organization_id,
            "name": ctx.organization_name,
            "role": ctx.role,
        },
    }
    if ctx.workspace_id is not None:
        # Agents are bound to their workspace's organization.
        data["current_workspace_id"] = ctx.workspace_id
    others = [
        {"organization_id": m["id"], "name": m["name"], "role": m["role"]}
        for m in OrganizationService().list_user_organizations(user)
        if m["id"] != ctx.organization_id
    ]
    if others and ctx.workspace_id is None:
        data["other_organizations"] = others
        data["switch_organization"] = (
            "Send the X-Organization-Id header with another organization_id."
        )
    counts: dict[str, Any] = {}
    if ctx.can(APIKeyPermission.WORKSPACES_READ):
        workspaces = resolve.list_workspaces(ctx)
        counts["workspaces"] = len(workspaces)
        counts["workspaces_running"] = sum(
            1 for w in workspaces if str(w.status) == "running"
        )
    if ctx.can(APIKeyPermission.HARNESS_READ):
        chats = get_harness_service().list_conversations(
            organization_id=ctx.organization_id,
            workspace_ids=[w.id for w in resolve.list_workspaces(ctx)],
        )
        counts["chats"] = len(chats)
        counts["chats_running"] = sum(1 for c in chats if c["status"] == "busy")
        counts["chats_waiting_for_input"] = sum(
            1 for c in chats if c["needs_attention"]
        )
        counts["scheduled_tasks"] = len(
            ScheduledTaskService().list(
                organization_id=ctx.organization_id, owner_id=user.id
            )
        )
        data["engines"] = _engines(ctx)
    if ctx.can(APIKeyPermission.SKILLS_READ):
        counts["skills"] = len(SkillService().list_skills(user, ctx.organization_id))
    data["counts"] = counts
    data["api_key_permissions"] = sorted(ctx.principal.permissions or [])
    return data


@tool(
    "opencuria_context",
    title="Show OpenCuria context",
    permission=APIKeyPermission.MCP_ACCESS,
    read_only=True,
    schema=obj({}),
    description="""
        Start here. Returns the signed-in user, the active organization and the
        user's role in it (organization skills and plugins need the 'admin'
        role), counts of workspaces, chats (running / waiting for input),
        scheduled tasks and skills, the chat engines with their connection
        status, and the permissions of this API key (a tool you cannot see
        needs a permission the key lacks). Agents running inside an OpenCuria
        workspace also get current_workspace_id, the workspace they run in.
    """,
)
async def opencuria_context(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(_context)(ctx)


def _models(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    engine = args.get("engine", "native")
    try:
        models = EngineCatalogService().list_models(
            engine, ctx.organization_id, ctx.user.id
        )
    except ProviderError as exc:
        raise ToolError(
            "The model catalog is temporarily unavailable; check the provider "
            "account in OpenCuria settings or retry later."
        ) from exc
    except (AuthenticationError, NotFoundError) as exc:
        raise ToolError(
            f"The '{engine}' engine is not connected for this user. Connect a "
            "provider account in OpenCuria settings first (see opencuria_context)."
        ) from exc
    query = (args.get("query") or "").strip().casefold()
    rows = [
        compact(
            {
                "model": m.id,
                "name": m.name,
                "provider": m.provider,
                "reasoning_efforts": list(m.reasoning_efforts),
                "default_effort": m.default_effort,
                "context_length": m.context_length or None,
            }
        )
        for m in models
        if not query
        or query in m.id.casefold()
        or query in (m.name or "").casefold()
        or query in (m.provider or "").casefold()
    ]
    page, meta = paginate(
        rows, limit=args.get("limit", 30), offset=args.get("offset", 0)
    )
    return {"engine": engine, "models": page, **meta}


@tool(
    "model_list",
    title="List models",
    permission=APIKeyPermission.HARNESS_READ,
    read_only=True,
    schema=obj(
        {
            "engine": enum(
                ENGINES,
                "Chat engine: 'native' (OpenCuria agent, uses the organization's "
                "provider accounts), 'claude' (Claude Agent) or 'codex'.",
                default="native",
            ),
            "query": string(
                "Optional case-insensitive filter on model ID, name or provider, "
                "e.g. 'sonnet' or 'openai'."
            ),
            "limit": limit(30, 200),
            "offset": OFFSET,
        }
    ),
    description="""
        List the models a chat engine can use, with their reasoning effort
        levels. Use the returned 'model' value as the model argument of
        chat_start, chat_send or scheduled_task_create. Omitting the model
        there uses the engine's default, which is usually what you want.
        Native catalogs can be long: filter with query.
    """,
)
async def model_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(_models)(ctx, args)
