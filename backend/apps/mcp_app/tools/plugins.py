"""Plugin tools: bundles of skills, MCP servers and credential requirements.

Agents create personal plugins for the workspace owner (admins may also
create organization plugins) and enable them in their own workspace; the
harness picks up the plugin's skills and MCP tools at the agent's next step.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from asgiref.sync import sync_to_async

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
    limit,
    obj,
    string,
)

TOOLS: list[ToolSpec] = []
tool = registrar(TOOLS)

PLUGIN_REF = string("Plugin ID, slug or exact name.")

_STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}}

PICKUP_NOTE = (
    "Agents in the workspace, including you, get its skills and MCP tools "
    "with their next step. MCP tools are named mcp__<plugin>__<server>__<tool> "
    "or similar; servers that fail to start are reported to you."
)

PLUGIN_SKILL = obj(
    {
        "name": string("Skill name, e.g. 'Release checklist'.", minLength=1),
        "description": string(
            "What the skill does and when to use it (third person, max 1024 "
            "characters). Agents only see this until they load the skill."
        ),
        "body": string(
            "Markdown instructions. A SKILL.md with name/description "
            "frontmatter is accepted as is.",
            minLength=1,
        ),
    },
    ("name", "body"),
)

SECRET = {
    "anyOf": [
        {"type": "string", "minLength": 1},
        {"type": "null"},
        {
            "type": "object",
            "properties": {"credential": {"type": "string", "minLength": 1}},
            "required": ["credential"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"per_user": {"const": True}},
            "required": ["per_user"],
            "additionalProperties": False,
        },
    ]
}

_EXACT_PLACEHOLDER = re.compile(r"^\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}$")
_ANY_PLACEHOLDER = re.compile(r"\{\{\s*credential\.([A-Za-z0-9_.-]{1,128})\s*\}\}")

PLUGIN_MCP_SERVER = obj(
    {
        "name": string("Server name, e.g. 'GitHub'.", minLength=1),
        "transport": enum(
            ["stdio", "streamable_http", "sse"],
            "'stdio' runs command inside the workspace; 'streamable_http' or "
            "'sse' connect to url.",
            default="stdio",
        ),
        "command": string(
            "stdio only: the command line, e.g. 'npx -y @modelcontextprotocol/"
            "server-filesystem /workspace'. Runs without a shell."
        ),
        "env": {
            **_STRING_MAP,
            "description": "stdio only: environment variables (not secret). "
            "Values may reference a credential requirement as {{credential.KEY}}.",
        },
        "url": string("HTTP transports only: server URL, e.g. https://…/mcp."),
        "headers": {
            **_STRING_MAP,
            "description": "HTTP only: request headers (not secret); values may "
            "use {{credential.KEY}}.",
        },
        "secrets": {
            "type": "object",
            "additionalProperties": SECRET,
            "description": "Secret environment variables (stdio) or headers "
            "(HTTP), stored as credentials, e.g. {'GITHUB_TOKEN': 'ghp_…'} or "
            "{'Authorization': 'Bearer …'}. Per name: the value (only values "
            "the user gave you); {'credential': '<name>'} to use one of the "
            "user's credentials (see workspace_options); {'per_user': true} to "
            "let each workspace attach its own; null keeps what is set. Never "
            "put secrets into env or headers.",
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
    },
    ("name",),
)

PLUGIN_REQUIREMENT = obj(
    {
        "key": string(
            "Stable key referenced as {{credential.KEY}}, e.g. 'github_token'.",
            minLength=1,
        ),
        "service": string(
            "Credential service ID, slug or name (the kind of credential, e.g. "
            "'GitHub token' or an MCP OAuth service)."
        ),
        "credential": string(
            "Optional credential ID or name the plugin always uses; otherwise "
            "each workspace attaches its own."
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
    "skills": array(PLUGIN_SKILL, "Skills (Markdown instructions) of the plugin."),
    "mcp_servers": array(
        PLUGIN_MCP_SERVER, "MCP servers whose tools agents get in the workspace."
    ),
    "credential_requirements": array(
        PLUGIN_REQUIREMENT,
        "Advanced: credential services referenced as {{credential.KEY}} or by "
        "OAuth servers. For secret variables use the servers' secrets instead.",
    ),
}

ENABLE = boolean(
    "Enable the plugin in the current workspace right away (default true).",
    default=True,
)


# ---------------------------------------------------------------------------
# Presenters
# ---------------------------------------------------------------------------


def _summary(plugin: dict[str, Any], *, admin: bool = True) -> dict[str, Any]:
    """Summarize a plugin; organization release is an admin setting."""
    shared = plugin["scope"] != "personal"
    return compact(
        {
            "plugin_id": plugin["id"],
            "name": plugin["name"],
            "slug": plugin["slug"],
            "description": preview(plugin["description"], 200),
            "scope": plugin["scope"],
            "editable": plugin["editable"] or None,
            "active_in_organization": plugin["org_enabled"]
            if admin and shared
            else None,
            "skills": [skill["name"] for skill in plugin["skills"]],
            "mcp_servers": [server["name"] for server in plugin["mcp_servers"]],
            "required_credentials": [
                requirement["service_name"]
                for requirement in plugin["credential_requirements"]
                if requirement["required"]
            ],
        }
    )


def _bound(mapping: dict[str, str]) -> dict[str, str]:
    """Variables whose whole value is one credential: name -> requirement key."""
    return {
        name: match.group(1)
        for name, value in (mapping or {}).items()
        if (match := _EXACT_PLACEHOLDER.match(value or ""))
    }


def _describe_credential(requirement: dict[str, Any] | None) -> str:
    if requirement is None:
        return "missing credential"
    if requirement.get("credential_name"):
        return f"credential '{requirement['credential_name']}'"
    return (
        "each workspace attaches its own credential of "
        f"'{requirement['service_name']}'"
    )


def _server(
    server: dict[str, Any],
    *,
    full: bool,
    requirements: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Present an MCP server; configuration only for those who maintain it."""
    if not full:
        return {"name": server["name"], "slug": server["slug"]}
    stdio = server["transport"] == "stdio"
    mapping = server["env"] if stdio else server["headers"]
    bound = _bound(mapping)
    plain = {name: value for name, value in mapping.items() if name not in bound}
    secrets = {
        name: _describe_credential((requirements or {}).get(key))
        for name, key in bound.items()
    }
    return compact(
        {
            "name": server["name"],
            "slug": server["slug"],
            "transport": server["transport"],
            "command": " ".join(
                [server["command"], *[_quote(arg) for arg in server["args"]]]
            )
            if stdio and server["command"]
            else None,
            "env": plain if stdio else None,
            "url": server["url"],
            "headers": None if stdio else plain,
            "secrets": secrets or None,
            "auth_type": None if server["auth_type"] == "none" else server["auth_type"],
            "oauth_requirement_key": server["oauth_requirement_key"],
        }
    )


def _quote(arg: str) -> str:
    """Shell-quote one argument for display."""
    return shlex.quote(arg)


def _full(plugin: dict[str, Any], *, admin: bool = True) -> dict[str, Any]:
    data = _summary(plugin, admin=admin)
    data["description"] = plugin["description"]
    full_config = admin or plugin["scope"] == "personal"
    data["skills"] = [
        compact(
            {
                "skill_id": skill["id"],
                "name": skill["name"],
                "description": skill["description"],
                "body": skill["body"],
            }
        )
        for skill in plugin["skills"]
    ]
    requirements = {
        requirement["key"]: requirement
        for requirement in plugin["credential_requirements"]
    }
    data["mcp_servers"] = [
        _server(server, full=full_config, requirements=requirements)
        for server in plugin["mcp_servers"]
    ]
    data["credential_requirements"] = [
        compact(
            {
                "key": requirement["key"],
                "service": requirement["service_name"],
                "service_slug": requirement["service_slug"],
                "credential": requirement.get("credential_name") or None,
                "description": requirement["description"],
                "required": requirement["required"],
            }
        )
        for requirement in plugin["credential_requirements"]
    ]
    return data


def _pick_service(ctx: ToolContext, ref: str) -> Any:
    services = list(
        CredentialServiceSvc().list_services(org_id=ctx.organization_id, user=ctx.user)
    )
    return resolve.pick(
        services,
        ref,
        kind="credential service",
        list_tool="workspace_options",
        id_of=lambda s: s.id,
        names_of=lambda s: [s.name, s.slug],
    )


def _pick_credential(ctx: ToolContext, ref: str) -> Any:
    return resolve.pick(
        resolve.list_credentials(ctx),
        ref,
        kind="credential",
        list_tool="workspace_options",
        id_of=lambda c: c.id,
        names_of=lambda c: [c.name],
    )


def _requirements(ctx: ToolContext, items: list[dict[str, Any]]) -> dict[str, dict]:
    """Translate model-facing requirements (names) to service/credential IDs."""
    result: dict[str, dict] = {}
    for item in items:
        entry: dict[str, Any] = {
            "key": item["key"],
            "description": item.get("description", ""),
            "required": item.get("required", True),
            "service_id": _pick_service(ctx, item["service"]).id,
        }
        if item.get("credential"):
            entry["credential_id"] = _pick_credential(ctx, item["credential"]).id
        result[item["key"]] = entry
    return result


def secret_key(name: str) -> str:
    """Requirement key of a secret variable: ``GITHUB_TOKEN`` -> github_token."""
    key = re.sub(r"[^a-z0-9_-]+", "_", name.strip().lower()).strip("_-")
    return key or "secret"


def _credential_plan(
    ctx: ToolContext,
    args: dict[str, Any],
    servers: list[dict[str, Any]] | None,
    current: dict[str, Any] | None,
    plugin_name: str,
) -> list[dict] | None:
    """Turn server ``secrets`` into placeholders plus credential requirements.

    Each secret becomes one requirement (key from its name): with a value a
    new credential service and credential (personal or organization, like
    the plugin), with ``credential`` a link to an existing credential, with
    ``per_user`` no credential of its own. ``servers`` are edited in place.
    Returns the full requirement list, or ``None`` to keep the stored one.
    """
    explicit = args.get("credential_requirements")
    if servers is None and explicit is None:
        return None
    stored = {
        requirement["key"]: requirement
        for requirement in (current or {}).get("credential_requirements", [])
    }
    if explicit is not None:
        plan = _requirements(ctx, explicit)
    else:
        plan = {
            key: {
                "key": key,
                "description": requirement["description"],
                "required": requirement["required"],
                "service_id": requirement["service_id"],
                "credential_id": requirement.get("credential_id"),
            }
            for key, requirement in stored.items()
        }
    for server in servers or []:
        secrets = server.pop("secrets", None) or {}
        target = "env" if server.get("transport", "stdio") == "stdio" else "headers"
        mapping = dict(server.get(target) or {})
        for name, spec in secrets.items():
            key = secret_key(name)
            known = plan.get(key) or stored.get(key)
            mapping[name] = "{{credential." + key + "}}"
            entry: dict[str, Any] = {
                "key": key,
                "description": (known or {}).get("description", ""),
                "required": True,
            }
            if known is not None:
                entry["service_id"] = known["service_id"]
            else:
                entry["new_service"] = {
                    "name": f"{name} ({plugin_name})",
                    "env_var_name": name,
                }
            if spec is None:
                if known is None:
                    raise ToolError(
                        f"Secret '{name}' has nothing saved yet; pass its value."
                    )
                entry["credential_id"] = known.get("credential_id")
            elif isinstance(spec, str):
                entry["credential_id"] = (known or {}).get("credential_id")
                entry["value"] = spec
            elif spec.get("credential"):
                credential = _pick_credential(ctx, spec["credential"])
                entry.pop("new_service", None)
                entry["service_id"] = credential.service_id
                entry["credential_id"] = credential.id
            else:
                entry["credential_id"] = None
            plan[key] = entry
        server[target] = mapping
    if servers is not None and explicit is None:
        # Secrets removed from every server drop their requirement.
        used = {
            key
            for server in servers
            for value in [
                *(server.get("env") or {}).values(),
                *(server.get("headers") or {}).values(),
            ]
            for key in _ANY_PLACEHOLDER.findall(value)
        } | {server.get("oauth_requirement_key") for server in servers}
        retired = {
            key
            for server in (current or {}).get("mcp_servers", [])
            for key in _bound(
                server["env"] if server["transport"] == "stdio" else server["headers"]
            ).values()
        }
        for key in retired - used:
            plan.pop(key, None)
    return list(plan.values())


def _servers(
    items: list[dict[str, Any]], current: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Keep stored server identities by name."""
    by_name = {server["name"].casefold(): server["id"] for server in current or []}
    by_slug = {server["slug"]: server["id"] for server in current or []}
    result = []
    for item in items:
        server = dict(item)
        known = by_name.get(str(server.get("name", "")).casefold()) or by_slug.get(
            server.get("slug", "")
        )
        if known is not None:
            server["id"] = known
        result.append(server)
    return result


def _components(
    ctx: ToolContext, args: dict[str, Any], current: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Return create/update keyword arguments for the given component fields."""
    values: dict[str, Any] = {}
    if "skills" in args:
        known = {
            skill["name"].casefold(): skill["id"]
            for skill in (current or {}).get("skills", [])
        }
        values["skills"] = [
            {
                **skill,
                "position": index,
                **(
                    {"id": known[skill["name"].casefold()]}
                    if skill["name"].casefold() in known
                    else {}
                ),
            }
            for index, skill in enumerate(args["skills"])
        ]
    if "mcp_servers" in args:
        values["mcp_servers"] = _servers(
            args["mcp_servers"], (current or {}).get("mcp_servers")
        )
    requirements = _credential_plan(
        ctx,
        args,
        values.get("mcp_servers"),
        current,
        args.get("name") or (current or {}).get("name", ""),
    )
    if requirements is not None:
        values["credential_requirements"] = requirements
    return values


def _editable(ctx: ToolContext, ref: str, action: str) -> dict[str, Any]:
    plugin = resolve.plugin(ctx, ref)
    if plugin["scope"] == "builtin":
        raise ToolError(
            f"'{plugin['name']}' is a built-in plugin and cannot be changed; "
            "create your own plugin instead."
        )
    if not plugin["editable"]:
        raise ToolError(
            f"Only organization admins can {action} '{plugin['name']}'. Create "
            "a personal plugin instead."
        )
    return plugin


async def _set_workspace_plugin(
    ctx: ToolContext, workspace: Any, plugin: dict[str, Any], active: bool
) -> dict[str, Any]:
    """Enable or disable *plugin* in *workspace* and report its state there."""

    def plan() -> list[Any]:
        selected = list(PluginService().workspace_plugin_ids(workspace.id))
        if active and plugin["id"] not in selected:
            if not plugin["available"]:
                raise ToolError(
                    f"'{plugin['name']}' is not released for the organization. "
                    + (
                        "Release it first (plugin_set_active with organization=true)."
                        if ctx.is_admin
                        else "Ask an organization admin to release it."
                    )
                )
            selected.append(plugin["id"])
        elif not active:
            selected = [value for value in selected if value != plugin["id"]]
        return selected

    selected = await sync_to_async(plan)()
    await get_runner_service().workspace_lifecycle.update_workspace(
        workspace.id,
        plugin_ids=selected,
        user=ctx.user,
        organization_id=ctx.organization_id,
    )

    def state() -> dict[str, Any]:
        row = next(
            (
                item
                for item in PluginService().list_workspace_plugins(
                    workspace=workspace, org_id=ctx.organization_id
                )
                if item["id"] == plugin["id"]
            ),
            None,
        )
        enabled = bool(row and row["workspace_enabled"])
        return compact(
            {
                "workspace": workspace.name,
                "current_workspace": str(workspace.id) == str(ctx.workspace_id),
                "enabled": enabled,
                "ready": row["ready"] if row else None,
                "missing_credentials": [
                    gap.get("service_name") or gap.get("key")
                    for gap in (row or {}).get("missing_required_credentials", [])
                ]
                or None,
                "note": PICKUP_NOTE if enabled else None,
            }
        )

    return await sync_to_async(state)()


def _workspace_or_current(ctx: ToolContext, ref: str | None) -> Any:
    """Resolve *ref*, defaulting to the workspace the agent runs in."""
    return resolve.workspace(ctx, ref or str(ctx.workspace_id))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@tool(
    "plugin_list",
    title="List plugins",
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
        List plugins: built-in ones, the organization's and the user's own
        personal plugins. A plugin bundles skills (Markdown instructions) and
        MCP servers (extra tools, e.g. a browser or GitHub). Built-in and
        organization plugins must be released for the organization
        (active_in_organization) before workspaces can enable them; personal
        plugins can always be enabled. enabled_here tells whether the plugin
        is enabled in the current workspace. With workspace: the usable
        plugins with enabled, ready and missing_credentials.
    """,
    member_description="""
        List the plugins the user can use: built-in and organization plugins
        released for the organization and the user's own personal plugins. A
        plugin bundles skills (Markdown instructions) and MCP servers (extra
        tools, e.g. a browser or GitHub). enabled_here tells whether it is
        enabled in the current workspace. With workspace: the usable plugins
        with enabled, ready and missing_credentials.
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
                        "scope": plugin["scope"],
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
            enabled_here = PluginService().workspace_plugin_ids(ctx.workspace_id)
            rows = [
                {
                    **_summary(plugin, admin=ctx.is_admin),
                    "enabled_here": plugin["id"] in enabled_here,
                }
                for plugin in PluginService().list_visible(
                    org_id=ctx.organization_id, user=ctx.user, admin=ctx.is_admin
                )
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
    read_only=True,
    schema=obj({"plugin": PLUGIN_REF}, ("plugin",)),
    description="""
        Get a plugin's full definition: skills with their Markdown, MCP server
        configuration (secret values are never shown, only their names;
        credentials appear as {{credential.KEY}} placeholders) and credential
        requirements.
    """,
    member_description="""
        Get a plugin: its skills with their Markdown, its MCP servers (full
        configuration for the user's own plugins, names otherwise; secret
        values are never shown) and the credentials a workspace needs.
    """,
)
async def plugin_get(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await sync_to_async(
        lambda: _full(resolve.plugin(ctx, args["plugin"]), admin=ctx.is_admin)
    )()


def _create_schema(*, admin: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "name": string("Plugin name, e.g. 'GitHub tools'.", minLength=1),
        **COMPONENTS,
        "enable": ENABLE,
    }
    if admin:
        fields["scope"] = enum(
            ["personal", "organization"],
            "'personal' (default): only the user sees and uses it. "
            "'organization': shared with and released for all members.",
            default="personal",
        )
    return obj(fields, ("name",))


@tool(
    "plugin_create",
    title="Create plugin",
    schema=_create_schema(admin=True),
    description="""
        Create a plugin from skills and MCP servers and, by default, enable it
        in the current workspace so you can use and test it right away (its
        tools arrive with your next step). Check plugin_list first and change
        an existing plugin with plugin_update instead of duplicating it.
        Store API keys the user gave you as secrets, never in env or headers.
    """,
    member_schema=_create_schema(admin=False),
    member_description="""
        Create a personal plugin from skills and MCP servers and, by default,
        enable it in the current workspace so you can use and test it right
        away (its tools arrive with your next step). Check plugin_list first
        and change an existing plugin with plugin_update instead of
        duplicating it. Store API keys the user gave you as secrets, never in
        env or headers.
    """,
)
async def plugin_create(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    scope = args.get("scope", "personal")

    def create() -> tuple[dict[str, Any], Any]:
        service = PluginService()
        components = _components(ctx, args)
        plugin = service.create_plugin(
            org_id=ctx.organization_id,
            user=ctx.user,
            admin=ctx.is_admin,
            scope=scope,
            name=args["name"],
            description=args.get("description", ""),
            skills=components.get("skills"),
            mcp_servers=components.get("mcp_servers"),
            credential_requirements=components.get("credential_requirements"),
        )
        if scope == "organization":
            plugin = service.set_org_activation(
                plugin["id"], org_id=ctx.organization_id, user=ctx.user, active=True
            )
        workspace = (
            _workspace_or_current(ctx, None) if args.get("enable", True) else None
        )
        return plugin, workspace

    plugin, workspace = await sync_to_async(create)()
    result = _full(plugin, admin=ctx.is_admin)
    if workspace is not None:
        result["workspace"] = await _set_workspace_plugin(ctx, workspace, plugin, True)
    return result


def _update_schema(*, admin: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "plugin": PLUGIN_REF,
        "name": string("New name.", minLength=1),
        **COMPONENTS,
    }
    return obj(fields, ("plugin",))


@tool(
    "plugin_update",
    title="Update plugin",
    idempotent=True,
    schema=_update_schema(admin=True),
    description="""
        Change one of the user's personal plugins or (admins) an organization
        plugin; built-in plugins are read-only. Only the fields you pass
        change; skills, mcp_servers and credential_requirements each replace
        the whole list (entries keep their identity and saved secrets by
        name), so read the plugin with plugin_get first and send the edited
        list. Agents using the plugin pick up the change with their next step.
    """,
    member_schema=_update_schema(admin=False),
    member_description="""
        Change one of the user's personal plugins. Only the fields you pass
        change; skills, mcp_servers and credential_requirements each replace
        the whole list (entries keep their identity and saved secrets by
        name), so read the plugin with plugin_get first and send the edited
        list. Agents using the plugin pick up the change with their next step.
    """,
)
async def plugin_update(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def update() -> dict[str, Any]:
        plugin = _editable(ctx, args["plugin"], "change")
        components = _components(ctx, args, plugin)
        updated = PluginService().update_plugin(
            plugin["id"],
            org_id=ctx.organization_id,
            user=ctx.user,
            admin=ctx.is_admin,
            name=args.get("name"),
            description=args.get("description"),
            skills=components.get("skills"),
            mcp_servers=components.get("mcp_servers"),
            credential_requirements=components.get("credential_requirements"),
        )
        return _full(updated, admin=ctx.is_admin)

    return await sync_to_async(update)()


@tool(
    "plugin_delete",
    title="Delete plugin",
    destructive=True,
    schema=obj({"plugin": PLUGIN_REF}, ("plugin",)),
    description="""
        Permanently delete one of the user's personal plugins or (admins) an
        organization plugin. Workspaces lose its skills and MCP servers;
        credentials are kept.
    """,
    member_description="""
        Permanently delete one of the user's personal plugins. Workspaces lose
        its skills and MCP servers; credentials are kept.
    """,
)
async def plugin_delete(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    def delete() -> dict[str, Any]:
        plugin = _editable(ctx, args["plugin"], "delete")
        PluginService().delete_plugin(
            plugin["id"], org_id=ctx.organization_id, user=ctx.user, admin=ctx.is_admin
        )
        return {"plugin_id": plugin["id"], "name": plugin["name"], "deleted": True}

    return await sync_to_async(delete)()


def _activation_schema(*, admin: bool) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "plugin": PLUGIN_REF,
        "active": boolean("true to enable, false to disable."),
        "workspace": string("Workspace ID or name; defaults to the current workspace."),
    }
    if admin:
        fields["organization"] = boolean(
            "true releases (or withdraws) a built-in or organization plugin for "
            "the whole organization instead of one workspace.",
            default=False,
        )
    return obj(fields, ("plugin", "active"))


@tool(
    "plugin_set_active",
    title="Enable or disable plugin",
    idempotent=True,
    schema=_activation_schema(admin=True),
    description="""
        Enable or disable a plugin in a workspace (default: the current one);
        its agents get or lose the plugin's skills and MCP tools with their
        next step. With organization=true, release or withdraw a built-in or
        organization plugin for the whole organization (required before
        workspaces can enable it). Enabling fails with the missing
        credentials listed if the workspace lacks a required credential;
        attach one with workspace_update.
    """,
    member_schema=_activation_schema(admin=False),
    member_description="""
        Enable or disable a plugin in a workspace (default: the current one);
        its agents get or lose the plugin's skills and MCP tools with their
        next step. Enabling fails with the missing credentials listed if the
        workspace lacks a required credential; attach one with
        workspace_update.
    """,
)
async def plugin_set_active(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    active = bool(args["active"])
    if args.get("organization"):

        def set_org() -> dict[str, Any]:
            ctx.require_admin("release plugins for the organization")
            plugin = resolve.plugin(ctx, args["plugin"])
            if plugin["scope"] == "personal":
                raise ToolError(
                    "Personal plugins are only for their owner; enable them per "
                    "workspace instead."
                )
            updated = PluginService().set_org_activation(
                plugin["id"], org_id=ctx.organization_id, user=ctx.user, active=active
            )
            return {"scope": "organization", **_summary(updated)}

        return await sync_to_async(set_org)()

    workspace, plugin = await sync_to_async(
        lambda: (
            _workspace_or_current(ctx, args.get("workspace")),
            resolve.plugin(ctx, args["plugin"]),
        )
    )()
    state = await _set_workspace_plugin(ctx, workspace, plugin, active)
    return {"plugin_id": plugin["id"], "name": plugin["name"], **state}
