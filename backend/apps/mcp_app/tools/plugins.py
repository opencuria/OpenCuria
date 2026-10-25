"""Plugin tools: bundles of skills, MCP servers and credential requirements."""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission
from apps.credentials.services import CredentialServiceSvc
from apps.plugins.services import PluginService
from apps.runners.services import get_runner_service

from .. import resolve
from ..context import ToolContext
from ..errors import ToolError
from ..registry import ToolSpec, registrar
from ..results import compact, paginate, preview
from ..schema import (
    OFFSET,
    array,
    boolean,
    enum,
    integer,
    limit,
    obj,
    string,
    string_list,
)

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

PLUGIN_REF = string("Plugin ID, slug or exact name.")

_STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}}

PLUGIN_SKILL = obj(
    {
        "name": string("Skill name.", minLength=1),
        "body": string("Markdown instructions.", minLength=1),
        "slug": string("Optional stable slug (derived from the name)."),
    },
    ("name", "body"),
)

PLUGIN_MCP_SERVER = obj(
    {
        "name": string("Server name, e.g. 'GitHub'.", minLength=1),
        "slug": string("Optional stable slug (derived from the name)."),
        "transport": enum(
            ["stdio", "streamable_http", "sse"],
            "'stdio' runs command inside the workspace; 'streamable_http' or "
            "'sse' connect to url.",
            default="stdio",
        ),
        "command": string("stdio only: one executable without arguments, e.g. 'npx'."),
        "args": string_list("stdio only: arguments, e.g. ['-y', '@acme/mcp']."),
        "cwd": string("stdio only: working directory (default /workspace)."),
        "env": {
            **_STRING_MAP,
            "description": "Environment variables; values may reference a "
            "credential requirement as {{credential.KEY}}.",
        },
        "url": string("HTTP transports only: server URL (https://…)."),
        "headers": {
            **_STRING_MAP,
            "description": "HTTP headers; values may use {{credential.KEY}}, e.g. "
            "{'Authorization': 'Bearer {{credential.GITHUB_TOKEN}}'}.",
        },
        "auth_type": enum(
            ["none", "oauth"],
            "'oauth' lets each user sign in via OAuth (HTTP transports only; set "
            "oauth_requirement_key).",
            default="none",
        ),
        "oauth_requirement_key": string(
            "Key of the OAuth credential requirement used by this server."
        ),
        "startup_timeout_seconds": integer("Default 30.", minimum=1, maximum=600),
        "request_timeout_seconds": integer("Default 60.", minimum=1, maximum=600),
    },
    ("name",),
)

PLUGIN_REQUIREMENT = obj(
    {
        "key": string(
            "Stable key referenced as {{credential.KEY}}, e.g. 'GITHUB_TOKEN'.",
            minLength=1,
        ),
        "service": string(
            "Credential service ID, slug or name (the kind of credential, e.g. "
            "'GitHub token'); users attach a matching credential to the workspace."
        ),
        "description": string("What the credential is used for."),
        "required": boolean(
            "Whether the plugin cannot work without it (default true)."
        ),
    },
    ("key", "service"),
)

COMPONENTS = {
    "description": string("What the plugin does and when to enable it."),
    "skills": array(PLUGIN_SKILL, "Skills the plugin contributes to every chat."),
    "mcp_servers": array(
        PLUGIN_MCP_SERVER, "MCP servers whose tools agents get in the workspace."
    ),
    "credential_requirements": array(
        PLUGIN_REQUIREMENT, "Credentials the plugin needs from the workspace."
    ),
}


# ---------------------------------------------------------------------------
# Presenters
# ---------------------------------------------------------------------------


def _summary(plugin: dict[str, Any]) -> dict[str, Any]:
    return compact(
        {
            "plugin_id": plugin["id"],
            "name": plugin["name"],
            "slug": plugin["slug"],
            "description": preview(plugin["description"], 200),
            "scope": "global" if plugin["is_global"] else "organization",
            "available": plugin["enabled"] and plugin["published"],
            "active_in_organization": plugin["org_enabled"],
            "skills": [skill["name"] for skill in plugin["skills"]],
            "mcp_servers": [server["name"] for server in plugin["mcp_servers"]],
            "required_credentials": [
                requirement["service_name"]
                for requirement in plugin["credential_requirements"]
                if requirement["required"]
            ],
        }
    )


def _full(plugin: dict[str, Any]) -> dict[str, Any]:
    data = _summary(plugin)
    data["description"] = plugin["description"]
    data["editable"] = not plugin["is_global"]
    data["skills"] = [
        {"name": skill["name"], "slug": skill["slug"], "body": skill["body"]}
        for skill in plugin["skills"]
    ]
    data["mcp_servers"] = [
        compact(
            {
                "name": server["name"],
                "slug": server["slug"],
                "transport": server["transport"],
                "command": server["command"],
                "args": server["args"],
                "cwd": server["cwd"] if server["transport"] == "stdio" else None,
                "env": server["env"],
                "url": server["url"],
                "headers": server["headers"],
                "auth_type": None
                if server["auth_type"] == "none"
                else server["auth_type"],
                "oauth_requirement_key": server["oauth_requirement_key"],
                "startup_timeout_seconds": server["startup_timeout_seconds"],
                "request_timeout_seconds": server["request_timeout_seconds"],
            }
        )
        for server in plugin["mcp_servers"]
    ]
    data["credential_requirements"] = [
        compact(
            {
                "key": requirement["key"],
                "service": requirement["service_name"],
                "service_slug": requirement["service_slug"],
                "description": requirement["description"],
                "required": requirement["required"],
            }
        )
        for requirement in plugin["credential_requirements"]
    ]
    return data


def _requirements(ctx: ToolContext, items: list[dict[str, Any]]) -> list[dict]:
    """Translate model-facing requirements (service by name) to service IDs."""
    services = list(CredentialServiceSvc().list_services(org_id=ctx.organization_id))
    return [
        {
            "key": item["key"],
            "description": item.get("description", ""),
            "required": item.get("required", True),
            "service_id": resolve.pick(
                services,
                item["service"],
                kind="credential service",
                list_tool="workspace_options",
                id_of=lambda s: s.id,
                names_of=lambda s: [s.name, s.slug],
            ).id,
        }
        for item in items
    ]


def _components(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Return create/update keyword arguments for the given component fields."""
    values: dict[str, Any] = {}
    if "skills" in args:
        values["skills"] = [
            {**skill, "position": index} for index, skill in enumerate(args["skills"])
        ]
    if "mcp_servers" in args:
        values["mcp_servers"] = list(args["mcp_servers"])
    if "credential_requirements" in args:
        values["credential_requirements"] = _requirements(
            ctx, args["credential_requirements"]
        )
    return values


def _editable(ctx: ToolContext, ref: str, action: str) -> dict[str, Any]:
    ctx.require_admin(f"{action} plugins")
    plugin = resolve.plugin(ctx, ref)
    if plugin["is_global"]:
        raise ToolError(
            f"'{plugin['name']}' is a global plugin and cannot be changed here; "
            "create an organization plugin instead."
        )
    return plugin


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "plugin_list",
    title="List plugins",
    permission=APIKeyPermission.PLUGINS_READ,
    read_only=True,
    schema=obj(
        {
            "workspace": string(
                "Optional workspace ID or name: list the plugins usable there with "
                "enabled/ready state and missing credentials."
            ),
            "query": string("Optional case-insensitive filter on name or description."),
            "limit": limit(50, 200),
            "offset": OFFSET,
        }
    ),
    description="""
        List plugins. A plugin bundles skills, MCP servers (extra tools for
        agents, e.g. browser automation or GitHub) and the credentials they
        need. Plugins are activated for the organization by an admin and then
        enabled per workspace. Without workspace: the catalog with
        active_in_organization. With workspace: the activated plugins with
        enabled (in that workspace), ready and missing_credentials.
    """,
)
async def plugin_list(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        query = (args.get("query") or "").strip().casefold()
        if args.get("workspace"):
            workspace = resolve.workspace(ctx, args["workspace"])
            rows = [
                compact(
                    {
                        "plugin_id": plugin["id"],
                        "name": plugin["name"],
                        "slug": plugin["slug"],
                        "description": preview(plugin["description"], 200),
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
        else:
            rows = [
                _summary(plugin)
                for plugin in PluginService().list_visible(org_id=ctx.organization_id)
            ]
        rows = [
            row
            for row in rows
            if not query
            or query in row["name"].casefold()
            or query in (row.get("description") or "").casefold()
        ]
        page, meta = paginate(
            rows, limit=args.get("limit", 50), offset=args.get("offset", 0)
        )
        return {"plugins": page, **meta}

    return await sync_to_async(build)()


@tool(
    "plugin_get",
    title="Get plugin",
    permission=APIKeyPermission.PLUGINS_READ,
    read_only=True,
    schema=obj({"plugin": PLUGIN_REF}, ("plugin",)),
    description="""
        Get a plugin's full definition: skills with their Markdown, MCP server
        configuration (credentials appear only as {{credential.KEY}}
        placeholders, never as secrets) and credential requirements.
    """,
)
async def plugin_get(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(lambda: _full(resolve.plugin(ctx, args["plugin"])))()


@tool(
    "plugin_create",
    title="Create plugin",
    permission=APIKeyPermission.PLUGINS_WRITE,
    schema=obj(
        {
            "name": string("Plugin name, e.g. 'GitHub tools'.", minLength=1),
            "slug": string("Optional URL-safe slug (derived from the name)."),
            **COMPONENTS,
            "activate": boolean(
                "Also activate the plugin for the organization so workspaces can "
                "enable it (default true).",
                default=True,
            ),
        },
        ("name",),
    ),
    description="""
        Create an organization plugin (admins only) from skills, MCP servers
        and credential requirements. Secrets never go into the definition:
        declare a credential requirement with a key and reference it as
        {{credential.KEY}} in env or headers; each workspace supplies a
        matching credential. Enable it for a workspace afterwards with
        plugin_set_active.
    """,
)
async def plugin_create(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def create() -> dict[str, Any]:
        ctx.require_admin("create plugins")
        service = PluginService()
        components = _components(ctx, args)
        plugin = service.create_org_plugin(
            org_id=ctx.organization_id,
            user=ctx.user,
            name=args["name"],
            slug=args.get("slug", ""),
            description=args.get("description", ""),
            skills=components.get("skills"),
            mcp_servers=components.get("mcp_servers"),
            credential_requirements=components.get("credential_requirements"),
        )
        if args.get("activate", True):
            plugin = service.set_org_activation(
                plugin["id"], org_id=ctx.organization_id, user=ctx.user, active=True
            )
        return _full(plugin)

    return await sync_to_async(create)()


@tool(
    "plugin_update",
    title="Update plugin",
    permission=APIKeyPermission.PLUGINS_WRITE,
    idempotent=True,
    schema=obj(
        {
            "plugin": PLUGIN_REF,
            "name": string("New name.", minLength=1),
            "slug": string("New slug."),
            **COMPONENTS,
            "available": boolean(
                "Publish (true) or withdraw (false) the plugin definition."
            ),
        },
        ("plugin",),
    ),
    description="""
        Change an organization plugin (admins only; global plugins are
        read-only). Only the fields you pass change; skills, mcp_servers and
        credential_requirements each replace the whole list, so read the
        current definition with plugin_get first and send the edited list.
    """,
)
async def plugin_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def update() -> dict[str, Any]:
        plugin = _editable(ctx, args["plugin"], "change")
        components = _components(ctx, args)
        available = args.get("available")
        updated = PluginService().update_org_plugin(
            plugin["id"],
            org_id=ctx.organization_id,
            user=ctx.user,
            name=args.get("name"),
            slug=args.get("slug"),
            description=args.get("description"),
            enabled=available,
            published=available,
            skills=components.get("skills"),
            mcp_servers=components.get("mcp_servers"),
            credential_requirements=components.get("credential_requirements"),
        )
        return _full(updated)

    return await sync_to_async(update)()


@tool(
    "plugin_delete",
    title="Delete plugin",
    permission=APIKeyPermission.PLUGINS_WRITE,
    destructive=True,
    schema=obj({"plugin": PLUGIN_REF}, ("plugin",)),
    description="""
        Permanently delete an organization plugin (admins only). Workspaces
        lose its skills and MCP servers; credentials are kept.
    """,
)
async def plugin_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def delete() -> dict[str, Any]:
        plugin = _editable(ctx, args["plugin"], "delete")
        PluginService().delete_org_plugin(plugin["id"], org_id=ctx.organization_id)
        return {"plugin_id": plugin["id"], "name": plugin["name"], "deleted": True}

    return await sync_to_async(delete)()


@tool(
    "plugin_set_active",
    title="Enable or disable plugin",
    permission=APIKeyPermission.PLUGINS_WRITE,
    idempotent=True,
    schema=obj(
        {
            "plugin": PLUGIN_REF,
            "active": boolean("true to enable, false to disable."),
            "workspace": string(
                "Workspace ID or name to enable/disable the plugin in. Omit to "
                "activate/deactivate it for the whole organization (admins only)."
            ),
        },
        ("plugin", "active"),
    ),
    description="""
        Enable or disable a plugin, either in one workspace (pass workspace;
        its agents get the plugin's skills and MCP tools from their next turn)
        or for the whole organization (omit workspace; admins only, required
        before workspaces can enable it). Enabling in a workspace fails with
        the missing credentials listed if the workspace lacks a required
        credential; attach one with workspace_update.
    """,
)
async def plugin_set_active(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    active = bool(args["active"])
    if not args.get("workspace"):

        def set_org() -> dict[str, Any]:
            ctx.require_admin("activate plugins for the organization")
            plugin = resolve.plugin(ctx, args["plugin"])
            updated = PluginService().set_org_activation(
                plugin["id"], org_id=ctx.organization_id, user=ctx.user, active=active
            )
            return {"scope": "organization", **_summary(updated)}

        return await sync_to_async(set_org)()

    ctx.require(APIKeyPermission.WORKSPACES_UPDATE)

    def plan() -> tuple[Any, dict[str, Any], list[Any]]:
        workspace = resolve.workspace(ctx, args["workspace"])
        plugin = resolve.plugin(ctx, args["plugin"])
        selected = list(PluginService().workspace_plugin_ids(workspace.id))
        if active and plugin["id"] not in selected:
            if not plugin["org_enabled"]:
                raise ToolError(
                    f"'{plugin['name']}' is not activated for the organization. "
                    "An admin must activate it first (plugin_set_active without "
                    "workspace)."
                )
            selected.append(plugin["id"])
        elif not active:
            selected = [value for value in selected if value != plugin["id"]]
        return workspace, plugin, selected

    workspace, plugin, selected = await sync_to_async(plan)()
    await get_runner_service().update_workspace(
        workspace.id,
        plugin_ids=selected,
        user=ctx.user,
        organization_id=ctx.organization_id,
    )

    def result() -> dict[str, Any]:
        state = next(
            (
                row
                for row in PluginService().list_workspace_plugins(
                    workspace=workspace, org_id=ctx.organization_id
                )
                if row["id"] == plugin["id"]
            ),
            None,
        )
        return compact(
            {
                "scope": "workspace",
                "workspace": workspace.name,
                "plugin_id": plugin["id"],
                "name": plugin["name"],
                "enabled": bool(state and state["workspace_enabled"]),
                "ready": state["ready"] if state else None,
            }
        )

    return await sync_to_async(result)()
