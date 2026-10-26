"""Skill tools: reusable Markdown instructions for agents."""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from apps.skills.services import SkillService

from .. import resolve
from ..context import ToolContext
from ..registry import ToolSpec, registrar
from ..results import compact, paginate, preview
from ..schema import OFFSET, enum, limit, obj, string

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

SKILL_REF = string("Skill ID or exact skill name.")
SKILL_BODY = string(
    "Markdown instructions the agent follows when the skill is selected, e.g. "
    "coding conventions, a review checklist or a release procedure. Write "
    "them as direct instructions; the skill name becomes the heading.",
    minLength=1,
)


def _scope(skill: Any) -> str:
    return "organization" if skill.organization_id else "personal"


def _summary(skill: Any) -> dict[str, Any]:
    return {
        "skill_id": skill.id,
        "name": skill.name,
        "scope": _scope(skill),
        "preview": preview(skill.body),
        "characters": len(skill.body or ""),
        "updated_at": skill.updated_at,
    }


def _full(skill: Any) -> dict[str, Any]:
    return compact(
        {
            "skill_id": skill.id,
            "name": skill.name,
            "scope": _scope(skill),
            "body": skill.body,
            "created_by": skill.created_by.email if skill.created_by else None,
            "created_at": skill.created_at,
            "updated_at": skill.updated_at,
        }
    )


@tool(
    "skill_list",
    title="List skills",
    read_only=True,
    schema=obj(
        {
            "query": string(
                "Optional case-insensitive filter on skill name and content."
            ),
            "scope": enum(
                ["personal", "organization"],
                "Optional filter: the user's own skills or skills shared with the "
                "organization.",
            ),
            "limit": limit(50, 200),
            "offset": OFFSET,
        }
    ),
    description="""
        List the skills available to the user: personal skills and skills
        shared with the organization, with a one-line preview. Skills are
        reusable Markdown instructions that can be selected for a chat
        (chat_start skills) or a scheduled task. Read the full text with
        skill_get.
    """,
)
async def skill_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        query = (args.get("query") or "").strip().casefold()
        rows = [
            _summary(skill)
            for skill in SkillService().list_skills(ctx.user, ctx.organization_id)
            if (not args.get("scope") or _scope(skill) == args["scope"])
            and (
                not query
                or query in skill.name.casefold()
                or query in (skill.body or "").casefold()
            )
        ]
        page, meta = paginate(
            rows, limit=args.get("limit", 50), offset=args.get("offset", 0)
        )
        return {"skills": page, **meta}

    return await sync_to_async(build)()


@tool(
    "skill_get",
    title="Get skill",
    read_only=True,
    schema=obj({"skill": SKILL_REF}, ("skill",)),
    description="Get the full Markdown body and metadata of one skill.",
)
async def skill_get(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(lambda: _full(resolve.skill(ctx, args["skill"])))()


@tool(
    "skill_create",
    title="Create skill",
    schema=obj(
        {
            "name": string(
                "Short descriptive name, e.g. 'Python style guide'.", minLength=1
            ),
            "body": SKILL_BODY,
            "scope": enum(
                ["personal", "organization"],
                "'personal' (default): only this user sees it. 'organization': "
                "shared with all members (admins only).",
                default="personal",
            ),
        },
        ("name", "body"),
    ),
    description="""
        Create a skill: reusable Markdown instructions that agents follow when
        the skill is selected for a chat or scheduled task. Check skill_list
        first to avoid duplicates; update an existing skill with skill_update.
    """,
)
async def skill_create(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def create() -> dict[str, Any]:
        service = SkillService()
        if args.get("scope") == "organization":
            ctx.require_admin("create organization skills")
            skill = service.create_org_skill(
                name=args["name"],
                body=args["body"],
                org_id=ctx.organization_id,
                user=ctx.user,
            )
        else:
            skill = service.create_personal_skill(
                name=args["name"], body=args["body"], user=ctx.user
            )
        return _full(skill)

    return await sync_to_async(create)()


@tool(
    "skill_update",
    title="Update skill",
    idempotent=True,
    schema=obj(
        {
            "skill": SKILL_REF,
            "name": string("New name.", minLength=1),
            "body": string(
                "New complete Markdown body (replaces the old body; read it with "
                "skill_get first to make partial edits).",
                minLength=1,
            ),
        },
        ("skill",),
    ),
    description="""
        Rename a skill or replace its Markdown body. Personal skills can be
        changed by their owner, organization skills only by admins. Chats and
        schedules that use the skill pick up the new text on their next turn.
    """,
)
async def skill_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def update() -> dict[str, Any]:
        skill = resolve.skill(ctx, args["skill"])
        updated = SkillService().update_skill(
            skill.id,
            name=args.get("name"),
            body=args.get("body"),
            user=ctx.user,
            org_id=ctx.organization_id,
            is_admin=ctx.is_admin,
        )
        return _full(updated)

    return await sync_to_async(update)()


@tool(
    "skill_delete",
    title="Delete skill",
    destructive=True,
    schema=obj({"skill": SKILL_REF}, ("skill",)),
    description="""
        Permanently delete a skill. Chats and scheduled tasks that selected it
        simply stop using it.
    """,
)
async def skill_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def delete() -> dict[str, Any]:
        skill = resolve.skill(ctx, args["skill"])
        SkillService().delete_skill(
            skill.id,
            user=ctx.user,
            org_id=ctx.organization_id,
            is_admin=ctx.is_admin,
        )
        return {"skill_id": skill.id, "name": skill.name, "deleted": True}

    return await sync_to_async(delete)()
