"""
Service layer for the organizations app.

All business logic for organization management lives here.
"""

from __future__ import annotations

import logging
import re

from common.exceptions import ConflictError, NotFoundError

from .models import MembershipRole, Organization
from .repositories import MembershipRepository, OrganizationRepository

logger = logging.getLogger(__name__)


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

        # Seed activations: activate all standard agent definitions and
        # all credential services for the new organization.
        self._seed_activations(org)

        logger.info(
            "Organization created: %s (slug=%s, admin=%s)",
            org.id,
            slug,
            user.email,
        )
        return org

    @staticmethod
    def _seed_activations(org) -> None:
        """Activate all standard agent definitions and credential services for a new org."""
        from apps.runners.models import AgentDefinition, OrgAgentDefinitionActivation
        from apps.credentials.models import CredentialService, OrgCredentialServiceActivation

        standard_agents = AgentDefinition.objects.filter(organization__isnull=True)
        agent_activations = [
            OrgAgentDefinitionActivation(organization=org, agent_definition=agent)
            for agent in standard_agents
        ]
        OrgAgentDefinitionActivation.objects.bulk_create(
            agent_activations, ignore_conflicts=True
        )

        all_services = CredentialService.objects.all()
        svc_activations = [
            OrgCredentialServiceActivation(organization=org, credential_service=svc)
            for svc in all_services
        ]
        OrgCredentialServiceActivation.objects.bulk_create(
            svc_activations, ignore_conflicts=True
        )

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
                "created_at": m.organization.created_at,
            }
            for m in memberships
        ]

    def get_organization(self, org_id, user) -> Organization:
        """
        Get an organization by ID. User must be a member.
        """
        import uuid

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
        workspace_auto_stop_timeout_minutes: int | None,
    ) -> Organization:
        """Update org-wide workspace inactivity settings."""
        org = self.require_admin(user, org_id)

        if workspace_auto_stop_timeout_minutes is not None:
            if workspace_auto_stop_timeout_minutes < 1:
                raise ValueError(
                    "workspace_auto_stop_timeout_minutes must be at least 1"
                )
            if workspace_auto_stop_timeout_minutes > 10080:
                raise ValueError(
                    "workspace_auto_stop_timeout_minutes must be at most 10080"
                )

        return self.organizations.update_workspace_auto_stop_timeout(
            org,
            workspace_auto_stop_timeout_minutes,
        )

    def get_user_role(self, user, org_id) -> str | None:
        """Get the user's role in the given organization, or None."""
        import uuid

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
        import uuid as uuid_mod

        org = self.organizations.get_by_id(uuid_mod.UUID(str(org_id)))
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
        from common.exceptions import AuthenticationError

        org = self.require_membership(user, org_id)

        if not self.memberships.is_admin(user, org):
            raise AuthenticationError(
                "You must be an admin of this organization"
            )

        return org

    @staticmethod
    def _generate_slug(name: str) -> str:
        """Generate a URL-safe slug from an organization name."""
        slug = name.lower().strip()
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug)
        return slug.strip("-")
