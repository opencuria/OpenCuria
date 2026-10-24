"""Harness engine, provider, account and agent configuration MCP tools."""

from __future__ import annotations

from asgiref.sync import sync_to_async
from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.harness.constants import DEFAULT_MAX_DEPTH, MAX_SUBAGENT_DEPTH
from apps.harness.engines.schemas import engine_model_to_out
from apps.harness.engines.service import EngineCatalogService
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.provider_oauth import ProviderOAuthService
from apps.harness.providers.base import ProviderError
from apps.harness.providers.models_catalog import clear_models_cache
from apps.harness.schemas import (
    ProviderConfigIn,
    SubagentConfigIn,
    SubagentConfigOut,
    agent_s_config_to_out,
    provider_config_to_out,
)
from apps.harness.services import (
    AgentSConfigService,
    ProviderConfigService,
    SubagentConfigService,
)
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError

from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="get_provider_config",
        description="Get the org-wide model and reasoning-effort defaults.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_harness_engines",
        description="List harness engines, defaults and connection status.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_engine_models",
        description="List selected-account models for a harness engine.",
        inputSchema={
            "type": "object",
            "properties": {
                "engine_id": {"type": "string", "enum": ["native", "claude", "codex"]}
            },
            "required": ["engine_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="save_provider_config",
        description="Save the org-wide small model and reasoning effort.",
        inputSchema={
            "type": "object",
            "properties": {
                "small_model": {"type": "string"},
                "small_effort": {"type": "string"},
            },
        },
    ),
    Tool(
        name="delete_provider_config",
        description="Delete the org-wide harness provider config.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_provider_accounts",
        description=(
            "List connected personal and organization-shared "
            "provider accounts visible to you."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="save_provider_account",
        description=(
            "Create or update a named provider account. "
            "Shared accounts require an org admin."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "account_id": {"type": "string", "format": "uuid"},
                "label": {"type": "string"},
                "shared": {"type": "boolean"},
                "api_key": {"type": "string"},
                "base_url": {"type": "string"},
                "models": {"type": "array", "items": {"type": "string"}},
                "auth_method": {"type": "string"},
                "region": {"type": "string"},
                "access_key_id": {"type": "string"},
                "secret_access_key": {"type": "string"},
                "session_token": {"type": "string"},
                "bearer_token": {"type": "string"},
                "token": {"type": "string"},
                "auth_type": {"type": "string"},
            },
            "required": ["provider"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="delete_provider_account",
        description="Delete one exact provider account you are allowed to manage.",
        inputSchema={
            "type": "object",
            "properties": {"account_id": {"type": "string", "format": "uuid"}},
            "required": ["account_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="select_provider_account",
        description=(
            "Select one visible provider account, or null for no active account."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "provider": {"type": "string"},
                "account_id": {"type": ["string", "null"]},
            },
            "required": ["provider", "account_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_subagent_config",
        description="Get org-wide subagent depth (defaults when unstored).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="save_subagent_config",
        description="Save (upsert) the org-wide maximum subagent depth.",
        inputSchema={
            "type": "object",
            "properties": {
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SUBAGENT_DEPTH,
                    "default": DEFAULT_MAX_DEPTH,
                },
            },
        },
    ),
    Tool(
        name="get_agent_s_config",
        description="Get the org-wide Agent-S harness config (defaults when unstored).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="save_agent_s_config",
        description="Save (upsert) the org-wide Agent-S harness config.",
        inputSchema={
            "type": "object",
            "properties": {
                "grounding_model": {"type": "string"},
                "grounding_width": {"type": "integer"},
                "grounding_height": {"type": "integer"},
                "model_temperature": {"type": ["number", "null"]},
                "max_steps": {"type": "integer"},
                "max_trajectory_length": {"type": "integer"},
                "enable_reflection": {"type": "boolean"},
                "enable_code_agent": {"type": "boolean"},
                "enable_recording": {"type": "boolean"},
                "screenshot_max_dimension": {"type": "integer"},
                "action_pre_delay": {"type": "number"},
                "action_post_delay": {"type": "number"},
                "wait_delay": {"type": "number"},
            },
        },
    ),
    Tool(
        name="chatgpt_oauth_start",
        description="Start an isolated ChatGPT device OAuth flow for this user.",
        inputSchema={
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "shared": {"type": "boolean"},
                "account_id": {"type": "string", "format": "uuid"},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="chatgpt_oauth_status",
        description=(
            "Poll once for ChatGPT OAuth completion. Returns status: "
            "pending, connected, expired, denied, or no_flow."
        ),
        inputSchema={
            "type": "object",
            "properties": {"flow_id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="chatgpt_oauth_cancel",
        description="Cancel one of your pending ChatGPT OAuth flows.",
        inputSchema={
            "type": "object",
            "properties": {"flow_id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "get_provider_config": APIKeyPermission.HARNESS_READ,
    "list_harness_engines": APIKeyPermission.HARNESS_READ,
    "list_engine_models": APIKeyPermission.HARNESS_READ,
    "save_provider_config": APIKeyPermission.HARNESS_RUN,
    "delete_provider_config": APIKeyPermission.HARNESS_RUN,
    "list_provider_accounts": APIKeyPermission.HARNESS_PROVIDERS,
    "save_provider_account": APIKeyPermission.HARNESS_PROVIDERS,
    "delete_provider_account": APIKeyPermission.HARNESS_PROVIDERS,
    "select_provider_account": APIKeyPermission.HARNESS_PROVIDERS,
    "get_subagent_config": APIKeyPermission.HARNESS_READ,
    "save_subagent_config": APIKeyPermission.HARNESS_RUN,
    "get_agent_s_config": APIKeyPermission.HARNESS_READ,
    "save_agent_s_config": APIKeyPermission.HARNESS_RUN,
    "chatgpt_oauth_start": APIKeyPermission.HARNESS_PROVIDERS,
    "chatgpt_oauth_status": APIKeyPermission.HARNESS_PROVIDERS,
    "chatgpt_oauth_cancel": APIKeyPermission.HARNESS_PROVIDERS,
}


def _call_list_harness_engines(api_key, org_id, args: dict) -> list[TextContent]:
    """List available harness engines and the caller's connection status."""
    try:
        OrganizationService().require_membership(api_key.user, org_id)
        return text_result(EngineCatalogService().list_engines(org_id, api_key.user.id))
    except NotFoundError as exc:
        return error_result(exc.message)


def _call_list_engine_models(api_key, org_id, args: dict) -> list[TextContent]:
    """Expose the same shared, account-scoped catalog as REST."""
    try:
        OrganizationService().require_membership(api_key.user, org_id)
        models = EngineCatalogService().list_models(
            args.get("engine_id", ""), org_id, api_key.user.id
        )
        return text_result(
            [engine_model_to_out(m).model_dump(mode="json") for m in models]
        )
    except (AuthenticationError, NotFoundError, ValueError) as exc:
        return error_result(str(exc))
    except ProviderError:
        return error_result("Engine model catalog is temporarily unavailable")


def _call_get_provider_config(api_key, org_id, args: dict) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    config = ProviderConfigService().find_config(org_id)
    return text_result(provider_config_to_out(config).model_dump(mode="json"))


def _call_save_provider_config(api_key, org_id, args: dict) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    payload = ProviderConfigIn(**args)
    config = ProviderConfigService().save_config(
        organization_id=org_id, **payload.model_dump()
    )
    return text_result(provider_config_to_out(config).model_dump(mode="json"))


def _call_delete_provider_config(api_key, org_id, args: dict) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    ProviderConfigService().delete_config(org_id)
    return text_result({"deleted": True})


def _call_get_subagent_config(api_key, org_id, args: dict) -> list[TextContent]:
    """Return subagent config after checking organization membership."""
    OrganizationService().require_membership(api_key.user, org_id)
    config = SubagentConfigOut(**SubagentConfigService().get_or_default(org_id))
    return text_result(config.model_dump(mode="json"))


def _call_save_subagent_config(api_key, org_id, args: dict) -> list[TextContent]:
    """Validate and save subagent config with REST-equivalent semantics."""
    OrganizationService().require_membership(api_key.user, org_id)
    try:
        payload = SubagentConfigIn(**args)
        config = SubagentConfigOut(
            **SubagentConfigService().save_config(org_id, payload.max_depth)
        )
        return text_result(config.model_dump(mode="json"))
    except (ValueError, KeyError, TypeError) as exc:
        return error_result(str(exc))


def _call_get_agent_s_config(api_key, org_id, args: dict) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    row = AgentSConfigService().get_or_default(org_id)
    return text_result(agent_s_config_to_out(row).model_dump(mode="json"))


def _call_save_agent_s_config(api_key, org_id, args: dict) -> list[TextContent]:
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    try:
        # MCP dict args carry no "explicitly set" tracking, so the service
        # merge treats provided keys as the partial update (never defaults).
        row = AgentSConfigService().save_config(org_id, dict(args or {}))
        return text_result(agent_s_config_to_out(row).model_dump(mode="json"))
    except (ValueError, KeyError, TypeError) as exc:
        return error_result(str(exc))


async def _call_chatgpt_oauth_start(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        result = await ProviderOAuthService().start(
            organization_id=org_id,
            user=api_key.user,
            label=str(args.get("label") or ""),
            shared=args.get("shared"),
            account_id=args.get("account_id"),
        )
        return text_result(result)
    except Exception:
        return error_result("ChatGPT OAuth provider request failed")


async def _call_chatgpt_oauth_status(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        result = await ProviderOAuthService().status(
            organization_id=org_id, user=api_key.user, flow_id=args.get("flow_id")
        )
        return text_result(result)
    except Exception:
        return error_result("ChatGPT OAuth provider request failed")


async def _call_chatgpt_oauth_cancel(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        cancelled = await sync_to_async(ProviderOAuthService().cancel)(
            organization_id=org_id,
            user=api_key.user,
            flow_id=args.get("flow_id"),
        )
        return text_result({"cancelled": cancelled})
    except Exception as exc:
        return error_result(str(exc))


def _call_list_provider_accounts(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        rows = ProviderAccountService().list_accounts(org_id, int(api_key.user.id))
        return text_result([row for row in rows if row["connected"]])
    except Exception as exc:
        return error_result(str(exc))


def _call_save_provider_account(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        values = dict(args)
        provider = values.pop("provider")
        account_id = values.pop("account_id", None)
        if provider == "chatgpt" and account_id is None:
            return error_result("ChatGPT accounts must be connected using OAuth")
        account = ProviderAccountService().save_account(
            organization_id=org_id,
            user=api_key.user,
            provider=provider,
            payload=values,
            account_id=account_id,
        )
        clear_models_cache(str(org_id))
        row = next(
            row
            for row in ProviderAccountService().list_accounts(
                org_id, int(api_key.user.id), provider
            )
            if row["id"] == str(account.id)
        )
        return text_result(row)
    except Exception as exc:
        return error_result(str(exc))


def _call_delete_provider_account(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        account = ProviderAccountService().get_account(
            org_id, int(api_key.user.id), args["account_id"], manage=True
        )
        ProviderAccountService().delete_account(
            org_id, int(api_key.user.id), account.id
        )
        clear_models_cache(str(org_id))
        return text_result({"deleted": True, "account_id": str(account.id)})
    except Exception as exc:
        return error_result(str(exc))


def _call_select_provider_account(api_key, org_id, args: dict) -> list[TextContent]:
    try:
        ProviderAccountService().select_account(
            org_id, int(api_key.user.id), args["provider"], args.get("account_id")
        )
        return text_result(
            {
                "selected": True,
                "provider": args["provider"],
                "account_id": args.get("account_id"),
            }
        )
    except Exception as exc:
        return error_result(str(exc))


HANDLERS: dict[str, ToolHandler] = {
    "get_provider_config": _call_get_provider_config,
    "list_harness_engines": _call_list_harness_engines,
    "list_engine_models": _call_list_engine_models,
    "save_provider_config": _call_save_provider_config,
    "delete_provider_config": _call_delete_provider_config,
    "list_provider_accounts": _call_list_provider_accounts,
    "save_provider_account": _call_save_provider_account,
    "delete_provider_account": _call_delete_provider_account,
    "select_provider_account": _call_select_provider_account,
    "get_subagent_config": _call_get_subagent_config,
    "save_subagent_config": _call_save_subagent_config,
    "get_agent_s_config": _call_get_agent_s_config,
    "save_agent_s_config": _call_save_agent_s_config,
    "chatgpt_oauth_start": _call_chatgpt_oauth_start,
    "chatgpt_oauth_status": _call_chatgpt_oauth_status,
    "chatgpt_oauth_cancel": _call_chatgpt_oauth_cancel,
}
