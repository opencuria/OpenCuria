"""
Pydantic schemas for the accounts/auth REST API.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ninja import Schema
from pydantic import Field

# ---------------------------------------------------------------------------
# Auth schemas
# ---------------------------------------------------------------------------


class RegisterIn(Schema):
    """Request schema for user registration."""

    email: str
    password: str


class LoginIn(Schema):
    """Request schema for login."""

    email: str
    password: str


class RefreshIn(Schema):
    """Request schema for token refresh."""

    refresh_token: str


class TokenOut(Schema):
    """Response schema for JWT token pair."""

    access_token: str
    refresh_token: str


class SsoCallbackIn(Schema):
    """Request schema for OIDC authorization-code callback exchange."""

    code: str
    redirect_uri: str


class SsoProviderOut(Schema):
    """Optional SSO provider settings needed by the frontend."""

    enabled: bool
    provider: str | None = None
    authorization_endpoint: str | None = None
    client_id: str | None = None
    scope: str | None = None
    supports_pkce: bool = True


class AuthProvidersOut(Schema):
    """Supported auth providers for this deployment."""

    password_enabled: bool = True
    sso: SsoProviderOut


# ---------------------------------------------------------------------------
# User schemas
# ---------------------------------------------------------------------------


class UserOut(Schema):
    """Response schema for the current user."""

    id: int
    email: str
    first_name: str
    last_name: str


class UserWithOrgsOut(Schema):
    """Response schema for the current user with their organizations."""

    id: int
    email: str
    first_name: str
    last_name: str
    organizations: list[UserOrgOut] = []


class UserOrgOut(Schema):
    """Organization info within a user response."""

    id: uuid.UUID
    name: str
    slug: str
    role: str
    created_at: datetime


# Fix forward reference
UserWithOrgsOut.model_rebuild()


# ---------------------------------------------------------------------------
# API key schemas
# ---------------------------------------------------------------------------


class APIKeyCreateIn(Schema):
    """Request schema for creating a new API key."""

    name: str = Field(..., max_length=255, description="User-defined label for this key")
    expires_at: datetime | None = Field(None, description="Optional expiry; None = never expires")
    permissions: list[str] = Field(
        default_factory=list,
        description="Allowed permission strings. Empty means all currently available permissions.",
    )


class APIKeyUpdateIn(Schema):
    """Request schema for updating an API key's permissions."""

    permissions: list[str] = Field(
        default_factory=list,
        description="Allowed permission strings. Empty means all currently available permissions.",
    )


class APIKeyOut(Schema):
    """Response schema for an API key (never includes the raw token)."""

    id: uuid.UUID
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    permissions: list[str] = Field(default_factory=list)


class APIKeyCreatedOut(APIKeyOut):
    """Response schema returned once at key creation, includes the raw token."""

    key: str = Field(..., description="Full token — shown only once, cannot be retrieved again")
