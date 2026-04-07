"""Pydantic schemas for the skills REST API."""

from __future__ import annotations

import uuid
from datetime import datetime

from ninja import Schema


class SessionSkillOut(Schema):
    """Snapshot of a skill attached to a session."""

    id: uuid.UUID
    skill_id: uuid.UUID | None
    name: str
    body: str
    created_at: datetime


class SkillOut(Schema):
    """Response schema for a skill."""

    id: uuid.UUID
    name: str
    body: str
    scope: str  # "personal" or "organization"
    created_by_email: str | None = None
    created_at: datetime
    updated_at: datetime


class SkillCreateIn(Schema):
    """Payload for creating a skill."""

    name: str
    body: str
    organization_skill: bool = False


class SkillUpdateIn(Schema):
    """Payload for updating a skill."""

    name: str | None = None
    body: str | None = None
