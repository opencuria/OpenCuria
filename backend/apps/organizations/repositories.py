"""
Repository layer for the organizations app.

Encapsulates all database queries for Organization and Membership models.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db.models import QuerySet

from .models import Membership, MembershipRole, Organization


# ---------------------------------------------------------------------------
# Organization Repository
# ---------------------------------------------------------------------------


class OrganizationRepository:
    """Data access for Organization records."""

    @staticmethod
    def get_by_id(org_id: uuid.UUID) -> Organization | None:
        """Fetch an organization by its ID."""
        return Organization.objects.filter(id=org_id).first()

    @staticmethod
    def get_by_slug(slug: str) -> Organization | None:
        """Fetch an organization by its slug."""
        return Organization.objects.filter(slug=slug).first()

    @staticmethod
    def create(*, name: str, slug: str) -> Organization:
        """Create a new organization."""
        return Organization.objects.create(name=name, slug=slug)

    @staticmethod
    def update_workspace_auto_stop_timeout(
        organization: Organization,
        timeout_minutes: int | None,
    ) -> Organization:
        """Persist the org-wide workspace inactivity timeout policy."""
        organization.workspace_auto_stop_timeout_minutes = timeout_minutes
        organization.save(
            update_fields=["workspace_auto_stop_timeout_minutes", "updated_at"]
        )
        return organization

    @staticmethod
    def list_for_user(user) -> QuerySet[Organization]:
        """Return all organizations a user is a member of."""
        return Organization.objects.filter(
            memberships__user=user,
        ).distinct()


# ---------------------------------------------------------------------------
# Membership Repository
# ---------------------------------------------------------------------------


class MembershipRepository:
    """Data access for Membership records."""

    @staticmethod
    def get(user, organization: Organization) -> Membership | None:
        """Fetch a specific membership."""
        return Membership.objects.filter(
            user=user,
            organization=organization,
        ).first()

    @staticmethod
    def create(
        *,
        user,
        organization: Organization,
        role: MembershipRole = MembershipRole.MEMBER,
    ) -> Membership:
        """Create a new membership."""
        return Membership.objects.create(
            user=user,
            organization=organization,
            role=role,
        )

    @staticmethod
    def list_for_organization(organization: Organization) -> QuerySet[Membership]:
        """List all memberships for an organization."""
        return Membership.objects.filter(
            organization=organization,
        ).select_related("user")

    @staticmethod
    def list_for_user(user) -> QuerySet[Membership]:
        """List all memberships for a user."""
        return Membership.objects.filter(
            user=user,
        ).select_related("organization")

    @staticmethod
    def is_admin(user, organization: Organization) -> bool:
        """Check if a user is an admin of an organization."""
        return Membership.objects.filter(
            user=user,
            organization=organization,
            role=MembershipRole.ADMIN,
        ).exists()
