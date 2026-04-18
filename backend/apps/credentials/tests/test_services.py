from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.credentials.models import CredentialService
from apps.credentials.services import CredentialServiceSvc, CredentialSvc
from apps.organizations.models import Organization
from common.exceptions import ConflictError


@pytest.mark.django_db
def test_create_service_accepts_file_target_path() -> None:
    service = CredentialServiceSvc().create_service(
        name="OpenAI Codex Auth",
        slug="openai-codex-auth-custom",
        description="Codex auth file",
        credential_type="file",
        env_var_name="",
        target_path="~/.codex/auth.json",
        label="auth.json",
    )

    assert service.credential_type == "file"
    assert service.target_path == "~/.codex/auth.json"
    assert service.env_var_name == ""


@pytest.mark.django_db
def test_create_service_rejects_invalid_file_target_path() -> None:
    with pytest.raises(ValueError, match="target_path must not contain empty, '.' or '..' segments"):
        CredentialServiceSvc().create_service(
            name="Broken File Credential",
            slug="broken-file-credential",
            description="",
            credential_type="file",
            env_var_name="",
            target_path="../secrets/auth.json",
            label="auth.json",
        )


@pytest.mark.django_db
def test_resolve_credentials_includes_file_entries() -> None:
    user_model = get_user_model()
    user = user_model.objects.create_user(email="file-user@test.local", password="secret")
    organization = Organization.objects.create(
        name="Files",
        slug=f"files-{uuid.uuid4().hex[:8]}",
    )
    service = CredentialService.objects.create(
        name="OpenAI Codex Auth",
        slug=f"codex-auth-{uuid.uuid4().hex[:8]}",
        credential_type="file",
        target_path="~/.codex/auth.json",
        label="auth.json",
    )

    credential = CredentialSvc().create_org_credential(
        organization_id=organization.id,
        service_id=service.id,
        name="Codex Auth",
        value='{"access_token":"xyz"}',
        user=user,
    )

    resolved = CredentialSvc().resolve_credentials(
        [credential.id],
        org_id=organization.id,
        user=user,
    )

    assert resolved.env_vars == {}
    assert resolved.ssh_keys == []
    assert len(resolved.files) == 1
    assert resolved.files[0].target_path == "~/.codex/auth.json"
    assert resolved.files[0].content == '{"access_token":"xyz"}'


@pytest.mark.django_db
def test_resolve_credentials_rejects_multiple_credentials_for_same_service() -> None:
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email="duplicate-user@test.local",
        password="secret",
    )
    organization = Organization.objects.create(
        name="Duplicates",
        slug=f"duplicates-{uuid.uuid4().hex[:8]}",
    )
    service = CredentialService.objects.create(
        name="GitHub",
        slug=f"github-{uuid.uuid4().hex[:8]}",
        credential_type="env",
        env_var_name="GITHUB_TOKEN",
        label="Personal Access Token",
    )

    credential_svc = CredentialSvc()
    personal_credential = credential_svc.create_personal_credential(
        service_id=service.id,
        name="Personal GitHub",
        value="ghp-personal",
        user=user,
    )
    org_credential = credential_svc.create_org_credential(
        organization_id=organization.id,
        service_id=service.id,
        name="Org GitHub",
        value="ghp-org",
        user=user,
    )

    with pytest.raises(
        ConflictError,
        match="Only one credential per credential service can be attached to a workspace",
    ):
        credential_svc.resolve_credentials(
            [personal_credential.id, org_credential.id],
            org_id=organization.id,
            user=user,
        )
