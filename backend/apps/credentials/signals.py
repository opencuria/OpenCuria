"""Organization-event receivers for credential-service defaults."""

from __future__ import annotations

from django.dispatch import receiver

from apps.organizations.events import organization_created
from apps.organizations.models import Organization

from .services import OrgCredentialServiceActivationSvc


@receiver(organization_created)
def activate_global_services(sender, organization: Organization, **kwargs) -> None:
    """Start every new organization with all global services activated."""
    OrgCredentialServiceActivationSvc().activate_global_services(organization.id)
