"""
Repository layer for the accounts app.

Encapsulates all database queries for APIKey records.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db.models import QuerySet
from django.utils import timezone

from .models import APIKey, User


class APIKeyRepository:
    """Data access for APIKey records."""

    @staticmethod
    def list_for_user(user: User) -> QuerySet[APIKey]:
        """Return all keys of a user, newest first."""
        return APIKey.objects.filter(user=user).order_by("-created_at")

    @staticmethod
    def get_for_user(key_id: uuid.UUID, user: User) -> APIKey | None:
        """Fetch one key owned by *user*."""
        return APIKey.objects.filter(id=key_id, user=user).first()

    @staticmethod
    def get_active_by_hash(key_hash: str) -> APIKey | None:
        """Fetch an active key and its user by token hash."""
        return (
            APIKey.objects.select_related("user")
            .filter(key_hash=key_hash, is_active=True)
            .first()
        )

    @staticmethod
    def create(
        *,
        user: User,
        name: str,
        key_hash: str,
        key_prefix: str,
        expires_at: datetime | None,
        permissions: list[str],
    ) -> APIKey:
        """Create a new key from an already hashed token."""
        return APIKey.objects.create(
            user=user,
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            expires_at=expires_at,
            permissions=permissions,
        )

    @staticmethod
    def update_permissions(api_key: APIKey, permissions: list[str]) -> APIKey:
        """Persist a new permission allowlist."""
        api_key.permissions = permissions
        api_key.save(update_fields=["permissions"])
        return api_key

    @staticmethod
    def deactivate(api_key: APIKey) -> APIKey:
        """Revoke a key."""
        api_key.is_active = False
        api_key.save(update_fields=["is_active"])
        return api_key

    @staticmethod
    def touch_last_used(api_key: APIKey) -> APIKey:
        """Record that a key was just used to authenticate."""
        api_key.last_used_at = timezone.now()
        api_key.save(update_fields=["last_used_at"])
        return api_key
