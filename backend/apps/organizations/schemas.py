"""
Pydantic schemas for the organizations REST API.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from ninja import Schema


class OrganizationCreateIn(Schema):
    """Request schema for creating an organization."""

    name: str


class OrganizationOut(Schema):
    """Response schema for an organization."""

    id: uuid.UUID
    name: str
    slug: str
    role: str
    workspace_auto_stop_timeout_minutes: int | None = None
    image_versions_to_keep: int = 2
    created_at: datetime


class OrganizationWorkspacePolicyUpdateIn(Schema):
    """Partial update of org workspace policy; omitted fields stay unchanged."""

    workspace_auto_stop_timeout_minutes: int | None = None
    image_versions_to_keep: int | None = None
