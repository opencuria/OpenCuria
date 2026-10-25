"""
Django Ninja authentication classes for JWT-based and API key-based protection.

Uses the pluggable AuthBackend to validate JWT tokens. API key auth
delegates to ``APIKeyService``.

Pass all three to NinjaAPI(auth=[JWTAuth(), APIKeyBearer(), APIKeyInHeader()])
so Django Ninja tries each in order and accepts the first that succeeds.
"""

from __future__ import annotations

import logging

from django.http import HttpRequest
from ninja.security import APIKeyHeader, HttpBearer

from .services import API_KEY_TOKEN_PREFIX, APIKeyService

logger = logging.getLogger(__name__)


class JWTAuth(HttpBearer):
    """
    Django Ninja HTTP Bearer authentication using JWT.

    Skips tokens that start with ``kai_`` so that API key tokens fall
    through to the next auth backend in the list.

    On successful authentication, ``request.user`` is set to the
    authenticated User instance.
    """

    def authenticate(self, request: HttpRequest, token: str):
        """
        Validate the Bearer token and return the User.

        Returns the User instance on success, None on failure
        (Django Ninja converts None to 401).
        """
        if token.startswith(API_KEY_TOKEN_PREFIX):
            return None

        from apps.accounts.auth_backends import get_auth_backend
        from common.exceptions import AuthenticationError

        backend = get_auth_backend()

        try:
            user = backend.validate_access_token(token)
        except AuthenticationError:
            return None

        request.user = user
        return user


class APIKeyBearer(HttpBearer):
    """
    Django Ninja HTTP Bearer authentication using long-lived API keys.

    Handles ``Authorization: Bearer kai_xxxx`` tokens. Tokens not
    starting with ``kai_`` are skipped so JWT tokens fall through to
    the JWTAuth backend.
    """

    def authenticate(self, request: HttpRequest, token: str):
        """
        Validate the API key token and return the User.

        Returns the User instance on success, None on failure.
        """
        if not token.startswith(API_KEY_TOKEN_PREFIX):
            return None

        return _authenticate_api_key(request, token)


class APIKeyInHeader(APIKeyHeader):
    """
    Django Ninja API key header authentication.

    Handles ``X-API-Key: kai_xxxx`` for tools like n8n and Zapier
    that use a dedicated API key header rather than Authorization Bearer.
    """

    param_name = "X-API-Key"

    def authenticate(self, request: HttpRequest, key: str | None):
        """
        Validate the X-API-Key header value and return the User.

        Returns the User instance on success, None on failure.
        """
        if not key:
            return None
        return _authenticate_api_key(request, key)


def _authenticate_api_key(request: HttpRequest, token: str):
    """
    Shared API key validation logic used by both APIKeyBearer and APIKeyInHeader.

    Authenticates the token via ``APIKeyService``, sets request.user, and
    attaches the APIKey instance to ``request.api_key`` for permission
    checking downstream.
    """
    api_key = APIKeyService().authenticate(token)
    if api_key is None:
        return None

    request.user = api_key.user
    # Attach the full APIKey object so downstream code can check permissions
    request.api_key = api_key  # type: ignore[attr-defined]
    return api_key.user


def check_api_key_permission(request: HttpRequest, permission: str) -> bool:
    """
    Check if the current request's API key grants the given permission.

    JWT-authenticated requests (no api_key attached) always pass — permissions
    only apply to API key authentication. Returns True if access is allowed.
    """
    api_key = getattr(request, "api_key", None)
    if api_key is None:
        # JWT auth — full access
        return True
    return api_key.has_permission(permission)


def require_api_key_permission(permission: str):
    """
    Django Ninja compatible permission guard for a single permission string.

    Usage in a view::

        from apps.accounts.api_auth import require_api_key_permission
        from apps.accounts.models import APIKeyPermission

        @router.get("/")
        def list_things(request):
            if not check_api_key_permission(request, APIKeyPermission.THINGS_READ):
                return 403, ErrorOut(detail="Permission denied", code="forbidden")
            ...
    """
    from .models import APIKeyPermission as _P

    # Normalise — accept both enum members and plain strings
    perm_value = permission.value if isinstance(permission, _P) else permission

    def _check(request: HttpRequest) -> bool:
        return check_api_key_permission(request, perm_value)

    return _check
