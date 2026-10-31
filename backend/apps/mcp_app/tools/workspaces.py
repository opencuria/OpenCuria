"""Workspace tools: discover, inspect, create, configure and power workspaces."""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from asgiref.sync import sync_to_async

from apps.credentials.services import CredentialSvc
from apps.harness.conversation_service import ConversationService
from apps.plugins.services import PluginService
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.services import get_runner_service
from apps.runners.services.image_definitions import ImageDefinitionService
from apps.runners.services.image_versions import ImageVersionService
from apps.runners.services.workspace_reads import WorkspaceReadService
from apps.scheduled_tasks.services import ScheduledTaskService

from .. import resolve
from ..context import ToolContext
from ..errors import ToolError
from ..registry import ToolSpec, registrar
from ..results import compact, paginate, timestamp
from ..schema import (
    OFFSET,
    enum,
    integer,
    limit,
    obj,
    string,
    string_list,
)

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

#: Image recipe states hidden from new workspaces (mirrors the webapp).
_HIDDEN_DEFINITION_STATUSES = frozenset(
    {"deleted", "pending_deletion", "deleting", "delete_failed", "deactivated"}
)
_WAIT_POLL_SECONDS = 2.0
#: States in which workspace_start has nothing to do but wait.
_STARTABLE_OR_STARTING = frozenset({"running", "stopped", "creating", "resuming"})

WORKSPACE_REF = string("Workspace ID or exact workspace name.")

WAIT_FOR_RUNNING = integer(
    "Seconds to wait until the workspace is running before returning "
    "(0 = return immediately, max 120).",
    minimum=0,
    maximum=120,
    default=0,
)

RESOURCES = {
    "vcpus": integer("Virtual CPUs (runner default when omitted).", minimum=1),
    "memory_mb": integer("Memory in MB, e.g. 4096 (runner default).", minimum=256),
    "disk_gb": integer("Disk size in GB (runner default).", minimum=1),
}


# ---------------------------------------------------------------------------
# Presenters
# ---------------------------------------------------------------------------


def _image_ref(workspace: Any) -> dict[str, Any] | None:
    """Image name, version and whether a newer version exists."""
    ref = ImageVersionService().version_ref(workspace.base_image_instance)
    if ref is None:
        return None
    return compact(
        {
            "name": ref["name"],
            "version": ref["version"],
            "update_available": ref["update_available"] or None,
        }
    )


def _summary(workspace: Any) -> dict[str, Any]:
    """Compact list entry for a workspace."""
    summary = WorkspaceReadService().summary(workspace)
    return compact(
        {
            "workspace_id": workspace.id,
            "name": workspace.name,
            "status": str(workspace.status),
            "operation": workspace.active_operation or None,
            "agent_running": bool(
                getattr(workspace, "has_active_harness_session", False)
            ),
            "runner_online": summary["runner_online"],
            "image": summary["base_image_name"],
            "needs_intervention": workspace.intervention_required or None,
            "last_activity_at": workspace.last_activity_at,
        }
    )


def _details(ctx: ToolContext, workspace: Any) -> dict[str, Any]:
    """Full workspace view with attachments, chats and schedules."""
    data = _summary(workspace)
    runner = workspace.runner
    data.update(
        compact(
            {
                "lifecycle_diagnostic": workspace.lifecycle_diagnostic or None,
                "runner": compact(
                    {"runner_id": runner.id, "name": runner.name or None}
                ),
                "image": _image_ref(workspace),
                "resources": compact(
                    {
                        "vcpus": workspace.qemu_vcpus,
                        "memory_mb": workspace.qemu_memory_mb,
                        "disk_gb": workspace.qemu_disk_size_gb,
                    }
                ),
                "desktop": f"{workspace.desktop_width}x{workspace.desktop_height}",
                "repos": list(getattr(workspace, "repos", None) or []),
                "created_at": workspace.created_at,
                "credentials": [
                    {
                        "credential_id": credential.id,
                        "name": credential.name,
                        "type": credential.service.credential_type,
                    }
                    for credential in CredentialSvc().list_workspace_credentials(
                        workspace.id
                    )
                    if credential.service.credential_type != "provider_secret"
                ],
            }
        )
    )
    data["plugins"] = [
        compact(
            {
                "plugin_id": plugin["id"],
                "name": plugin["name"],
                "enabled": plugin["workspace_enabled"],
                "ready": plugin["ready"],
                "missing_credentials": [
                    gap.get("service_name") or gap.get("key")
                    for gap in plugin["missing_required_credentials"]
                ],
            }
        )
        for plugin in PluginService().list_workspace_plugins(
            workspace=workspace, org_id=ctx.organization_id
        )
    ]
    chats = ConversationService().list_conversations(
        organization_id=ctx.organization_id, workspace_ids=[workspace.id]
    )
    data["recent_chats"] = [
        compact(
            {
                "chat_id": chat["session_id"],
                "title": chat["title"],
                "status": "running" if chat["status"] == "busy" else "idle",
                "waiting_for_input": chat["needs_attention"] or None,
                "last_message_at": timestamp(chat["last_message_at"]),
            }
        )
        for chat in chats[:5]
    ]
    data["total_chats"] = len(chats)
    data["scheduled_tasks"] = [
        compact(
            {
                "scheduled_task_id": task.id,
                "name": task.name,
                "status": "active"
                if task.enabled
                else "ended"
                if task.recurrence == "once"
                else "paused",
                "next_run_at": task.next_run_at if task.enabled else None,
            }
        )
        for task in ScheduledTaskService().list(
            organization_id=ctx.organization_id, owner_id=ctx.user.id
        )
        if task.workspace_id == workspace.id
    ]
    return data


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def _selectable_images(ctx: ToolContext) -> list[dict[str, Any]]:
    """Images a new workspace can start from right now, with their runners."""
    images: list[dict[str, Any]] = []
    definitions = ImageDefinitionService(get_runner_service())
    for definition in definitions.list_visible(ctx.user, ctx.organization_id):
        if not definition.is_active or definition.status in _HIDDEN_DEFINITION_STATUSES:
            continue
        targets = []
        for build in definitions.list_builds(
            ctx.user, ctx.organization_id, definition.id
        ):
            artifact = build.current_generation
            if (
                build.status != "active"
                or artifact is None
                or build.runner.status != RunnerStatus.ONLINE
            ):
                continue
            targets.append(
                {
                    "runner_id": build.runner_id,
                    "runner": build.runner.name or str(build.runner_id)[:8],
                    "artifact_id": artifact.id,
                }
            )
        if targets:
            images.append(
                {
                    "image_id": definition.id,
                    "name": definition.name,
                    "kind": "organization",
                    "description": definition.description,
                    "targets": targets,
                }
            )
    for view in ImageVersionService().list_captured_images(
        ctx.user, ctx.organization_id
    ):
        line, latest = view["line"], view["latest"]
        if (
            line.status != "active"
            or latest is None
            or latest.status != "ready"
            or line.runner.status != RunnerStatus.ONLINE
        ):
            continue
        images.append(
            {
                "image_id": line.id,
                "name": line.name,
                "kind": "captured",
                "description": f"Captured image, version {latest.generation}",
                "targets": [
                    {
                        "runner_id": line.runner_id,
                        "runner": line.runner.name or str(line.runner_id)[:8],
                        "artifact_id": latest.id,
                    }
                ],
            }
        )
    return images


def _image_option(image: dict[str, Any]) -> dict[str, Any]:
    """Model-facing image entry (internal artifact IDs omitted)."""
    return compact(
        {
            "image_id": image["image_id"],
            "name": image["name"],
            "kind": image["kind"],
            "description": image["description"],
            "runners": [target["runner"] for target in image["targets"]],
        }
    )


def _pick_image(ctx: ToolContext, ref: str | None, runner_ref: str | None):
    """Return ``(image, target)`` for workspace creation."""
    images = _selectable_images(ctx)
    if not images:
        raise ToolError(
            "No image is ready on an online runner. Ask the user to build an "
            "image or start a runner in OpenCuria."
        )
    if ref:
        image = resolve.pick(
            images,
            ref,
            kind="image",
            list_tool="workspace_options",
            id_of=lambda i: i["image_id"],
            names_of=lambda i: [i["name"]],
        )
    elif len(images) == 1:
        image = images[0]
    else:
        names = ", ".join(f"'{i['name']}'" for i in images[:10])
        raise ToolError(f"Several images are available; pass image as one of {names}.")
    targets = image["targets"]
    if runner_ref:
        target = resolve.pick(
            targets,
            runner_ref,
            kind="runner",
            list_tool="workspace_options",
            id_of=lambda t: t["runner_id"],
            names_of=lambda t: [t["runner"]],
        )
    else:
        target = targets[0]
    return image, target


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    workspaces = resolve.list_workspaces(ctx)
    query = (args.get("query") or "").strip().casefold()
    status = args.get("status")
    rows = [
        _summary(w)
        for w in workspaces
        if (not query or query in (w.name or "").casefold())
        and (not status or str(w.status) == status)
    ]
    page, meta = paginate(
        rows, limit=args.get("limit", 50), offset=args.get("offset", 0)
    )
    return {"workspaces": page, **meta}


@tool(
    "workspace_list",
    title="List workspaces",
    read_only=True,
    schema=obj(
        {
            "query": string("Optional case-insensitive filter on the workspace name."),
            "status": enum(
                ["creating", "running", "stopped", "failed"],
                "Optional status filter.",
            ),
            "limit": limit(50, 200),
            "offset": OFFSET,
        }
    ),
    description="""
        List the user's workspaces (isolated VMs in which agents work), newest
        first, with status (creating, running, stopped, failed), whether an
        agent is currently working in it (agent_running) and the base image.
        Use workspace_get for attached plugins, credentials, chats and schedules.
    """,
)
async def workspace_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(_list)(ctx, args)


@tool(
    "workspace_get",
    title="Get workspace details",
    read_only=True,
    schema=obj({"workspace": WORKSPACE_REF}, ("workspace",)),
    description="""
        Get one workspace in detail: status and lifecycle diagnostics, runner,
        image version (update_available), resources, attached credentials,
        plugins with readiness (missing credentials), the 5 most recent chats
        and its scheduled tasks.
    """,
)
async def workspace_get(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        return _details(ctx, resolve.workspace(ctx, args["workspace"]))

    return await sync_to_async(build)()


def _options(ctx: ToolContext) -> dict[str, Any]:
    data: dict[str, Any] = {}
    data["images"] = [_image_option(image) for image in _selectable_images(ctx)]
    data["runners"] = [
        {
            "runner_id": runner.id,
            "name": runner.name or str(runner.id)[:8],
            "online": runner.status == RunnerStatus.ONLINE,
            "defaults": {
                "vcpus": runner.qemu_default_vcpus,
                "memory_mb": runner.qemu_default_memory_mb,
                "disk_gb": runner.qemu_default_disk_size_gb,
            },
            "limits": {
                "vcpus": [runner.qemu_min_vcpus, runner.qemu_max_vcpus],
                "memory_mb": [runner.qemu_min_memory_mb, runner.qemu_max_memory_mb],
                "disk_gb": [
                    runner.qemu_min_disk_size_gb,
                    runner.qemu_max_disk_size_gb,
                ],
            },
        }
        for runner in get_runner_service().list_runners(
            organization_id=ctx.organization_id
        )
    ]
    data["credentials"] = [
        {
            "credential_id": credential.id,
            "name": credential.name,
            "service": credential.service.name,
            "type": credential.service.credential_type,
            "scope": "personal" if credential.user_id else "organization",
        }
        for credential in resolve.list_credentials(ctx)
    ]
    data["plugins"] = [
        compact(
            {
                "plugin_id": plugin["id"],
                "name": plugin["name"],
                "description": plugin["description"],
                "required_credentials": [
                    requirement["service_name"]
                    for requirement in plugin["credential_requirements"]
                    if requirement["required"]
                ],
            }
        )
        for plugin in PluginService().list_visible(org_id=ctx.organization_id)
        if plugin["org_enabled"] and plugin["enabled"] and plugin["published"]
    ]
    return data


@tool(
    "workspace_options",
    title="List workspace options",
    read_only=True,
    schema=obj({}),
    description="""
        List the choices for workspace_create and workspace_update: images that
        are ready on an online runner, runners with resource defaults and
        limits, credentials the user can attach (API tokens, SSH keys, env
        files; names only, never secrets) and the plugins enabled for the
        organization with the credentials they require.
    """,
)
async def workspace_options(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(_options)(ctx)


async def _wait_until_running(
    ctx: ToolContext, workspace_id: uuid.UUID, seconds: int
) -> Any:
    """Poll until the workspace runs, fails or *seconds* elapse."""

    def load() -> Any:
        return resolve.workspace(ctx, str(workspace_id))

    deadline = time.monotonic() + seconds
    workspace = await sync_to_async(load)()
    while (
        str(workspace.status) not in {WorkspaceStatus.RUNNING, WorkspaceStatus.FAILED}
        or workspace.active_operation
    ) and time.monotonic() < deadline:
        await asyncio.sleep(_WAIT_POLL_SECONDS)
        workspace = await sync_to_async(load)()
    return workspace


async def _status_result(
    ctx: ToolContext, workspace_id: uuid.UUID, wait_seconds: int, pending_hint: str
) -> dict[str, Any]:
    """Return the workspace summary after an optional wait, with a next step."""
    if wait_seconds:
        workspace = await _wait_until_running(ctx, workspace_id, wait_seconds)
    else:
        workspace = await sync_to_async(resolve.workspace)(ctx, str(workspace_id))
    data = await sync_to_async(_summary)(workspace)
    if (
        str(workspace.status) == WorkspaceStatus.RUNNING
        and not workspace.active_operation
    ):
        data["next_step"] = "The workspace is running; chats can be started."
    elif str(workspace.status) == WorkspaceStatus.FAILED:
        data["next_step"] = (
            "The workspace failed. Check lifecycle_diagnostic with workspace_get."
        )
    else:
        data["next_step"] = pending_hint
    return data


@tool(
    "workspace_create",
    title="Create workspace",
    schema=obj(
        {
            "name": string("Workspace name, e.g. 'billing-service'.", minLength=1),
            "image": string(
                "Image ID or name from workspace_options. Optional when exactly "
                "one image is available."
            ),
            "runner": string(
                "Runner ID or name; defaults to the first online runner that has "
                "the image."
            ),
            "repos": string_list(
                "Git repository URLs to clone into /workspace, e.g. "
                "['git@github.com:acme/api.git']. Private repos need an SSH key "
                "or token credential."
            ),
            "credentials": string_list(
                "Credential IDs or names to attach (see workspace_options)."
            ),
            "plugins": string_list("Plugin IDs, slugs or names to enable."),
            **RESOURCES,
            "wait_seconds": WAIT_FOR_RUNNING,
        },
        ("name",),
    ),
    description="""
        Create a new workspace VM from an image. Provisioning runs in the
        background and usually takes one to a few minutes; pass wait_seconds
        to wait for it, or poll workspace_get until status is 'running'.
        Call workspace_options first to see images, credentials and plugins.
    """,
)
async def workspace_create(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def plan() -> dict[str, Any]:
        image, target = _pick_image(ctx, args.get("image"), args.get("runner"))
        credential_ids = resolve.credential_ids(ctx, args.get("credentials") or [])
        return {
            "image": image,
            "target": target,
            "plugin_ids": resolve.plugin_ids(ctx, args.get("plugins") or []),
            "credential_ids": credential_ids,
        }

    prepared = await sync_to_async(plan)()
    workspace, _task = await get_runner_service().create_workspace(
        name=args["name"].strip(),
        repos=args.get("repos") or [],
        qemu_vcpus=args.get("vcpus"),
        qemu_memory_mb=args.get("memory_mb"),
        qemu_disk_size_gb=args.get("disk_gb"),
        credential_ids=prepared["credential_ids"],
        runner_id=prepared["target"]["runner_id"],
        image_artifact_id=prepared["target"]["artifact_id"],
        user=ctx.user,
        organization_id=ctx.organization_id,
        plugin_ids=prepared["plugin_ids"],
    )
    data = await _status_result(
        ctx,
        workspace.id,
        args.get("wait_seconds", 0),
        "Provisioning continues in the background; call workspace_get (or "
        "workspace_start with wait_seconds) until status is 'running'.",
    )
    data["image"] = prepared["image"]["name"]
    data["runner"] = prepared["target"]["runner"]
    return data


@tool(
    "workspace_update",
    title="Update workspace",
    idempotent=True,
    schema=obj(
        {
            "workspace": WORKSPACE_REF,
            "name": string("New workspace name.", minLength=1),
            "plugins": string_list(
                "Complete list of plugins to enable (replaces the current "
                "selection; [] disables all). To toggle one plugin use "
                "plugin_set_active instead."
            ),
            "credentials": string_list(
                "Complete list of credentials to attach (replaces the current "
                "selection; [] detaches all)."
            ),
            **RESOURCES,
            "desktop_width": integer("Desktop width in pixels.", minimum=640),
            "desktop_height": integer("Desktop height in pixels.", minimum=480),
        },
        ("workspace",),
    ),
    description="""
        Rename a workspace, replace its enabled plugins or attached credentials,
        or change its resources and desktop size. Only the fields you pass
        change. Resource changes apply on the next start; credential changes
        are synced into a running workspace.
    """,
)
async def workspace_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    changes = set(args) - {"workspace"}
    if not changes:
        raise ToolError("Pass at least one field to change besides workspace.")

    def plan() -> dict[str, Any]:
        workspace = resolve.workspace(ctx, args["workspace"])
        credential_ids = (
            resolve.credential_ids(ctx, args["credentials"])
            if "credentials" in args
            else None
        )
        plugin_ids = (
            resolve.plugin_ids(ctx, args["plugins"]) if "plugins" in args else None
        )
        return {
            "workspace": workspace,
            "credential_ids": credential_ids,
            "plugin_ids": plugin_ids,
        }

    prepared = await sync_to_async(plan)()
    updated = await get_runner_service().update_workspace(
        prepared["workspace"].id,
        name=args.get("name"),
        credential_ids=prepared["credential_ids"],
        qemu_vcpus=args.get("vcpus"),
        qemu_memory_mb=args.get("memory_mb"),
        qemu_disk_size_gb=args.get("disk_gb"),
        desktop_width=args.get("desktop_width"),
        desktop_height=args.get("desktop_height"),
        plugin_ids=prepared["plugin_ids"],
        user=ctx.user,
        organization_id=ctx.organization_id,
    )

    def result() -> dict[str, Any]:
        data = _details(ctx, resolve.workspace(ctx, str(updated.id)))
        sync_status = getattr(updated, "credential_sync_status", "not_required")
        if sync_status not in ("", "not_required", "synced"):
            data["credential_sync"] = compact(
                {
                    "status": sync_status,
                    "detail": getattr(updated, "credential_sync_detail", None),
                }
            )
        data["updated_fields"] = sorted(changes)
        return data

    return await sync_to_async(result)()


@tool(
    "workspace_start",
    title="Start workspace",
    idempotent=True,
    schema=obj(
        {"workspace": WORKSPACE_REF, "wait_seconds": WAIT_FOR_RUNNING}, ("workspace",)
    ),
    description="""
        Start (resume) a stopped workspace. Chats need a running workspace.
        Starting takes a few seconds to a minute; pass wait_seconds to wait
        until it runs. Starting an already running workspace is a no-op.
    """,
)
async def workspace_start(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    workspace = await sync_to_async(resolve.workspace)(ctx, args["workspace"])
    status = str(workspace.status)
    if status == WorkspaceStatus.STOPPED and not workspace.active_operation:
        await get_runner_service().resume_workspace(workspace.id)
    elif status not in _STARTABLE_OR_STARTING:
        raise ToolError(
            f"Workspace '{workspace.name}' is {workspace.status} and cannot be "
            "started now. Check it with workspace_get."
        )
    return await _status_result(
        ctx,
        workspace.id,
        args.get("wait_seconds", 0),
        "The workspace is starting; call workspace_get or workspace_start with "
        "wait_seconds until status is 'running'.",
    )


@tool(
    "workspace_stop",
    title="Stop workspace",
    idempotent=True,
    schema=obj({"workspace": WORKSPACE_REF}, ("workspace",)),
    description="""
        Stop a running workspace to free runner resources. Files on its disk
        are kept; running agents and processes are interrupted. Stopping an
        already stopped workspace is a no-op.
    """,
)
async def workspace_stop(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    workspace = await sync_to_async(resolve.workspace)(ctx, args["workspace"])
    if str(workspace.status) != WorkspaceStatus.STOPPED:
        await get_runner_service().stop_workspace(workspace.id)
    workspace = await sync_to_async(resolve.workspace)(ctx, str(workspace.id))
    return await sync_to_async(_summary)(workspace)


@tool(
    "workspace_delete",
    title="Delete workspace",
    destructive=True,
    schema=obj({"workspace": WORKSPACE_REF}, ("workspace",)),
    description="""
        Permanently delete a workspace and its disk; its chats and scheduled
        tasks go with it. This cannot be undone; confirm with the user first
        unless they asked for it explicitly.
    """,
)
async def workspace_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    workspace = await sync_to_async(resolve.workspace)(ctx, args["workspace"])
    await get_runner_service().remove_workspace(workspace.id)
    return {
        "workspace_id": workspace.id,
        "name": workspace.name,
        "deleted": True,
        "note": "Deletion runs in the background; the workspace disappears from "
        "workspace_list once the runner confirms it.",
    }
