"""
Database models for the skills app.

Skills are reusable Markdown prompt fragments owned by either a User
(personal, visible across all their organizations) or an Organization
(shared with all members).
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
