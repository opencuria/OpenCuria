"""
Repository layer for the accounts app.

Encapsulates all database queries and writes for users and API keys.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.db.models import QuerySet
from django.utils import timezone

from .models import APIKey, User


class UserRepository:
    """Local user persistence shared by password/JWT and SSO authentication."""

    @staticmethod
    def get_by_email(email: str) -> User | None:
        """Fetch a user by exact email, including disabled accounts."""
        try:
            return User.objects.get(email=email)
        except User.DoesNotExist:
            return None

    @staticmethod
    def get_by_id(user_id: int) -> User | None:
        """Fetch a user by primary key, including disabled accounts."""
        try:
            return User.objects.get(id=user_id)
        except User.DoesNotExist:
            return None

    @staticmethod
    def email_exists(email: str) -> bool:
        """Return whether the exact email is already registered."""
        return User.objects.filter(email=email).exists()

    @staticmethod
    def verify_password(user: User, password: str) -> bool:
        """Check a password, persisting Django's hash upgrade when needed."""
        return user.check_password(password)

    @staticmethod
    def create_user(*, email: str, password: str, **fields: Any) -> User:
        """Create a user through the manager's normalization and password hashing."""
        return User.objects.create_user(
            username=email, email=email, password=password, **fields
        )

    @staticmethod
    def get_or_create_by_email(
        email: str, *, defaults: dict[str, Any]
    ) -> tuple[User, bool]:
        """Fetch an account or insert one with the supplied profile defaults."""
        return User.objects.get_or_create(email=email, defaults=defaults)

    @staticmethod
    def update_profile(user: User, **fields: str) -> User:
        """Persist only the supplied profile changes."""
        for field, value in fields.items():
            setattr(user, field, value)
        if fields:
            user.save(update_fields=list(fields))
        return user


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
