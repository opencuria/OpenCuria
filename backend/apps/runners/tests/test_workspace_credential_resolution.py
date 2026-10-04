"""Regression tests for credential-safe workspace creation fallbacks."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from apps.credentials.models import CredentialService, OrgCredentialServiceActivation
from apps.credentials.services import CredentialSvc
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
        runtime_type="docker",
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

    workspace, _ = await service.create_workspace(
        name="Fallback credentials",
        repos=[],
        image_artifact_id=artifact.id,
        credentials=[credential],
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
    service.handle_workspace_created(
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
async def test_update_workspace_fallback_resolves_requested_credential_ids(
    workspace, user
):
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
    service._call_runner = AsyncMock(return_value={"ok": True})

    await service.update_workspace(
        workspace.id,
        credentials=[credential],
        user=user,
        organization_id=workspace.runner.organization_id,
    )

    workspace.refresh_from_db()
    assert list(workspace.credentials.values_list("id", flat=True)) == [credential.id]
    # A successful fake call without the runner ACK does not change disk state.
    assert workspace.credentials_present is False
    assert service._call_runner.called
    call_args = service._call_runner.await_args
    payload = next(arg for arg in call_args.args if isinstance(arg, dict))
    assert payload["env_vars"] == {"UPDATE_TOKEN": "new-workspace-secret"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_workspace_fallback_requires_user_and_organization(runner, user):
    socket_server = AsyncMock()
    service = RunnerService(sio_server=socket_server)
    artifact = ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        runtime_type="docker",
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
        await service.create_workspace(
            name="Missing context",
            repos=[],
            image_artifact_id=artifact.id,
            credentials=[credential],
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_create_workspace_fallback_validates_credential_ownership(runner, user):
    socket_server = AsyncMock()
    service = RunnerService(sio_server=socket_server)
    artifact = ImageInstance.objects.create(
        is_legacy=True,
        runner=runner,
        runtime_type="docker",
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
        await service.create_workspace(
            name="Foreign credential",
            repos=[],
            image_artifact_id=artifact.id,
            credentials=[credential],
            user=user,
            organization_id=runner.organization_id,
        )

    assert not service.workspaces.list_all().filter(name="Foreign credential").exists()
