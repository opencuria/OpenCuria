"""Push provider keys use the vault without ever becoming workspace material."""

import pytest

from apps.credentials.services import CredentialSvc
from apps.plugins.runtime import _workspace_credentials_by_service
from apps.plugins.services import PluginService
from common.exceptions import ConflictError, NotFoundError

pytestmark = pytest.mark.django_db


def test_push_keys_are_not_injectable_into_workspaces(setup):
    org, user, workspace, _, _, credentials = setup
    credential = credentials["apns"]
    assert credential.service.credential_type == "server_secret"
    with pytest.raises(NotFoundError):
        CredentialSvc().resolve_credentials([credential.id], org_id=org.id, user=user)
    workspace.credentials.add(credential)
    with pytest.raises(ConflictError, match="Server-only"):
        CredentialSvc().resolve_workspace_credentials(workspace)
    with pytest.raises(ConflictError, match="Server-only"):
        _workspace_credentials_by_service(workspace, org_id=org.id)


def test_plugin_requirements_cannot_reference_push_signing_services(setup):
    org, _, _, _, _, credentials = setup
    with pytest.raises(ValueError, match="Server-only"):
        PluginService()._prepare_requirement_inputs(
            [{"key": "APNS", "service_id": str(credentials["apns"].service_id)}],
            org_id=org.id,
        )
