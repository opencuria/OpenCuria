"""
Django Ninja authentication classes for JWT-based and API key-based protection.

Uses the pluggable AuthBackend to validate JWT tokens. API key auth
looks up hashed tokens in the APIKey model.

Pass all three to NinjaAPI(auth=[JWTAuth(), APIKeyBearer(), APIKeyInHeader()])
so Django Ninja tries each in order and accepts the first that succeeds.
"""

from __future__ import annotations

import logging
from datetime import timezone as dt_timezone

from django.http import HttpRequest
from django.utils import timezone
from ninja.security import APIKeyHeader, HttpBearer

logger = logging.getLogger(__name__)

_KAI_PREFIX = "kai_"


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
        if token.startswith(_KAI_PREFIX):
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
        if not token.startswith(_KAI_PREFIX):
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

    Hashes the token, looks up the APIKey record, checks active/expiry,
    updates last_used_at, sets request.user, and attaches the APIKey instance
    to ``request.api_key`` for permission checking downstream.
    """
    from common.utils import hash_token

    from .models import APIKey

    token_hash = hash_token(token)

    try:
        api_key = APIKey.objects.select_related("user").get(
            key_hash=token_hash,
            is_active=True,
        )
    except APIKey.DoesNotExist:
        return None

    if api_key.expires_at is not None:
        now = timezone.now()
        if api_key.expires_at.tzinfo is None:
            # Make naive datetimes comparable
            from datetime import datetime
            now = now.replace(tzinfo=None)  # type: ignore[assignment]
        if now > api_key.expires_at:
            return None

    api_key.last_used_at = timezone.now()
    api_key.save(update_fields=["last_used_at"])

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
