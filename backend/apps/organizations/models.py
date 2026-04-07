"""
Organization and Membership models for multi-tenancy.

Organizations are the central tenant concept. Runners belong to
organizations, users are members of organizations with a role.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Organization(models.Model):
    """
    A tenant organization that owns runners and provides workspace access.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255, unique=True)
    workspace_auto_stop_timeout_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "Automatically stop running workspaces after this many minutes of "
            "inactivity. Null disables the policy."
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "organizations_organization"
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class MembershipRole(models.TextChoices):
    """Roles a user can have within an organization."""

    ADMIN = "admin", "Admin"
    MEMBER = "member", "Member"


class Membership(models.Model):
    """
    Links a user to an organization with a specific role.

    A user can belong to multiple organizations. Each membership
    defines the user's role within that organization.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(
        max_length=20,
        choices=MembershipRole.choices,
        default=MembershipRole.MEMBER,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "organizations_membership"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="unique_user_organization",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} → {self.organization} ({self.role})"
