"""Service accessors and owner-scoped lookups shared by MCP tool domains.

Tool modules call these through the module (``access.runner_service()``),
so the REST-shared service singletons and ownership checks stay a single
seam for every domain.
"""

from __future__ import annotations

import uuid

from apps.harness.harness_service import get_harness_service
from apps.organizations.services import OrganizationService
from apps.runners.services import get_runner_service
from common.exceptions import NotFoundError

from .results import error_result


def runner_service():
    """Return the process-wide RunnerService."""
    return get_runner_service()


def owned_workspace_or_error(api_key, org_id, workspace_id):
    """Return an owned workspace or an MCP-formatted error payload."""
    svc = runner_service()
    org_service = OrganizationService()
    org_service.require_membership(api_key.user, org_id)
    try:
        workspace = svc.get_workspace_for_user(
            workspace_id,
            user=api_key.user,
            organization_id=org_id,
        )
    except NotFoundError:
        return None, error_result("Workspace not found")
    return workspace, None


def harness_service():
    """Return the process-wide HarnessService (shared with REST)."""
    return get_harness_service()


def owned_harness_session_or_error(api_key, org_id, session_id):
    """Return a harness session scoped to an owned workspace."""
    try:
        session_uuid = uuid.UUID(str(session_id))
    except ValueError:
        return None, error_result("Invalid session_id UUID")

    service = harness_service()
    try:
        session = service.get_session(session_uuid)
    except NotFoundError:
        return None, error_result("Harness session not found")

    _workspace, error = owned_workspace_or_error(api_key, org_id, session.workspace_id)
    if error is not None:
        return None, error
    return session, None
