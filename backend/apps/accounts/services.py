"""
Service layer for the accounts app.

All business logic for API keys lives here. REST and MCP authenticate
API keys through the same ``APIKeyService``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.utils import timezone

from common.exceptions import NotFoundError, ServiceError
from common.utils import generate_api_token, hash_token

from .models import APIKey, APIKeyPermission, User
from .repositories import APIKeyRepository

#: Prefix that marks a bearer token as an API key rather than a JWT.
API_KEY_TOKEN_PREFIX = "kai_"
#: Length of the token prefix stored in clear text to identify a key.
_DISPLAY_PREFIX_LENGTH = 12


class APIKeyService:
    """Issue, manage and authenticate long-lived API keys.

    Only the SHA-256 hash of a token is persisted; the raw token is returned
    exactly once by :meth:`create_key`.
    """

    def __init__(self) -> None:
        self.keys = APIKeyRepository

    def list_keys(self, user: User) -> list[APIKey]:
        """Return all keys of *user*, newest first."""
        return list(self.keys.list_for_user(user))

    def create_key(
        self,
        *,
        user: User,
        name: str,
        expires_at: datetime | None,
        permissions: list[str] | None,
    ) -> tuple[APIKey, str]:
        """Create a key and return it with its raw token.

        Omitted or empty *permissions* grant every permission.
        """
        granted = self._validated_permissions(permissions)
        raw_token = f"{API_KEY_TOKEN_PREFIX}{generate_api_token()}"
        api_key = self.keys.create(
            user=user,
            name=name,
            key_hash=hash_token(raw_token),
            key_prefix=raw_token[:_DISPLAY_PREFIX_LENGTH],
            expires_at=expires_at,
            permissions=granted,
        )
        return api_key, raw_token

    def update_permissions(
        self, *, user: User, key_id: uuid.UUID, permissions: list[str] | None
    ) -> APIKey:
        """Replace the permissions of a key owned by *user*.

        Omitted or empty *permissions* grant every permission.
        """
        api_key = self._get_owned(key_id, user)
        return self.keys.update_permissions(
            api_key, self._validated_permissions(permissions)
        )

    def revoke(self, *, user: User, key_id: uuid.UUID) -> None:
        """Deactivate a key owned by *user*."""
        self.keys.deactivate(self._get_owned(key_id, user))

    def authenticate(
        self,
        token: str,
        *,
        required_permission: APIKeyPermission | None = None,
    ) -> APIKey | None:
        """Return the active, unexpired key for *token* and record its use.

        Returns None when the token is unknown, revoked, expired or lacks
        *required_permission*; rejected attempts never count as a use.
        """
        if not token:
            return None
        api_key = self.keys.get_active_by_hash(hash_token(token))
        if api_key is None:
            return None
        if api_key.expires_at is not None and timezone.now() > api_key.expires_at:
            return None
        if required_permission is not None and not api_key.has_permission(
            required_permission
        ):
            return None
        return self.keys.touch_last_used(api_key)

    def _get_owned(self, key_id: uuid.UUID, user: User) -> APIKey:
        api_key = self.keys.get_for_user(key_id, user)
        if api_key is None:
            raise NotFoundError("API key", str(key_id))
        return api_key

    @staticmethod
    def _validated_permissions(permissions: list[str] | None) -> list[str]:
        valid = APIKeyPermission.all_values()
        invalid = [p for p in (permissions or []) if p not in valid]
        if invalid:
            raise ServiceError(
                f"Invalid permissions: {invalid}", code="invalid_permissions"
            )
        return permissions or valid
