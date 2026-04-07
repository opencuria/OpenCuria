"""Service layer for the skills app.

All business logic lives here. The API delegates to these methods.
"""

from __future__ import annotations

import logging
import uuid

from common.exceptions import AuthenticationError, NotFoundError

from .repositories import SessionSkillRepository, SkillRepository

logger = logging.getLogger(__name__)


class SkillService:
    """Business logic for skill CRUD and session snapshotting."""

    def __init__(self) -> None:
        self.skills = SkillRepository
        self.session_skills = SessionSkillRepository

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def list_skills(self, user, org_id: uuid.UUID) -> list:
        """Return all skills visible to this user in the given org."""
        return list(self.skills.list_for_user_in_org(user.id, org_id))

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_personal_skill(self, *, name: str, body: str, user) -> "Skill":
        """Create a personal skill owned by the user."""
        name = name.strip()
        body = body.strip()
        if not name:
            raise ValueError("Skill name must not be empty")
        if not body:
            raise ValueError("Skill body must not be empty")
        skill = self.skills.create_personal(name=name, body=body, user=user)
        logger.info("Personal skill created: %s (user=%s)", skill.id, user.id)
        return skill

    def create_org_skill(
        self, *, name: str, body: str, org_id: uuid.UUID, user
    ) -> "Skill":
        """Create an org-shared skill. Caller must verify admin role."""
        name = name.strip()
        body = body.strip()
        if not name:
            raise ValueError("Skill name must not be empty")
        if not body:
            raise ValueError("Skill body must not be empty")
        skill = self.skills.create_org(
            name=name, body=body, organization_id=org_id, created_by=user
        )
        logger.info("Org skill created: %s (org=%s)", skill.id, org_id)
        return skill

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_skill(
        self,
        skill_id: uuid.UUID,
        *,
        name: str | None = None,
        body: str | None = None,
        user,
        org_id: uuid.UUID,
        is_admin: bool,
    ) -> "Skill":
        """Update a skill. Enforces ownership rules:
        - Personal skills: only the owner may edit.
        - Org skills: only org admins may edit.
        """
        skill = self.skills.get_by_id(skill_id)
        if skill is None:
            raise NotFoundError("Skill", str(skill_id))

        self._assert_can_edit(skill, user=user, org_id=org_id, is_admin=is_admin)

        if name is not None:
            name = name.strip()
            if not name:
                raise ValueError("Skill name must not be empty")
        if body is not None:
            body = body.strip()
            if not body:
                raise ValueError("Skill body must not be empty")

        return self.skills.update(skill, name=name, body=body)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete_skill(
        self,
        skill_id: uuid.UUID,
        *,
        user,
        org_id: uuid.UUID,
        is_admin: bool,
    ) -> None:
        """Delete a skill. Enforces ownership rules."""
        skill = self.skills.get_by_id(skill_id)
        if skill is None:
            raise NotFoundError("Skill", str(skill_id))

        self._assert_can_edit(skill, user=user, org_id=org_id, is_admin=is_admin)
        self.skills.delete(skill_id)
        logger.info("Skill deleted: %s", skill_id)

    # ------------------------------------------------------------------
    # Session snapshot
    # ------------------------------------------------------------------

    def snapshot_skills_for_session(
        self, session, skill_ids: list[uuid.UUID]
    ) -> list:
        """Fetch skills by IDs and create snapshot records for a session.

        Silently skips IDs not found — we do not error on deleted skills
        during prompt dispatch.
        """
        if not skill_ids:
            return []
        skills = self.skills.get_many_by_ids(skill_ids)
        if not skills:
            return []
        return self.session_skills.create_snapshots(session, skills)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _assert_can_edit(self, skill, *, user, org_id: uuid.UUID, is_admin: bool) -> None:
        """Raise an error if the user cannot edit this skill."""
        if skill.user_id is not None:
            # Personal skill — only owner may edit
            if skill.user_id != user.id:
                raise AuthenticationError("You do not own this skill")
        else:
            # Org skill — must be in the right org and be an admin
            if skill.organization_id != org_id:
                raise NotFoundError("Skill", str(skill.id))
            if not is_admin:
                raise AuthenticationError(
                    "Only org admins can edit organization skills"
                )
