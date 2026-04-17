"""
Pydantic schemas for the credentials REST API.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ninja import Schema

# --- Credential Service (catalog) ---


class CredentialServiceOut(Schema):
    """A credential service with its type and injection details."""

    id: uuid.UUID
    name: str
    slug: str
    description: str
    credential_type: str
    env_var_name: str
    target_path: str
    label: str


class CredentialServiceCreateIn(Schema):
    """Payload for creating a credential service."""

    name: str
    slug: str = ""
    description: str = ""
    credential_type: str
    env_var_name: str = ""
    target_path: str = ""
    label: str = ""


# --- Credential ---


class CredentialCreateIn(Schema):
    """Payload for creating a new credential."""

    service_id: uuid.UUID
    name: str = ""
    value: str | None = None  # plaintext - not required for SSH keys
    organization_credential: bool = False  # True → org-owned, False → personal


class CredentialUpdateIn(Schema):
    """Payload for updating an existing credential."""

    name: str | None = None
    value: str | None = None  # plaintext - encrypted server-side


class CredentialOut(Schema):
    """Credential metadata - value is never exposed."""

    id: uuid.UUID
    name: str
    scope: str  # "personal" or "organization"
    service_id: uuid.UUID
    service_name: str
    service_slug: str
    credential_type: str
    env_var_name: str
    target_path: str
    has_public_key: bool
    created_by_id: int
    created_at: datetime
    updated_at: datetime


class PublicKeyOut(Schema):
    """SSH public key for a credential."""

    public_key: str


class CredentialServiceWithActivationOut(Schema):
    """A credential service with its activation status for an organization."""

    id: uuid.UUID
    name: str
    slug: str
    description: str
    credential_type: str
    env_var_name: str
    target_path: str
    label: str
    is_active: bool = False


class CredentialServiceActivationToggleIn(Schema):
    """Request schema for activating/deactivating a credential service for an org."""

    active: bool
