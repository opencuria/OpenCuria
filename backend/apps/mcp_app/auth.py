"""
MCP authentication helpers.

Validates API keys from HTTP headers and returns the APIKey model instance.
Only API key auth is supported for MCP — JWT tokens are not forwarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

if TYPE_CHECKING:
    from apps.accounts.models import APIKey


def authenticate_api_key(token: str) -> APIKey | None:
    """
    Validate an API key token and return the APIKey instance.

    Returns None if the token is invalid, expired, or not found.
    The MCP_ACCESS permission is checked here — if the key doesn't have it,
    the connection is rejected.
    """
    from common.utils import hash_token

    from apps.accounts.models import APIKey as APIKeyModel, APIKeyPermission

    if not token:
        return None

    # Strip "Bearer " prefix if present
    if token.startswith("Bearer "):
        token = token[len("Bearer "):]

    token_hash = hash_token(token)

    try:
        api_key = APIKeyModel.objects.select_related("user").get(
            key_hash=token_hash,
            is_active=True,
        )
    except APIKeyModel.DoesNotExist:
        return None

    if api_key.expires_at is not None:
        if timezone.now() > api_key.expires_at:
            return None

    # Check that the key has MCP access.
    if not api_key.has_permission(APIKeyPermission.MCP_ACCESS):
        return None

    api_key.last_used_at = timezone.now()
    api_key.save(update_fields=["last_used_at"])

    return api_key
