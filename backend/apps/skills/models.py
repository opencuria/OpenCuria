"""
Database models for the skills app.

Skills are reusable Markdown prompt fragments that can be attached to
sessions. They are owned by either a User (personal, visible across all
their organizations) or an Organization (shared with all members).

SessionSkill snapshots the name and body at the time of use so that later
edits or deletions do not alter historical session context.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Skill(models.Model):
    """
    A reusable Markdown prompt fragment.

    Ownership is mutually exclusive: either user OR organization is set.
    - user-owned: visible across all the user's organizations.
    - org-owned:  visible to all org members; only admins may edit/delete.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    body = models.TextField(help_text="Markdown content appended to the prompt.")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="skills",
        help_text="Set for personal skills (mutually exclusive with organization).",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="skills",
        help_text="Set for org-shared skills (mutually exclusive with user).",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_skills",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "skills_skill"
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(
                check=(
                    models.Q(user__isnull=False, organization__isnull=True)
                    | models.Q(user__isnull=True, organization__isnull=False)
                ),
                name="skill_owner_exclusive",
            )
        ]

    def __str__(self) -> str:
        return self.name


class SessionSkill(models.Model):
    """
    Snapshot of a Skill at the time it was attached to a Session.

    Copies name and body so that later edits or deletions do not alter
    historical prompt context. The FK to Skill is nullable so deletion
    of the source skill does not cascade.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        "runners.Session",
        on_delete=models.CASCADE,
        related_name="session_skills",
    )
    skill = models.ForeignKey(
        Skill,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="session_skills",
        help_text="Nullable — preserved even if the source skill is deleted.",
    )
    name = models.CharField(max_length=255, help_text="Snapshot of skill name at time of use.")
    body = models.TextField(help_text="Snapshot of skill body at time of use.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "skills_session_skill"
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"SessionSkill({self.session_id}, {self.name})"
