"""Credential and credential service MCP tools.

``connect_credential_oauth`` only returns the trusted frontend deep link;
the OAuth consent itself happens in the browser.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from mcp.types import TextContent, Tool

from apps.accounts.models import APIKeyPermission
from apps.credentials.mcp_oauth import frontend_return_url, oauth_credential_status
from apps.credentials.services import (
    CredentialOAuthSvc,
    CredentialServiceSvc,
    CredentialSvc,
    OrgCredentialServiceActivationSvc,
)
from apps.organizations.services import OrganizationService
from common.exceptions import ConflictError, NotFoundError

from ..results import ToolHandler, error_result, text_result

TOOLS: list[Tool] = [
    Tool(
        name="list_credentials",
        description="List credentials (metadata only, no secrets) for the current user.",
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_org_credential_services",
        description="List all credential services with organization activation status (admin).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="toggle_org_credential_service_activation",
        description="Activate or deactivate a credential service for the organization (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "active": {"type": "boolean"},
            },
            "required": ["service_id", "active"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_credential_services",
        description="List visible credential services, endpoint and activation state.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="create_org_credential_service",
        description="Create and activate an organization credential service (admin).",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "slug": {"type": "string"},
                "description": {"type": "string"},
                "credential_type": {
                    "type": "string",
                    "enum": ["env", "file", "ssh_key", "mcp_oauth"],
                },
                "env_var_name": {"type": "string"},
                "target_path": {"type": "string"},
                "label": {"type": "string"},
                "oauth_server_url": {"type": "string"},
            },
            "required": ["name", "credential_type"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="create_credential",
        description="Create a personal or org credential (secret values never returned).",
        inputSchema={
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
                "organization_credential": {"type": "boolean"},
            },
            "required": ["service_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="update_credential",
        description="Update credential name/value without returning secrets.",
        inputSchema={
            "type": "object",
            "properties": {
                "credential_id": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["credential_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="delete_credential",
        description="Delete a credential unless a selected plugin requires it.",
        inputSchema={
            "type": "object",
            "properties": {"credential_id": {"type": "string"}},
            "required": ["credential_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="connect_credential_oauth",
        description="Open the trusted frontend credential settings to connect/reconnect OAuth; no browser grant starts through MCP.",
        inputSchema={
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "credential_id": {"type": "string"},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="disconnect_credential_oauth",
        description="Disconnect an OAuth grant while keeping workspace links.",
        inputSchema={
            "type": "object",
            "properties": {"credential_id": {"type": "string"}},
            "required": ["credential_id"],
            "additionalProperties": False,
        },
    ),
]


PERMISSIONS: dict[str, APIKeyPermission] = {
    "list_credentials": APIKeyPermission.CREDENTIALS_READ,
    "list_credential_services": APIKeyPermission.CREDENTIALS_READ,
    "create_credential": APIKeyPermission.CREDENTIALS_WRITE,
    "update_credential": APIKeyPermission.CREDENTIALS_WRITE,
    "delete_credential": APIKeyPermission.CREDENTIALS_WRITE,
    "connect_credential_oauth": APIKeyPermission.CREDENTIALS_READ,
    "disconnect_credential_oauth": APIKeyPermission.CREDENTIALS_WRITE,
    "create_org_credential_service": APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE,
    "list_org_credential_services": APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ,
    "toggle_org_credential_service_activation": APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE,
}


def _call_list_credentials(api_key, org_id, args: dict) -> list[TextContent]:
    svc = CredentialSvc()
    creds = svc.list_credentials(api_key.user, org_id)

    result = [
        {
            "id": str(c.id),
            "name": c.name,
            "scope": "personal" if c.user_id is not None else "organization",
            "service_id": str(c.service_id),
            "service_name": c.service.name,
            "service_slug": c.service.slug,
            "credential_type": str(c.service.credential_type),
            "env_var_name": c.service.env_var_name,
            "target_path": c.service.target_path,
            "created_by_id": c.created_by_id,
            "created_at": c.created_at.isoformat(),
            "updated_at": c.updated_at.isoformat(),
            "oauth_connected": oauth_credential_status(c)["connected"]
            if c.service.credential_type == "mcp_oauth"
            else False,
            "oauth_status": oauth_credential_status(c)["status"]
            if c.service.credential_type == "mcp_oauth"
            else "",
            "oauth_reconnect_required": oauth_credential_status(c)["reconnect_required"]
            if c.service.credential_type == "mcp_oauth"
            else False,
            "oauth_expires_at": oauth_credential_status(c)["expires_at"].isoformat()
            if c.service.credential_type == "mcp_oauth"
            and oauth_credential_status(c)["expires_at"]
            else None,
        }
        for c in creds
    ]
    return text_result(result)


def _call_list_credential_services(api_key, org_id, args: dict):
    OrganizationService().require_membership(api_key.user, org_id)
    active = OrgCredentialServiceActivationSvc().activated_service_ids(org_id)
    return text_result(
        [
            {
                "id": str(s.id),
                "name": s.name,
                "slug": s.slug,
                "description": s.description,
                "credential_type": s.credential_type,
                "env_var_name": s.env_var_name,
                "target_path": s.target_path,
                "label": s.label,
                "organization_id": str(s.organization_id)
                if s.organization_id
                else None,
                "oauth_server_url": s.oauth_server_url,
                "is_active": s.id in active,
            }
            for s in CredentialServiceSvc().list_services(org_id=org_id)
        ]
    )


def _call_create_org_credential_service(api_key, org_id, args: dict):
    org = OrganizationService()
    org.require_membership(api_key.user, org_id)
    if org.get_user_role(api_key.user, org_id) != "admin":
        return error_result("Admin role required")
    try:
        service = CredentialServiceSvc().create_service(
            name=args.get("name", ""),
            slug=args.get("slug", ""),
            description=args.get("description", ""),
            credential_type=args.get("credential_type", ""),
            env_var_name=args.get("env_var_name", ""),
            target_path=args.get("target_path", ""),
            label=args.get("label", ""),
            organization_id=org_id,
            oauth_server_url=args.get("oauth_server_url", ""),
        )
        OrgCredentialServiceActivationSvc().set_activation(
            org_id=org_id, service=service, active=True
        )
        return text_result(
            {
                "id": str(service.id),
                "name": service.name,
                "slug": service.slug,
                "credential_type": service.credential_type,
                "oauth_server_url": service.oauth_server_url,
                "is_active": True,
            }
        )
    except (ValueError, Exception) as exc:
        return error_result(str(exc))


def _call_create_credential(api_key, org_id, args: dict):
    try:
        service_id = uuid.UUID(str(args.get("service_id", "")))
        if args.get("organization_credential"):
            org = OrganizationService()
            org.require_membership(api_key.user, org_id)
            if org.get_user_role(api_key.user, org_id) != "admin":
                return error_result("Admin role required")
            cred = CredentialSvc().create_org_credential(
                organization_id=org_id,
                service_id=service_id,
                name=args.get("name"),
                value=args.get("value"),
                user=api_key.user,
            )
        else:
            cred = CredentialSvc().create_personal_credential(
                service_id=service_id,
                name=args.get("name"),
                value=args.get("value"),
                user=api_key.user,
                org_id=org_id,
            )
        return text_result(
            {
                "id": str(cred.id),
                "name": cred.name,
                "service_id": str(cred.service_id),
                "credential_type": cred.service.credential_type,
            }
        )
    except Exception as exc:
        return error_result(str(exc))


def _call_update_credential(api_key, org_id, args: dict):
    org = OrganizationService()
    org.require_membership(api_key.user, org_id)
    try:
        credential_id = uuid.UUID(str(args.get("credential_id", "")))
        cred = CredentialSvc().update_credential(
            credential_id=credential_id,
            org_id=org_id,
            user=api_key.user,
            is_admin=org.get_user_role(api_key.user, org_id) == "admin",
            name=args.get("name"),
            value=args.get("value"),
        )
        return text_result({"id": str(cred.id), "name": cred.name})
    except Exception as exc:
        return error_result(str(exc))


def _call_delete_credential(api_key, org_id, args: dict):
    org = OrganizationService()
    org.require_membership(api_key.user, org_id)
    try:
        credential_id = uuid.UUID(str(args.get("credential_id", "")))
        CredentialSvc().delete_credential(
            credential_id,
            org_id=org_id,
            user=api_key.user,
            is_admin=org.get_user_role(api_key.user, org_id) == "admin",
        )
        return text_result({"deleted": True, "credential_id": str(credential_id)})
    except Exception as exc:
        return error_result(str(exc))


def _call_connect_credential_oauth(api_key, org_id, args: dict):
    """Return the trusted settings link for one visible MCP OAuth operation."""
    organizations = OrganizationService()
    organizations.require_membership(api_key.user, org_id)
    role = organizations.get_user_role(api_key.user, org_id)
    service_id = args.get("service_id")
    credential_id = args.get("credential_id")
    if bool(service_id) == bool(credential_id):
        return error_result("Provide exactly one of service_id or credential_id")

    try:
        service, credential = CredentialOAuthSvc().resolve_connection_target(
            user=api_key.user,
            organization_id=org_id,
            service_id=uuid.UUID(str(service_id)) if service_id else None,
            credential_id=uuid.UUID(str(credential_id)) if credential_id else None,
        )
        if credential is None:
            action = "add_credential"
            action_id = str(service.id)
        elif credential.user_id is None and role != "admin":
            return error_result("Admin role required for organization credentials")
        else:
            action = "reconnect_credential"
            action_id = str(credential.id)

        parsed = urlsplit(frontend_return_url())
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["settings"] = "credentials"
        query[action] = action_id
        link = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
        )
        return text_result(
            {
                "url": link,
                "credential_settings": True,
                "service_id": str(service.id),
                "credential_id": str(credential_id) if credential_id else None,
            }
        )
    except NotFoundError:
        return error_result(
            "OAuth credential not found" if credential_id else "OAuth service not found"
        )
    except ConflictError:
        return error_result("OAuth service is inactive for this organization")
    except (TypeError, ValueError):
        return error_result("Invalid OAuth service or credential ID")
    except Exception as exc:
        return error_result(str(exc))


def _call_disconnect_credential_oauth(api_key, org_id, args: dict):
    org = OrganizationService()
    org.require_membership(api_key.user, org_id)
    try:
        CredentialOAuthSvc().disconnect(
            credential_id=uuid.UUID(str(args.get("credential_id", ""))),
            user=api_key.user,
            organization_id=org_id,
        )
        return text_result({"disconnected": True})
    except Exception as exc:
        return error_result(str(exc))


def _call_list_org_credential_services(
    api_key, org_id, args: dict
) -> list[TextContent]:
    org_service = OrganizationService()
    try:
        org_service.require_membership(api_key.user, org_id)
    except Exception:
        return error_result("Organization membership required")
    if org_service.get_user_role(api_key.user, org_id) != "admin":
        return error_result("Admin role required")

    activated_ids = OrgCredentialServiceActivationSvc().activated_service_ids(org_id)
    services = CredentialServiceSvc().list_services(org_id=org_id)
    return text_result(
        [
            {
                "id": str(service.id),
                "name": service.name,
                "slug": service.slug,
                "description": service.description,
                "credential_type": str(service.credential_type),
                "env_var_name": service.env_var_name,
                "target_path": service.target_path,
                "label": service.label,
                "organization_id": str(service.organization_id)
                if service.organization_id
                else None,
                "oauth_server_url": service.oauth_server_url,
                "is_active": service.id in activated_ids,
            }
            for service in services
        ]
    )


def _call_toggle_org_credential_service_activation(
    api_key, org_id, args: dict
) -> list[TextContent]:
    try:
        org_service = OrganizationService()
        org_service.require_membership(api_key.user, org_id)
        if org_service.get_user_role(api_key.user, org_id) != "admin":
            return error_result("Admin role required")
        service_id = uuid.UUID(str(args.get("service_id", "")))
        if "active" not in args:
            raise ValueError("active is required")
        active = bool(args.get("active"))
        try:
            service = CredentialServiceSvc().get_service(service_id, org_id=org_id)
        except NotFoundError:
            return error_result("Credential service not found")

        OrgCredentialServiceActivationSvc().set_activation(
            org_id=org_id, service=service, active=active
        )

        return text_result(
            {
                "id": str(service.id),
                "name": service.name,
                "slug": service.slug,
                "description": service.description,
                "credential_type": str(service.credential_type),
                "env_var_name": service.env_var_name,
                "target_path": service.target_path,
                "label": service.label,
                "organization_id": str(service.organization_id)
                if service.organization_id
                else None,
                "oauth_server_url": service.oauth_server_url,
                "is_active": active,
            }
        )
    except (ValueError, TypeError) as exc:
        return error_result(str(exc) or "Invalid service_id")
    except Exception as exc:
        return error_result(str(exc))


HANDLERS: dict[str, ToolHandler] = {
    "list_credentials": _call_list_credentials,
    "list_credential_services": _call_list_credential_services,
    "create_credential": _call_create_credential,
    "update_credential": _call_update_credential,
    "delete_credential": _call_delete_credential,
    "connect_credential_oauth": _call_connect_credential_oauth,
    "disconnect_credential_oauth": _call_disconnect_credential_oauth,
    "create_org_credential_service": _call_create_org_credential_service,
    "list_org_credential_services": _call_list_org_credential_services,
    "toggle_org_credential_service_activation": _call_toggle_org_credential_service_activation,
}
