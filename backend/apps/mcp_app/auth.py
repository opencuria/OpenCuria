"""
MCP authentication helpers.

Validates API keys from HTTP headers and returns the APIKey model instance.
Only API key auth is supported for MCP — JWT tokens are not forwarded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apps.accounts.models import APIKeyPermission
from apps.accounts.services import APIKeyService

if TYPE_CHECKING:
    from apps.accounts.models import APIKey


def authenticate_api_key(token: str) -> APIKey | None:
    """
    Validate an API key token and return the APIKey instance.

    Returns None if the token is invalid, expired, or not found.
    The MCP_ACCESS permission is checked here — if the key doesn't have it,
    the connection is rejected.
    """
    # Strip "Bearer " prefix if present
    if token.startswith("Bearer "):
        token = token[len("Bearer ") :]

    return APIKeyService().authenticate(
        token, required_permission=APIKeyPermission.MCP_ACCESS
    )
