"""Workspace lifecycle resolves requested credential IDs in the service."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from apps.credentials.models import (
    Credential,
    CredentialService,
    OrgCredentialServiceActivation,
)
from apps.credentials.services import CredentialSvc, ResolvedCredentials
from apps.runners.models import ImageInstance
from apps.runners.services import RunnerService
from common.exceptions import ConflictError, NotFoundError


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_workspace_resolves_credentials_without_api_pre_resolution(
    runner, user
):
    socket_server = AsyncMock()
    service = RunnerService(sio_server=socket_server)
    artifact = ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
        created_by=user,
        runner_ref="captured-image",
        name="Captured image",
        status=ImageInstance.Status.READY,
    )
    credential_service = CredentialService.objects.create(
        name="Fallback API key",
        slug=f"fallback-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="FALLBACK_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization_id=runner.organization_id, credential_service=credential_service
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=credential_service.id,
        name="Fallback credential",
        value="resolved-secret",
        user=user,
        org_id=runner.organization_id,
    )

    workspace, _ = await service.workspace_lifecycle.create_workspace(
        name="Service-resolved credentials",
        repos=[],
        image_artifact_id=artifact.id,
        credential_ids=[credential.id],
        user=user,
        organization_id=runner.organization_id,
    )

    assert workspace.credentials.filter(pk=credential.pk).exists()
    workspace.refresh_from_db()
    assert workspace.credentials_present is True
    _, payload = socket_server.emit.await_args.args[:2]
    assert payload["env_vars"] == {"FALLBACK_TOKEN": "resolved-secret"}
    assert CredentialSvc().resolve_workspace_credentials(workspace).env_vars == {
        "FALLBACK_TOKEN": "resolved-secret"
    }

    # Only the runner's successful create acknowledgement updates disk state.
    service.workspace_lifecycle.handle_workspace_created(
        task_id=str(workspace.tasks.get().id),
        workspace_id=str(workspace.id),
        status="created",
        credentials_present=True,
        runner_id=str(runner.id),
    )
    workspace.refresh_from_db()
    assert workspace.credentials_present is True


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_update_workspace_resolves_requested_credential_ids(workspace, user):
    service = RunnerService(sio_server=AsyncMock())
    credential_service = CredentialService.objects.create(
        name="Update API key",
        slug=f"update-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="UPDATE_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization_id=workspace.runner.organization_id,
        credential_service=credential_service,
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=credential_service.id,
        name="Update credential",
        value="new-workspace-secret",
        user=user,
        org_id=workspace.runner.organization_id,
    )
    workspace.credentials.set([])
    service.runner_transport.call_runner = AsyncMock(return_value={"ok": True})

    await service.workspace_lifecycle.update_workspace(
        workspace.id,
        credential_ids=[credential.id],
        user=user,
        organization_id=workspace.runner.organization_id,
    )

    workspace.refresh_from_db()
    assert list(workspace.credentials.values_list("id", flat=True)) == [credential.id]
    # A successful fake call without the runner ACK does not change disk state.
    assert workspace.credentials_present is False
    assert service.runner_transport.call_runner.called
    call_args = service.runner_transport.call_runner.await_args
    payload = next(arg for arg in call_args.args if isinstance(arg, dict))
    assert payload["env_vars"] == {"UPDATE_TOKEN": "new-workspace-secret"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_workspace_credentials_require_user_and_organization(runner, user):
    socket_server = AsyncMock()
    service = RunnerService(sio_server=socket_server)
    artifact = ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
        created_by=user,
        runner_ref="captured-image",
        name="Captured image",
        status=ImageInstance.Status.READY,
    )
    credential_service = CredentialService.objects.create(
        name="Fallback API key",
        slug=f"fallback-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="FALLBACK_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization_id=runner.organization_id, credential_service=credential_service
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=credential_service.id,
        name="Fallback credential",
        value="resolved-secret",
        user=user,
        org_id=runner.organization_id,
    )

    with pytest.raises(ConflictError, match="User and organization"):
        await service.workspace_lifecycle.create_workspace(
            name="Missing context",
            repos=[],
            image_artifact_id=artifact.id,
            credential_ids=[credential.id],
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_workspace_validates_credential_ownership(runner, user):
    socket_server = AsyncMock()
    service = RunnerService(sio_server=socket_server)
    artifact = ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        origin_type=ImageInstance.OriginType.WORKSPACE_CAPTURE,
        created_by=user,
        runner_ref="captured-image",
        name="Captured image",
        status=ImageInstance.Status.READY,
    )
    other = type(user).objects.create_user(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    credential_service = CredentialService.objects.create(
        name="Fallback API key",
        slug=f"fallback-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="FALLBACK_TOKEN",
    )
    OrgCredentialServiceActivation.objects.create(
        organization_id=runner.organization_id, credential_service=credential_service
    )
    credential = CredentialSvc().create_personal_credential(
        service_id=credential_service.id,
        name="Other user's credential",
        value="not-visible",
        user=other,
        org_id=runner.organization_id,
    )

    with pytest.raises(NotFoundError, match="Credential"):
        await service.workspace_lifecycle.create_workspace(
            name="Foreign credential",
            repos=[],
            image_artifact_id=artifact.id,
            credential_ids=[credential.id],
            user=user,
            organization_id=runner.organization_id,
        )

    assert not service.workspaces.list_all().filter(name="Foreign credential").exists()


@pytest.mark.django_db
def test_material_credential_ids_exclude_server_side_types(workspace):
    """Only env, file and SSH credentials count as runner-delivered material."""
    organization = workspace.runner.organization
    attached = {}
    for credential_type in ("env", "file", "ssh_key", "mcp_oauth", "provider_secret"):
        credential_service = CredentialService.objects.create(
            name=f"{credential_type} service",
            slug=f"{credential_type}-{uuid.uuid4().hex[:8]}",
            credential_type=credential_type,
            organization=organization,
        )
        attached[credential_type] = Credential.objects.create(
            organization=organization,
            service=credential_service,
            name=f"{credential_type} credential",
            encrypted_value="ciphertext",
        )
    workspace.credentials.set(attached.values())
    material = {
        attached[credential_type].id for credential_type in ("env", "file", "ssh_key")
    }

    assert CredentialSvc().workspace_material_credential_ids(workspace.id) == material
    # A resolved selection classifies its credentials by the same rule.
    selection = ResolvedCredentials(credentials=list(attached.values()))
    assert selection.material_ids == material


@pytest.mark.parametrize(
    ("resolved", "expected"),
    [
        (ResolvedCredentials(), False),
        (ResolvedCredentials(oauth_credentials=[uuid.uuid4()]), False),
        (ResolvedCredentials(env_vars={"TOKEN": "secret"}), True),
        (ResolvedCredentials(ssh_keys=["private-key"]), True),
    ],
)
def test_resolved_material_reflects_what_the_runner_writes(resolved, expected):
    """Server-side OAuth grants alone leave nothing to write into a workspace."""
    assert resolved.has_material is expected
