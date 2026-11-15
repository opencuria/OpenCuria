"""Credential deletion guard for credentials that workspace plugins require."""

from __future__ import annotations

import uuid

from django.dispatch import receiver

from apps.credentials.deletion_guards import credential_deletion_requested
from apps.credentials.models import Credential

from .services.workspace_configuration import WorkspaceConfigurationService


@receiver(credential_deletion_requested)
def block_deleting_required_plugin_credentials(
    sender, credential: Credential, org_id: uuid.UUID, **kwargs
) -> list[dict]:
    """Report effective workspace plugins that still require *credential*."""
    return WorkspaceConfigurationService().credential_deletion_blockers(credential)
