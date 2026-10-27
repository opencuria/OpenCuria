"""
Service layer for the organizations app.

All business logic for organization management lives here. Other apps check
organizations and memberships through :class:`OrganizationService`; the
repositories are private to this context.
"""

from __future__ import annotations

import logging
import re
import uuid

from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .events import publish_organization_created
from .models import Membership, MembershipRole, Organization
from .repositories import MembershipRepository, OrganizationRepository

logger = logging.getLogger(__name__)

UNSET = object()
MIN_IMAGE_VERSIONS_TO_KEEP = 1
MAX_IMAGE_VERSIONS_TO_KEEP = 20


class OrganizationService:
    """Business logic for organizations and memberships."""

    def __init__(self) -> None:
        self.organizations = OrganizationRepository
        self.memberships = MembershipRepository

    def create_organization(self, *, name: str, user) -> Organization:
        """
        Create a new organization and make the user its admin.

        Generates a slug from the name. Raises ConflictError if slug exists.
        """
        slug = self._generate_slug(name)

        if self.organizations.get_by_slug(slug):
            raise ConflictError(f"Organization with slug '{slug}' already exists")

        org = self.organizations.create(name=name, slug=slug)

        self.memberships.create(
            user=user,
            organization=org,
            role=MembershipRole.ADMIN,
        )

        publish_organization_created(org)

        logger.info(
            "Organization created: %s (slug=%s, admin=%s)",
            org.id,
            slug,
            user.email,
        )
        return org

    def list_user_organizations(self, user) -> list[dict]:
        """
        List all organizations a user belongs to, with their role.
        """
        memberships = self.memberships.list_for_user(user)
        return [
            {
                "id": m.organization.id,
                "name": m.organization.name,
                "slug": m.organization.slug,
                "role": m.role,
                "workspace_auto_stop_timeout_minutes": m.organization.workspace_auto_stop_timeout_minutes,
                "image_versions_to_keep": m.organization.image_versions_to_keep,
                "created_at": m.organization.created_at,
            }
            for m in memberships
        ]

    def get_organization(self, org_id, user) -> Organization:
        """
        Get an organization by ID. User must be a member.
        """
        org = self.organizations.get_by_id(uuid.UUID(str(org_id)))
        if org is None:
            raise NotFoundError("Organization", str(org_id))

        membership = self.memberships.get(user=user, organization=org)
        if membership is None:
            raise NotFoundError("Organization", str(org_id))

        return org

    def update_workspace_policy(
        self,
        *,
        org_id,
        user,
        workspace_auto_stop_timeout_minutes: int | None | object = UNSET,
        image_versions_to_keep: int | object = UNSET,
    ) -> Organization:
        """Update org-wide workspace settings; omitted fields stay unchanged."""
        org = self.require_admin(user, org_id)

        if workspace_auto_stop_timeout_minutes is not UNSET:
            if workspace_auto_stop_timeout_minutes is not None:
                if workspace_auto_stop_timeout_minutes < 1:
                    raise ValueError(
                        "workspace_auto_stop_timeout_minutes must be at least 1"
                    )
                if workspace_auto_stop_timeout_minutes > 10080:
                    raise ValueError(
                        "workspace_auto_stop_timeout_minutes must be at most 10080"
                    )
            org = self.organizations.update_workspace_auto_stop_timeout(
                org,
                workspace_auto_stop_timeout_minutes,
            )
        if image_versions_to_keep is not UNSET:
            if not (
                MIN_IMAGE_VERSIONS_TO_KEEP
                <= int(image_versions_to_keep)
                <= MAX_IMAGE_VERSIONS_TO_KEEP
            ):
                raise ValueError(
                    "image_versions_to_keep must be between "
                    f"{MIN_IMAGE_VERSIONS_TO_KEEP} and {MAX_IMAGE_VERSIONS_TO_KEEP}"
                )
            org = self.organizations.update_image_versions_to_keep(
                org, int(image_versions_to_keep)
            )
        return org

    def get_user_role(self, user, org_id) -> str | None:
        """Get the user's role in the given organization, or None."""
        org = self.organizations.get_by_id(uuid.UUID(str(org_id)))
        if org is None:
            return None

        membership = self.memberships.get(user=user, organization=org)
        if membership is None:
            return None

        return membership.role

    def require_membership(self, user, org_id) -> Organization:
        """
        Validate that the user is a member of the organization.

        Returns the Organization. Raises NotFoundError if not a member.
        """
        org = self.organizations.get_by_id(uuid.UUID(str(org_id)))
        if org is None:
            raise NotFoundError("Organization", str(org_id))

        membership = self.memberships.get(user=user, organization=org)
        if membership is None:
            raise NotFoundError("Organization", str(org_id))

        return org

    def require_admin(self, user, org_id) -> Organization:
        """
        Validate that the user is an admin of the organization.

        Returns the Organization. Raises appropriate errors.
        """
        org = self.require_membership(user, org_id)

        if not self.memberships.is_admin(user, org):
            raise AuthenticationError("You must be an admin of this organization")

        return org

    def require_organization(self, org_id: uuid.UUID) -> Organization:
        """Return the organization or raise NotFoundError when it is missing."""
        org = self.organizations.get_by_id(org_id)
        if org is None:
            raise NotFoundError("Organization", str(org_id))
        return org

    def get_membership(
        self, user_id: int, org_id: uuid.UUID, *, lock: bool = False
    ) -> Membership | None:
        """Return a membership with its user and organization loaded.

        ``lock`` row-locks the membership inside the caller's transaction so
        authorization rechecks and the writes they guard stay atomic.
        """
        return self.memberships.get_for_user_and_org(user_id, org_id, lock=lock)

    def get_active_membership(
        self, user_id: int, org_id: uuid.UUID, *, lock: bool = False
    ) -> Membership | None:
        """Return the membership only while its user account is active."""
        membership = self.get_membership(user_id, org_id, lock=lock)
        if membership is None or not membership.user.is_active:
            return None
        return membership

    def require_active_membership(
        self, user_id: int, org_id: uuid.UUID, *, lock: bool = False
    ) -> Membership:
        """Return the active membership or raise AuthenticationError."""
        membership = self.get_active_membership(user_id, org_id, lock=lock)
        if membership is None:
            raise AuthenticationError("Active organization membership required")
        return membership

    @staticmethod
    def _generate_slug(name: str) -> str:
        """Generate a URL-safe slug from an organization name."""
        slug = name.lower().strip()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        return slug.strip("-")
