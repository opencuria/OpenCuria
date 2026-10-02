"""Service-owned MCP OAuth API and fixed public callback endpoint."""

from __future__ import annotations

import json
import uuid
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from ninja import Router, Schema

from apps.accounts.api_auth import check_api_key_permission
from apps.accounts.models import APIKeyPermission
from apps.organizations.repositories import OrganizationRepository
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .mcp_oauth import OAuthError, frontend_return_url, state_cookie_name
from .services import CredentialOAuthSvc


class OAuthConnectIn(Schema):
    """Start an independent OAuth account or reconnect a credential."""

    name: str = ""
    organization_credential: bool = False


class OAuthConnectOut(Schema):
    authorization_url: str
    status: str = "pending"


class OAuthErrorOut(Schema):
    detail: str
    code: str


service_oauth_router = Router(tags=["credential-oauth"])
credential_oauth_router = Router(tags=["credential-oauth"])


def _org_id(request: HttpRequest) -> uuid.UUID:
    value = request.headers.get("X-Organization-Id", "")
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError):
        raise AuthenticationError("X-Organization-Id header is required") from None


def _deny_anonymous(request: HttpRequest):
    if not getattr(request.user, "is_authenticated", False):
        return 401, OAuthErrorOut(
            detail="Authentication required", code="not_authenticated"
        )
    return None


def _deny_interactive_api_key(request: HttpRequest):
    denied = _deny_anonymous(request)
    if denied:
        return denied
    if getattr(request, "api_key", None) is not None:
        return 403, OAuthErrorOut(
            detail="OAuth connect requires an interactive JWT session", code="forbidden"
        )
    return None


def _get_organization(request: HttpRequest, org_id: uuid.UUID):
    """Load selected organization; OAuth service authorizes membership."""
    organization = OrganizationRepository.get_by_id(org_id)
    if organization is None:
        raise NotFoundError("Organization", str(org_id))
    return organization


def _start_response(
    authorization_url: str, binding: str, cookie_name: str
) -> HttpResponse:
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


def _begin_response(request: HttpRequest, *, service_id, credential_id, payload):
    """Call the OAuth service and format its interactive response."""
    org_id = _org_id(request)
    organization = _get_organization(request, org_id)
    authorization_url, binding, cookie_name = CredentialOAuthSvc().start_connection(
        user=request.user,
        organization=organization,
        organization_credential=payload.organization_credential,
        service_id=service_id,
        credential_id=credential_id,
        name=payload.name,
        request_scheme="https" if request.is_secure() else "http",
        request_host=request.get_host(),
    )
    return _start_response(authorization_url, binding, cookie_name)


@service_oauth_router.post(
    "/{service_id}/oauth/connect/",
    response={
        200: OAuthConnectOut,
        400: OAuthErrorOut,
        401: OAuthErrorOut,
        403: OAuthErrorOut,
        404: OAuthErrorOut,
        409: OAuthErrorOut,
    },
)
def connect(request: HttpRequest, service_id: uuid.UUID, payload: OAuthConnectIn):
    """Start a fresh service-scoped OAuth authorization transaction."""
    denied = _deny_interactive_api_key(request)
    if denied:
        return denied
    try:
        return _begin_response(
            request, service_id=service_id, credential_id=None, payload=payload
        )
    except AuthenticationError as exc:
        return 403, OAuthErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, OAuthErrorOut(detail=exc.message, code=exc.code)
    except ConflictError as exc:
        return 409, OAuthErrorOut(detail=exc.message, code=exc.code)
    except OAuthError as exc:
        return 400, OAuthErrorOut(detail=str(exc), code="oauth_error")
    except Exception:
        return 400, OAuthErrorOut(
            detail="OAuth connection could not be started", code="oauth_error"
        )


@credential_oauth_router.post(
    "/{credential_id}/oauth/reconnect/",
    response={
        200: OAuthConnectOut,
        400: OAuthErrorOut,
        401: OAuthErrorOut,
        403: OAuthErrorOut,
        404: OAuthErrorOut,
        409: OAuthErrorOut,
    },
)
def reconnect(request: HttpRequest, credential_id: uuid.UUID, payload: OAuthConnectIn):
    """Reconnect one explicit personal or organization credential row."""
    denied = _deny_interactive_api_key(request)
    if denied:
        return denied
    try:
        return _begin_response(
            request, service_id=None, credential_id=credential_id, payload=payload
        )
    except AuthenticationError as exc:
        return 403, OAuthErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError as exc:
        return 404, OAuthErrorOut(detail=exc.message, code=exc.code)
    except ConflictError as exc:
        return 409, OAuthErrorOut(detail=exc.message, code=exc.code)
    except OAuthError as exc:
        return 400, OAuthErrorOut(detail=str(exc), code="oauth_error")
    except Exception:
        return 400, OAuthErrorOut(
            detail="OAuth reconnection could not be started", code="oauth_error"
        )


@credential_oauth_router.delete(
    "/{credential_id}/oauth/disconnect/",
    response={
        204: None,
        401: OAuthErrorOut,
        403: OAuthErrorOut,
        404: OAuthErrorOut,
        409: OAuthErrorOut,
    },
)
def disconnect(request: HttpRequest, credential_id: uuid.UUID):
    """Clear OAuth grant material while preserving workspace attachments."""
    denied = _deny_anonymous(request)
    if denied:
        return denied
    if getattr(request, "api_key", None) is not None and not check_api_key_permission(
        request, APIKeyPermission.CREDENTIALS_WRITE
    ):
        return 403, OAuthErrorOut(detail="Permission denied", code="permission_denied")
    try:
        org_id = _org_id(request)
        CredentialOAuthSvc().disconnect(
            credential_id=credential_id, user=request.user, organization_id=org_id
        )
        return 204, None
    except AuthenticationError as exc:
        return 403, OAuthErrorOut(detail=exc.message, code=exc.code)
    except NotFoundError:
        return 404, OAuthErrorOut(detail="OAuth credential not found", code="not_found")


def callback(request: HttpRequest):
    """Complete a one-use callback and redirect only to the fixed frontend."""
    try:
        target = frontend_return_url()
    except OAuthError:
        return HttpResponse("OAuth callback is not configured", status=500)
    raw_state = request.GET.get("state", "")
    cookie_name = state_cookie_name(raw_state) if raw_state else ""
    binding = request.COOKIES.get(cookie_name, "") if cookie_name else ""
    outcome, credential_id = "error", ""
    try:
        credential_id = str(
            CredentialOAuthSvc().complete_authorization(
                raw_state=raw_state,
                binding=binding,
                code=request.GET.get("code", ""),
                error=request.GET.get("error", ""),
            )
        )
        outcome = "connected"
    except Exception:
        outcome = "error"
    params = {"oauth_result": outcome}
    if credential_id and outcome == "connected":
        params["credential_id"] = credential_id
    response = HttpResponseRedirect(
        target + ("&" if "?" in target else "?") + urlencode(params)
    )
    if cookie_name:
        response.delete_cookie(cookie_name, path="/api/v1/mcp-oauth/callback/")
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Referrer-Policy"] = "no-referrer"
    response["X-Content-Type-Options"] = "nosniff"
    return response
