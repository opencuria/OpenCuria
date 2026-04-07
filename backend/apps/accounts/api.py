"""
REST API endpoints for authentication.

Public endpoints (auth=None): register, login, refresh.
Protected endpoint: me (requires valid JWT).
"""

from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.accounts.auth_backends import DjangoJWTBackend, get_auth_backend
from apps.accounts.sso import KeycloakSsoService, get_sso_provider_config
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError

from .schemas import (
    APIKeyCreatedOut,
    APIKeyCreateIn,
    APIKeyOut,
    APIKeyUpdateIn,
    AuthProvidersOut,
    LoginIn,
    RefreshIn,
    RegisterIn,
    SsoCallbackIn,
    SsoProviderOut,
    TokenOut,
    UserOrgOut,
    UserWithOrgsOut,
)

auth_router = Router(tags=["auth"])


@auth_router.post(
    "/register/",
    response={201: TokenOut, 403: dict, 409: dict},
    auth=None,
    summary="Register a new user",
)
def register(request: HttpRequest, payload: RegisterIn):
    """Create a new user account and return JWT tokens.

    Registration is currently disabled — the system is closed.
    """
    return 403, {"detail": "Registration is currently disabled.", "code": "registration_disabled"}


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


@auth_router.get(
    "/me/",
    response=UserWithOrgsOut,
    summary="Get current user",
)
def me(request: HttpRequest):
    """Return the authenticated user's profile with organization memberships."""
    user = request.user
    org_service = OrganizationService()
    org_list = org_service.list_user_organizations(user)

    return UserWithOrgsOut(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        organizations=[
            UserOrgOut(
                id=o["id"],
                name=o["name"],
                slug=o["slug"],
                role=o["role"],
                created_at=o["created_at"],
            )
            for o in org_list
        ],
    )


# ---------------------------------------------------------------------------
# API key management
# ---------------------------------------------------------------------------


@auth_router.get(
    "/api-keys/",
    response=list[APIKeyOut],
    summary="List API keys",
)
def list_api_keys(request: HttpRequest):
    """Return all API keys for the authenticated user. Raw tokens are never returned."""
    from .models import APIKey

    keys = APIKey.objects.filter(user=request.user).order_by("-created_at")
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
    summary="Create API key",
)
def create_api_key(request: HttpRequest, payload: APIKeyCreateIn):
    """
    Create a new long-lived API key.

    The raw token is returned **once** in the response and cannot be retrieved again.
    Store it securely — opencuria only stores a SHA-256 hash.
    """
    from common.utils import generate_api_token, hash_token

    from .models import APIKey

    raw_token = f"kai_{generate_api_token()}"
    token_hash = hash_token(raw_token)
    key_prefix = raw_token[:12]

    from .models import APIKeyPermission

    # Validate permissions
    valid_perms = APIKeyPermission.all_values()
    invalid = [p for p in (payload.permissions or []) if p not in valid_perms]
    if invalid:
        return 400, {"detail": f"Invalid permissions: {invalid}", "code": "invalid_permissions"}

    permissions = payload.permissions or valid_perms

    api_key = APIKey.objects.create(
        user=request.user,
        name=payload.name,
        key_hash=token_hash,
        key_prefix=key_prefix,
        expires_at=payload.expires_at,
        permissions=permissions,
    )

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
    summary="Update API key permissions",
)
def update_api_key(request: HttpRequest, key_id: uuid.UUID, payload: APIKeyUpdateIn):
    """
    Update the permissions of an existing API key.
    """
    from .models import APIKey, APIKeyPermission

    try:
        api_key = APIKey.objects.get(id=key_id, user=request.user)
    except APIKey.DoesNotExist:
        return 404, {"detail": "API key not found.", "code": "not_found"}

    valid_perms = APIKeyPermission.all_values()
    invalid = [p for p in (payload.permissions or []) if p not in valid_perms]
    if invalid:
        return 400, {"detail": f"Invalid permissions: {invalid}", "code": "invalid_permissions"}

    api_key.permissions = payload.permissions or valid_perms
    api_key.save(update_fields=["permissions"])

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
    summary="Revoke API key",
)
def delete_api_key(request: HttpRequest, key_id: uuid.UUID):
    """
    Revoke an API key.

    Sets ``is_active=False`` immediately. The key can no longer be used
    for authentication. Only the owner can revoke their own keys.
    """
    from .models import APIKey

    try:
        api_key = APIKey.objects.get(id=key_id, user=request.user)
    except APIKey.DoesNotExist:
        return 404, {"detail": "API key not found.", "code": "not_found"}

    api_key.is_active = False
    api_key.save(update_fields=["is_active"])
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
        APIKeyPermission.PROMPTS_RUN: "Run prompts / send messages to agents",
        APIKeyPermission.PROMPTS_CANCEL: "Cancel running prompts / active agent completions",
        APIKeyPermission.TERMINAL_ACCESS: "Open interactive terminal sessions",
        APIKeyPermission.RUNNERS_READ: "List and view runners",
        APIKeyPermission.RUNNERS_CREATE: "Register new runners",
        APIKeyPermission.ORGANIZATIONS_READ: "List and view organizations",
        APIKeyPermission.ORGANIZATIONS_WRITE: "Create organizations",
        APIKeyPermission.AGENTS_READ: "List available agent definitions",
        APIKeyPermission.ORG_AGENT_DEFINITIONS_READ: "List organization agent definitions",
        APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE: "Create, update, delete, and activate organization agent definitions",
        APIKeyPermission.CREDENTIALS_READ: "View credentials (metadata only)",
        APIKeyPermission.CREDENTIALS_WRITE: "Create, update, delete credentials",
        APIKeyPermission.ORG_CREDENTIAL_SERVICES_READ: "List organization credential service activations",
        APIKeyPermission.ORG_CREDENTIAL_SERVICES_WRITE: "Activate and deactivate organization credential services",
        APIKeyPermission.CONVERSATIONS_READ: "List and view conversations",
        APIKeyPermission.IMAGES_READ: "List and view image artifacts",
        APIKeyPermission.IMAGES_CREATE: "Create image artifacts from workspaces",
        APIKeyPermission.IMAGES_DELETE: "Delete image artifacts",
        APIKeyPermission.IMAGES_CLONE: "Create workspaces from image artifacts",
        APIKeyPermission.IMAGE_DEFINITIONS_READ: "List image definitions",
        APIKeyPermission.IMAGE_DEFINITIONS_WRITE: "Create, update and delete image definitions",
        APIKeyPermission.IMAGE_DEFINITIONS_MANAGE_RUNNERS: "Assign, activate, deactivate and rebuild image definitions on runners",
        APIKeyPermission.SKILLS_READ: "List and view skills",
        APIKeyPermission.SKILLS_WRITE: "Create, update, delete skills",
        APIKeyPermission.MCP_ACCESS: "Connect via MCP interface",
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
