"""
REST API endpoints for authentication.

Public endpoints (auth=None): login, refresh, providers, SSO callback.
API key management requires a valid JWT.
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.api_auth import JWTAuth
from apps.accounts.auth_backends import DjangoJWTBackend, get_auth_backend
from apps.accounts.sso import KeycloakSsoService, get_sso_provider_config
from common.exceptions import AuthenticationError, NotFoundError, ServiceError

from .schemas import (
    APIKeyCreatedOut,
    APIKeyCreateIn,
    APIKeyOut,
    APIKeyUpdateIn,
    AuthProvidersOut,
    LoginIn,
    RefreshIn,
    SsoCallbackIn,
    SsoProviderOut,
    TokenOut,
)
from .services import APIKeyService

auth_router = Router(tags=["auth"])


@auth_router.post(
    "/login/",
    response={200: TokenOut, 401: dict},
    auth=None,
    summary="Login with email and password",
)
def login(request: HttpRequest, payload: LoginIn):
    """Authenticate user and return JWT tokens."""
    backend = get_auth_backend()

    try:
        user = backend.authenticate(
            email=payload.email,
            password=payload.password,
        )
    except AuthenticationError as e:
        return 401, {"detail": e.message, "code": e.code}

    tokens = backend.generate_tokens(user)
    return 200, TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


@auth_router.post(
    "/refresh/",
    response={200: TokenOut, 401: dict},
    auth=None,
    summary="Refresh access token",
)
def refresh(request: HttpRequest, payload: RefreshIn):
    """Exchange a refresh token for a new token pair."""
    backend = get_auth_backend()

    try:
        tokens = backend.refresh_tokens(payload.refresh_token)
    except AuthenticationError as e:
        return 401, {"detail": e.message, "code": e.code}

    return 200, TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


@auth_router.get(
    "/providers/",
    response=AuthProvidersOut,
    auth=None,
    summary="Get available authentication providers",
)
def providers(request: HttpRequest):
    """Return available login methods for the current deployment."""
    provider = get_sso_provider_config()
    sso = SsoProviderOut(enabled=False)
    if provider is not None:
        sso = SsoProviderOut(
            enabled=True,
            provider=provider.provider,
            authorization_endpoint=provider.authorization_endpoint,
            client_id=provider.client_id,
            scope=provider.scope,
            supports_pkce=provider.supports_pkce,
        )
    return AuthProvidersOut(password_enabled=True, sso=sso)


@auth_router.post(
    "/sso/callback/",
    response={200: TokenOut, 401: dict, 403: dict},
    auth=None,
    summary="Exchange SSO authorization code for opencuria tokens",
)
def sso_callback(request: HttpRequest, payload: SsoCallbackIn):
    """Validate external SSO login and issue local JWT tokens."""
    if get_sso_provider_config() is None:
        return 403, {"detail": "SSO is not enabled.", "code": "sso_disabled"}

    try:
        user = KeycloakSsoService().exchange_code_for_user(
            code=payload.code,
            redirect_uri=payload.redirect_uri,
        )
    except AuthenticationError as e:
        return 401, {"detail": e.message, "code": e.code}

    tokens = DjangoJWTBackend().generate_tokens(user)
    return 200, TokenOut(
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


# ---------------------------------------------------------------------------
# API key management — JWT sessions only; API keys must not manage credentials.
# ---------------------------------------------------------------------------


@auth_router.get(
    "/api-keys/",
    response=list[APIKeyOut],
    auth=JWTAuth(),
    summary="List API keys",
)
def list_api_keys(request: HttpRequest):
    """Return all API keys for the authenticated user. Raw tokens are never returned."""
    keys = APIKeyService().list_keys(request.user)
    return [
        APIKeyOut(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
            expires_at=k.expires_at,
            permissions=k.permissions or [],
        )
        for k in keys
    ]


@auth_router.post(
    "/api-keys/",
    response={201: APIKeyCreatedOut, 400: dict},
    auth=JWTAuth(),
    summary="Create API key",
)
def create_api_key(request: HttpRequest, payload: APIKeyCreateIn):
    """
    Create a new long-lived API key.

    The raw token is returned **once** in the response and cannot be retrieved again.
    Store it securely — opencuria only stores a SHA-256 hash.
    """
    try:
        api_key, raw_token = APIKeyService().create_key(
            user=request.user,
            name=payload.name,
            expires_at=payload.expires_at,
            permissions=payload.permissions,
        )
    except ServiceError as e:
        return 400, {"detail": e.message, "code": e.code}

    return 201, APIKeyCreatedOut(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        permissions=api_key.permissions or [],
        key=raw_token,
    )


@auth_router.patch(
    "/api-keys/{key_id}/",
    response={200: APIKeyOut, 400: dict, 404: dict},
    auth=JWTAuth(),
    summary="Update API key permissions",
)
def update_api_key(request: HttpRequest, key_id: uuid.UUID, payload: APIKeyUpdateIn):
    """
    Update the permissions of an existing API key.
    """
    try:
        api_key = APIKeyService().update_permissions(
            user=request.user, key_id=key_id, permissions=payload.permissions
        )
    except NotFoundError:
        return 404, {"detail": "API key not found.", "code": "not_found"}
    except ServiceError as e:
        return 400, {"detail": e.message, "code": e.code}

    return 200, APIKeyOut(
        id=api_key.id,
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        is_active=api_key.is_active,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        permissions=api_key.permissions or [],
    )


@auth_router.delete(
    "/api-keys/{key_id}/",
    response={204: None, 404: dict},
    auth=JWTAuth(),
    summary="Revoke API key",
)
def delete_api_key(request: HttpRequest, key_id: uuid.UUID):
    """
    Revoke an API key.

    Sets ``is_active=False`` immediately. The key can no longer be used
    for authentication. Only the owner can revoke their own keys.
    """
    try:
        APIKeyService().revoke(user=request.user, key_id=key_id)
    except NotFoundError:
        return 404, {"detail": "API key not found.", "code": "not_found"}
    return 204, None


@auth_router.get(
    "/api-key-permissions/",
    response=list[dict],
    summary="List available API key permissions",
    auth=None,
)
def list_api_key_permissions(request: HttpRequest):
    """Return all available permission strings with descriptions."""
    from .models import APIKeyPermission

    permission_descriptions = {
        APIKeyPermission.WORKSPACES_READ: "List and view workspaces",
        APIKeyPermission.WORKSPACES_CREATE: "Create new workspaces",
        APIKeyPermission.WORKSPACES_UPDATE: "Update workspace metadata",
        APIKeyPermission.WORKSPACES_STOP: "Stop running workspaces",
        APIKeyPermission.WORKSPACES_RESUME: "Resume stopped workspaces",
        APIKeyPermission.WORKSPACES_DELETE: "Delete workspaces",
        APIKeyPermission.WORKSPACES_GIT_READ: "View git status, diffs and commit details",
        APIKeyPermission.WORKSPACES_GIT_WRITE: "Run git operations (stage, commit, branches, merge, fetch/pull/push)",
        APIKeyPermission.TERMINAL_ACCESS: "Open interactive terminal sessions",
        APIKeyPermission.WORKSPACES_PROCESSES_READ: "List and view background processes",
        APIKeyPermission.WORKSPACES_PROCESSES_RUN: "Start and stop background processes",
        APIKeyPermission.RUNNERS_READ: "List and view runners",
        APIKeyPermission.RUNNERS_CREATE: "Register new runners",
        APIKeyPermission.ORGANIZATIONS_READ: "List and view organizations",
        APIKeyPermission.ORGANIZATIONS_WRITE: "Create organizations",
        APIKeyPermission.CREDENTIALS_READ: "View credentials (metadata only)",
        APIKeyPermission.CREDENTIALS_WRITE: "Create, update, delete credentials",
        APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ: "List organization credential service activations",
        APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE: "Activate and deactivate organization credential services",
        APIKeyPermission.IMAGES_READ: "List and view image artifacts",
        APIKeyPermission.IMAGES_CREATE: "Create image artifacts from workspaces",
        APIKeyPermission.IMAGES_DELETE: "Delete image artifacts",
        APIKeyPermission.IMAGES_CLONE: "Create workspaces from image artifacts",
        APIKeyPermission.IMAGE_DEFINITIONS_READ: "List image definitions",
        APIKeyPermission.IMAGE_DEFINITIONS_WRITE: "Create, update and delete image definitions",
        APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS: "Assign, activate, deactivate and rebuild image definitions on runners",
        APIKeyPermission.SKILLS_READ: "List and view skills",
        APIKeyPermission.SKILLS_WRITE: "Create, update, delete skills",
        APIKeyPermission.PLUGINS_READ: "List and view plugins",
        APIKeyPermission.PLUGINS_WRITE: (
            "Create, update, delete plugins and manage activations"
        ),
        APIKeyPermission.HARNESS_READ: "List and view harness sessions",
        APIKeyPermission.HARNESS_RUN: "Create harness sessions and send prompts",
        APIKeyPermission.HARNESS_PERMISSIONS: "Resolve harness permission requests",
        APIKeyPermission.HARNESS_PROVIDERS: "Manage provider connections",
    }

    return [
        {
            "value": perm.value,
            "label": perm.value.replace(":", " → ").replace("_", " ").title(),
            "description": permission_descriptions.get(perm, ""),
            "group": perm.value.split(":")[0],
        }
        for perm in APIKeyPermission
    ]
