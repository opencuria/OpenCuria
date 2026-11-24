"""When a credential may serve a plugin requirement in a workspace.

One rule for both sources: the credential the plugin names itself and the
one attached to the workspace. It must be of the requirement's service, and
belong to the workspace owner (personal) or the workspace's organization.
"""

from __future__ import annotations

import uuid
from typing import Any


def usable_credential(
    credential: Any,
    *,
    service_id: uuid.UUID,
    owner_id: int | None,
    org_id: uuid.UUID,
) -> bool:
    """Whether *credential* may serve a requirement of *service_id*."""
    if credential is None or credential.service_id != service_id:
        return False
    service = credential.service
    if service.credential_type == "provider_secret":
        return False
    if service.organization_id not in {None, org_id}:
        return False
    if service.owner_id not in {None, owner_id}:
        return False
    if credential.user_id is not None:
        return credential.user_id == owner_id and credential.organization_id is None
    return credential.organization_id == org_id


def requirement_credential(
    requirement: Any,
    attached_by_service: dict[uuid.UUID, Any],
    *,
    owner_id: int | None,
    org_id: uuid.UUID,
) -> Any:
    """The credential a requirement uses: the plugin's own, else the attached."""
    service_id = requirement.credential_service_id
    linked = getattr(requirement, "credential", None)
    if usable_credential(
        linked, service_id=service_id, owner_id=owner_id, org_id=org_id
    ):
        return linked
    attached = attached_by_service.get(service_id)
    if usable_credential(
        attached, service_id=service_id, owner_id=owner_id, org_id=org_id
    ):
        return attached
    return None
