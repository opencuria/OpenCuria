"""Resolve model-supplied references (ID or name) to owner-scoped objects.

Models refer to things the way users do, by name. Every resolver accepts an
ID or an exact (case-insensitive) name, is scoped to the caller's
organization and ownership, and fails with a message that lists the closest
matches and the tool that lists all candidates. All functions are
synchronous; call them from ``sync_to_async`` blocks.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from apps.credentials.services import CredentialSvc
from apps.plugins.services import PluginService
from apps.runners.services import get_runner_service
from apps.scheduled_tasks.services import ScheduledTaskService
from apps.skills.services import SkillService

from .context import ToolContext
from .errors import ToolError

T = TypeVar("T")

#: Workspace states that no longer exist for the user.
GONE_WORKSPACE_STATUSES = frozenset({"deleted", "removed"})


def parse_uuid(value: str) -> uuid.UUID | None:
    """Return *value* as UUID, or ``None`` when it is not one."""
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None


def pick(
    items: Iterable[T],
    ref: str,
    *,
    kind: str,
    list_tool: str,
    id_of: Callable[[T], Any],
    names_of: Callable[[T], Iterable[str]],
) -> T:
    """Return the single item whose ID or name matches *ref*.

    Raises:
        ToolError: When nothing or more than one item matches.
    """
    candidates = list(items)
    needle = str(ref or "").strip()
    if not needle:
        raise ToolError(f"A {kind} reference is required (ID or name).")
    wanted_id = parse_uuid(needle)
    if wanted_id is not None:
        matches = [item for item in candidates if str(id_of(item)) == str(wanted_id)]
    else:
        folded = needle.casefold()
        matches = [
            item
            for item in candidates
            if any(str(name).casefold() == folded for name in names_of(item) if name)
        ]
    if len(matches) == 1:
        return matches[0]

    def label(item: T) -> str:
        name = next((str(n) for n in names_of(item) if n), "")
        return f"'{name}' ({id_of(item)})"

    if matches:
        listed = ", ".join(label(item) for item in matches[:10])
        raise ToolError(
            f"{len(matches)} {kind}s match '{needle}': {listed}. "
            f"Pass the {kind} ID instead."
        )
    folded = needle.casefold()
    similar = [
        item
        for item in candidates
        if any(folded in str(name).casefold() for name in names_of(item) if name)
    ]
    shown = similar or candidates
    if not shown:
        raise ToolError(f"No {kind} '{needle}' found; there are no {kind}s yet.")
    listed = ", ".join(label(item) for item in shown[:8])
    prefix = "Did you mean" if similar else "Available"
    raise ToolError(
        f"No {kind} '{needle}' found. {prefix}: {listed}. "
        f"Call {list_tool} to see all {kind}s."
    )


# ---------------------------------------------------------------------------
# Domain resolvers
# ---------------------------------------------------------------------------


def list_workspaces(ctx: ToolContext) -> list[Any]:
    """Return the caller's existing workspaces in the organization, newest first."""
    return [
        workspace
        for workspace in get_runner_service().list_workspaces(
            organization_id=ctx.organization_id, user=ctx.user
        )
        if str(workspace.status) not in GONE_WORKSPACE_STATUSES
    ]


def workspace(ctx: ToolContext, ref: str) -> Any:
    """Resolve an owned workspace by ID or name."""
    return pick(
        list_workspaces(ctx),
        ref,
        kind="workspace",
        list_tool="workspace_list",
        id_of=lambda w: w.id,
        names_of=lambda w: [w.name],
    )


def skill(ctx: ToolContext, ref: str) -> Any:
    """Resolve a skill visible to the caller by ID or name."""
    return pick(
        SkillService().list_skills(ctx.user, ctx.organization_id),
        ref,
        kind="skill",
        list_tool="skill_list",
        id_of=lambda s: s.id,
        names_of=lambda s: [s.name],
    )


def skill_ids(ctx: ToolContext, refs: list[str] | None) -> list[str]:
    """Resolve skill references to their ID strings, preserving order."""
    if not refs:
        return []
    visible = SkillService().list_skills(ctx.user, ctx.organization_id)
    resolved: list[str] = []
    for ref in refs:
        found = pick(
            visible,
            ref,
            kind="skill",
            list_tool="skill_list",
            id_of=lambda s: s.id,
            names_of=lambda s: [s.name],
        )
        if str(found.id) not in resolved:
            resolved.append(str(found.id))
    return resolved


def skill_names(ctx: ToolContext, ids: Iterable[Any]) -> list[str]:
    """Map stored skill IDs to names; skills deleted since are skipped."""
    wanted = [str(value) for value in ids or []]
    if not wanted:
        return []
    by_id = {
        str(s.id): s.name
        for s in SkillService().list_skills(ctx.user, ctx.organization_id)
    }
    return [by_id[value] for value in wanted if value in by_id]


def plugin(ctx: ToolContext, ref: str) -> dict[str, Any]:
    """Resolve a visible plugin by ID, slug or name (serialized dict)."""
    return pick(
        PluginService().list_visible(org_id=ctx.organization_id),
        ref,
        kind="plugin",
        list_tool="plugin_list",
        id_of=lambda p: p["id"],
        names_of=lambda p: [p["name"], p["slug"]],
    )


def plugin_ids(ctx: ToolContext, refs: list[str]) -> list[uuid.UUID]:
    """Resolve plugin references to IDs, preserving order."""
    visible = PluginService().list_visible(org_id=ctx.organization_id)
    resolved: list[uuid.UUID] = []
    for ref in refs:
        found = pick(
            visible,
            ref,
            kind="plugin",
            list_tool="plugin_list",
            id_of=lambda p: p["id"],
            names_of=lambda p: [p["name"], p["slug"]],
        )
        if found["id"] not in resolved:
            resolved.append(found["id"])
    return resolved


def list_credentials(ctx: ToolContext) -> list[Any]:
    """Return attachable credentials, excluding control-plane secrets."""
    return [
        credential
        for credential in CredentialSvc().list_credentials(
            ctx.user, ctx.organization_id
        )
        if credential.service.credential_type != "provider_secret"
    ]


def credential_ids(ctx: ToolContext, refs: list[str]) -> list[uuid.UUID]:
    """Resolve credential references (ID or name) to IDs, preserving order."""
    available = list_credentials(ctx)
    resolved: list[uuid.UUID] = []
    for ref in refs:
        found = pick(
            available,
            ref,
            kind="credential",
            list_tool="workspace_options",
            id_of=lambda c: c.id,
            names_of=lambda c: [c.name],
        )
        if found.id not in resolved:
            resolved.append(found.id)
    return resolved


def scheduled_task(ctx: ToolContext, ref: str) -> Any:
    """Resolve one of the caller's scheduled tasks by ID or name."""
    return pick(
        ScheduledTaskService().list(
            organization_id=ctx.organization_id, owner_id=ctx.user.id
        ),
        ref,
        kind="scheduled task",
        list_tool="scheduled_task_list",
        id_of=lambda t: t.id,
        names_of=lambda t: [t.name],
    )
