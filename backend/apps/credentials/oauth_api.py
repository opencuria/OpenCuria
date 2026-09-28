"""MCP OAuth connect/status/disconnect API and public fixed callback."""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from ninja import Router, Schema

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from apps.plugins.models import PluginMcpServer
from apps.plugins.repositories import PluginRepository
from common.exceptions import AuthenticationError, NotFoundError
from common.utils import decrypt_value

from .enums import CredentialType
from .mcp_oauth import (
    OAuthError,
    complete_flow,
    frontend_return_url,
    start_flow,
    state_cookie_name,
)
from .models import Credential, CredentialService


class OAuthConnectIn(Schema):
    service_id: uuid.UUID
    organization_credential: bool = False


class OAuthConnectOut(Schema):
    authorization_url: str
    status: str = "pending"


class OAuthScopeStatusOut(Schema):
    connected: bool
    credential_id: uuid.UUID | None = None
    expires_at: str | None = None
    reconnect_required: bool = False


class OAuthStatusOut(Schema):
    personal: OAuthScopeStatusOut
    organization: OAuthScopeStatusOut


class OAuthErrorOut(Schema):
    detail: str
    code: str


router = Router(tags=["mcp-oauth"])


def _org_id(request: HttpRequest) -> uuid.UUID:
    value = request.headers.get("X-Organization-Id", "")
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        raise AuthenticationError("X-Organization-Id header is required") from None


def _server(plugin_id: uuid.UUID, server_id: uuid.UUID, org_id: uuid.UUID):
    plugin = PluginRepository.get_visible_by_id(plugin_id, org_id)
    if plugin is None:
        raise NotFoundError("Plugin", str(plugin_id))
    server = PluginMcpServer.objects.filter(id=server_id, plugin=plugin).first()
    if server is None:
        raise NotFoundError("MCP server", str(server_id))
    if server.auth_type != "oauth" or server.transport == "stdio":
        raise OAuthError("This MCP server does not use OAuth")
    return server


def _deny_non_user_or_api_key(request):
    if not getattr(request.user, "is_authenticated", False):
        return 401, OAuthErrorOut(
            detail="Authentication required", code="not_authenticated"
        )
    if getattr(request, "api_key", None) is not None:
        return 403, OAuthErrorOut(
            detail="OAuth connections require an interactive JWT session",
            code="forbidden",
        )
    return None


def _scope_status(credential: Credential | None) -> OAuthScopeStatusOut:
    if credential is None:
        return OAuthScopeStatusOut(connected=False)
    if credential.oauth_status != "connected" or not credential.encrypted_value:
        return OAuthScopeStatusOut(
            connected=False,
            credential_id=credential.id,
            reconnect_required=credential.oauth_status != "disconnected",
        )
    try:
        data = json.loads(decrypt_value(credential.encrypted_value))
        if (
            not isinstance(data.get("access_token"), str)
            or not data["access_token"]
            or data.get("server_id") != str(credential.oauth_server_id)
            or data.get("server_url") != credential.oauth_server_url
            or data.get("resource") != credential.oauth_resource
            or data.get("registration_id") != str(credential.oauth_registration_id)
        ):
            return OAuthScopeStatusOut(
                connected=False,
                credential_id=credential.id,
                reconnect_required=True,
            )
        expires_at = data.get("expires_at") or None
        if expires_at and not data.get("refresh_token"):
            from django.utils import timezone

            expiry = timezone.datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.get_current_timezone())
            if expiry <= timezone.now():
                return OAuthScopeStatusOut(
                    connected=False,
                    credential_id=credential.id,
                    expires_at=expires_at,
                    reconnect_required=True,
                )
        return OAuthScopeStatusOut(
            connected=True, credential_id=credential.id, expires_at=expires_at
        )
    except Exception:
        return OAuthScopeStatusOut(
            connected=False, credential_id=credential.id, reconnect_required=True
        )


@router.post(
    "/{plugin_id}/mcp-servers/{server_id}/oauth/connect/",
    response={
        200: OAuthConnectOut,
        400: OAuthErrorOut,
        401: OAuthErrorOut,
        403: OAuthErrorOut,
        404: OAuthErrorOut,
    },
)
def connect(
    request: HttpRequest,
    plugin_id: uuid.UUID,
    server_id: uuid.UUID,
    payload: OAuthConnectIn,
):
    denied = _deny_non_user_or_api_key(request)
    if denied:
        return denied
    org_id = _org_id(request)
    organizations = OrganizationService()
    try:
        org = organizations.get_organization(org_id, request.user)
        if not check_api_key_permission(request, APIKeyPermission.PLUGINS_READ):
            return 403, OAuthErrorOut(detail="Permission denied", code="forbidden")
        if (
            payload.organization_credential
            and organizations.get_user_role(request.user, org_id) != "admin"
        ):
            return 403, OAuthErrorOut(
                detail="Admin role required for organization OAuth credentials",
                code="forbidden",
            )
        server = _server(plugin_id, server_id, org_id)
        requirement = (
            server.plugin.credential_requirements.filter(
                key=server.oauth_requirement_key, required=True
            )
            .select_related("credential_service")
            .first()
        )
        service = CredentialService.objects.filter(id=payload.service_id).first()
        if (
            requirement is None
            or service is None
            or requirement.credential_service_id != service.id
            or service.credential_type != CredentialType.MCP_OAUTH
        ):
            return 404, OAuthErrorOut(
                detail="OAuth service not found", code="not_found"
            )
        callback_origin = urlparse(settings.MCP_OAUTH_CALLBACK_URL)
        request_origin = urlparse(f"//{request.get_host()}")
        request_scheme = "https" if request.is_secure() else "http"
        if (
            request_scheme != callback_origin.scheme
            or (request_origin.hostname or "").lower()
            != (callback_origin.hostname or "").lower()
        ):
            raise OAuthError(
                "MCP OAuth browser API requests must use the configured "
                "callback hostname and scheme"
            )
        authorization_url, binding, cookie_name = start_flow(
            user=request.user,
            organization=org,
            server=server,
            service=service,
            organization_credential=payload.organization_credential,
        )
        response = HttpResponse(
            json.dumps({"authorization_url": authorization_url, "status": "pending"}),
            content_type="application/json",
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        response["Referrer-Policy"] = "no-referrer"
        response.set_cookie(
            cookie_name,
            binding,
            max_age=600,
            httponly=True,
            secure=(not settings.DEBUG)
            or urlparse(settings.MCP_OAUTH_CALLBACK_URL).scheme == "https",
            samesite="Lax",
            path="/api/v1/mcp-oauth/callback/",
        )
        return response
    except NotFoundError:
        return 404, OAuthErrorOut(detail="Plugin not found", code="not_found")
    except AuthenticationError as exc:
        return 403, OAuthErrorOut(detail=exc.message, code=exc.code)
    except OAuthError as exc:
        return 400, OAuthErrorOut(detail=str(exc), code="oauth_error")
    except Exception:
        return 400, OAuthErrorOut(
            detail="OAuth connection could not be started", code="oauth_error"
        )


@router.get(
    "/{plugin_id}/mcp-servers/{server_id}/oauth/status/",
    response={
        200: OAuthStatusOut,
        401: OAuthErrorOut,
        403: OAuthErrorOut,
        404: OAuthErrorOut,
    },
)
def status(request: HttpRequest, plugin_id: uuid.UUID, server_id: uuid.UUID):
    denied = _deny_non_user_or_api_key(request)
    if denied:
        return denied
    org_id = _org_id(request)
    organizations = OrganizationService()
    try:
        organizations.require_membership(request.user, org_id)
        server = _server(plugin_id, server_id, org_id)
    except NotFoundError:
        return 404, OAuthErrorOut(detail="Plugin not found", code="not_found")
    except AuthenticationError:
        # Hide whether a foreign organization or membership exists.
        return 404, OAuthErrorOut(detail="Plugin not found", code="not_found")
    requirement = server.plugin.credential_requirements.filter(
        key=server.oauth_requirement_key,
        credential_service__credential_type=CredentialType.MCP_OAUTH,
    ).first()
    if requirement is None:
        return OAuthStatusOut(
            personal=OAuthScopeStatusOut(connected=False, reconnect_required=True),
            organization=OAuthScopeStatusOut(connected=False, reconnect_required=True),
        )
    common = {
        "service_id": requirement.credential_service_id,
        "oauth_server_id": server.id,
        "oauth_server_url": server.url,
    }
    personal = Credential.objects.filter(**common, user=request.user).first()
    organization = Credential.objects.filter(**common, organization_id=org_id).first()
    return OAuthStatusOut(
        personal=_scope_status(personal), organization=_scope_status(organization)
    )


@router.delete(
    "/{plugin_id}/mcp-servers/{server_id}/oauth/disconnect/",
    response={
        204: None,
        400: OAuthErrorOut,
        401: OAuthErrorOut,
        403: OAuthErrorOut,
        404: OAuthErrorOut,
    },
)
def disconnect(
    request: HttpRequest,
    plugin_id: uuid.UUID,
    server_id: uuid.UUID,
    service_id: uuid.UUID,
    organization_credential: bool = False,
):
    denied = _deny_non_user_or_api_key(request)
    if denied:
        return denied
    org_id = _org_id(request)
    organizations = OrganizationService()
    try:
        organizations.require_membership(request.user, org_id)
        server = _server(plugin_id, server_id, org_id)
    except NotFoundError:
        return 404, OAuthErrorOut(detail="Plugin not found", code="not_found")
    except AuthenticationError:
        return 404, OAuthErrorOut(detail="Plugin not found", code="not_found")
    if (
        organization_credential
        and organizations.get_user_role(request.user, org_id) != "admin"
    ):
        return 403, OAuthErrorOut(detail="Admin role required", code="forbidden")
    requirement = server.plugin.credential_requirements.filter(
        key=server.oauth_requirement_key,
        credential_service_id=service_id,
        credential_service__credential_type=CredentialType.MCP_OAUTH,
    ).first()
    if requirement is None:
        return 404, OAuthErrorOut(detail="OAuth service not found", code="not_found")
    query = Credential.objects.filter(
        service_id=service_id,
        oauth_server_id=server.id,
        oauth_server_url=server.url,
    )
    query = (
        query.filter(organization_id=org_id)
        if organization_credential
        else query.filter(user=request.user)
    )
    # Preserve workspace associations. Invalidate locally and allow the user
    # to detach this now-disconnected credential through the normal workspace API.
    from .mcp_oauth import disconnect_credential

    for credential_id in query.values_list("id", flat=True):
        disconnect_credential(credential_id, server_id=server.id, server_url=server.url)
    return 204, None


def callback(request: HttpRequest):
    """Fixed public redirect endpoint; state/error/code never reach frontend URL."""
    try:
        target = frontend_return_url()
    except OAuthError:
        # This is a server configuration fault. Do not fall back to a caller URL.
        return HttpResponse("OAuth callback is not configured", status=500)
    raw_state = request.GET.get("state", "")
    cookie_name = state_cookie_name(raw_state) if raw_state else ""
    binding = request.COOKIES.get(cookie_name, "") if cookie_name else ""
    outcome = "error"
    try:
        complete_flow(
            raw_state=raw_state,
            binding=binding,
            code=request.GET.get("code", ""),
            error=request.GET.get("error", ""),
        )
        outcome = "connected"
    except OAuthError:
        outcome = "error"
    except Exception:
        outcome = "error"
    response = HttpResponseRedirect(
        target + ("&" if "?" in target else "?") + urlencode({"mcp_oauth": outcome})
    )
    if cookie_name:
        response.delete_cookie(cookie_name, path="/api/v1/mcp-oauth/callback/")
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response
