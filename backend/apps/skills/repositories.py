"""Repository layer for the skills app.

All ORM access is isolated here. Services never call the ORM directly.
"""

from __future__ import annotations

import uuid

from django.db.models import Q, QuerySet

from .models import SessionSkill, Skill


class SkillRepository:
    """Data access for Skill records."""

    @staticmethod
    def list_for_user_in_org(user_id: int, org_id: uuid.UUID) -> QuerySet[Skill]:
        """Return all skills visible to a user in an org:
        - Skills personally owned by this user, OR
        - Skills owned by this organization.
        """
        return Skill.objects.filter(
            Q(user_id=user_id) | Q(organization_id=org_id)
        ).select_related("user", "organization", "created_by")

    @staticmethod
    def get_by_id(skill_id: uuid.UUID) -> Skill | None:
        """Fetch a single skill by primary key."""
        return (
            Skill.objects.filter(id=skill_id)
            .select_related("user", "organization", "created_by")
            .first()
        )

    @staticmethod
    def get_many_by_ids(skill_ids: list[uuid.UUID]) -> list[Skill]:
        """Fetch multiple skills by IDs, preserving order."""
        return list(Skill.objects.filter(id__in=skill_ids))

    @staticmethod
    def create_personal(*, name: str, body: str, user) -> Skill:
        """Create a skill owned by the given user."""
        return Skill.objects.create(name=name, body=body, user=user, created_by=user)

    @staticmethod
    def create_org(*, name: str, body: str, organization_id: uuid.UUID, created_by) -> Skill:
        """Create a skill owned by an organization."""
        return Skill.objects.create(
            name=name,
            body=body,
            organization_id=organization_id,
            created_by=created_by,
        )

    @staticmethod
    def update(skill: Skill, *, name: str | None = None, body: str | None = None) -> Skill:
        """Update mutable fields on an existing skill."""
        update_fields = ["updated_at"]
        if name is not None:
            skill.name = name
            update_fields.append("name")
        if body is not None:
            skill.body = body
            update_fields.append("body")
        skill.save(update_fields=update_fields)
        return skill

    @staticmethod
    def delete(skill_id: uuid.UUID) -> None:
        """Delete a skill by ID."""
        Skill.objects.filter(id=skill_id).delete()


class SessionSkillRepository:
    """Data access for SessionSkill snapshot records."""

    @staticmethod
    def create_snapshots(session, skills: list[Skill]) -> list[SessionSkill]:
        """Bulk-create SessionSkill snapshots for all supplied skills."""
        snapshots = [
            SessionSkill(
                session=session,
                skill=skill,
                name=skill.name,
                body=skill.body,
            )
            for skill in skills
        ]
        return SessionSkill.objects.bulk_create(snapshots)
