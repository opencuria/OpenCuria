"""
Tests for org agent definition management API endpoints.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.credentials.models import CredentialService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.models import (
    AgentCommand,
    AgentCredentialRelationCommand,
    AgentDefinition,
    AgentDefinitionCredentialRelation,
    OrgAgentDefinitionActivation,
)
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


def _auth_headers(token: str, org_id: str) -> dict[str, str]:
    return {
        "HTTP_X_API_KEY": token,
        "HTTP_X_ORGANIZATION_ID": org_id,
    }


def _create_api_key(*, user, permissions: list[str]) -> str:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return token


@pytest.mark.django_db
def test_org_admin_can_duplicate_standard_agent_definition(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="org-admin@test.com", password="secret")
    org = Organization.objects.create(name="Acme", slug="acme-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE.value],
    )
    service = CredentialService.objects.create(
        name="GitHub Token",
        slug="github-token-dup",
        credential_type="env",
        env_var_name="GITHUB_TOKEN",
    )
    source = AgentDefinition.objects.create(
        name="copilot-standard",
        description="Standard base definition",
        organization=None,
        supports_multi_chat=True,
        default_env={"BASE": "1"},
        available_options=[{"key": "mode", "label": "Mode", "choices": ["a"], "default": "a"}],
    )
    source.required_credential_services.add(service)
    AgentCommand.objects.create(
        agent=source,
        phase="run",
        args=["copilot", "run", "{prompt}"],
        workdir="/workspace",
        env={"A": "B"},
        description="run",
        order=0,
    )

    response = client.post(
        f"/api/v1/org-agent-definitions/{source.id}/duplicate/",
        data=json.dumps({}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["is_standard"] is False
    assert payload["organization_id"] == str(org.id)
    assert payload["name"] == source.name
    assert payload["required_credential_service_ids"] == [str(service.id)]
    assert payload["is_active"] is True

    copied = AgentDefinition.objects.get(id=payload["id"])
    assert copied.organization_id == org.id
    assert copied.commands.count() == 1
    assert copied.required_credential_services.filter(id=service.id).exists()
    assert OrgAgentDefinitionActivation.objects.filter(
        organization=org,
        agent_definition=copied,
    ).exists()


@pytest.mark.django_db
def test_duplicate_copies_credential_relations(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="org-admin-rel@test.com", password="secret")
    org = Organization.objects.create(name="Beta", slug="beta-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE.value],
    )
    service = CredentialService.objects.create(
        name="OpenAI Key",
        slug="openai-key-dup",
        credential_type="env",
        env_var_name="OPENAI_API_KEY",
    )
    source = AgentDefinition.objects.create(name="claude-standard", organization=None)
    AgentCommand.objects.create(
        agent=source,
        phase="run",
        args=["claude", "{prompt}"],
        order=0,
    )
    relation = AgentDefinitionCredentialRelation.objects.create(
        agent_definition=source,
        credential_service=service,
        default_env={"X": "1"},
    )
    AgentCredentialRelationCommand.objects.create(
        relation=relation,
        phase="configure",
        args=["echo", "auth"],
        order=0,
    )

    response = client.post(
        f"/api/v1/org-agent-definitions/{source.id}/duplicate/",
        data=json.dumps({"activate": False}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 201
    payload = response.json()
    copied = AgentDefinition.objects.get(id=payload["id"])
    copied_relation = AgentDefinitionCredentialRelation.objects.get(
        agent_definition=copied,
        credential_service=service,
    )
    assert copied_relation.default_env == {"X": "1"}
    copied_rel_cmd = copied_relation.commands.get(phase="configure")
    assert copied_rel_cmd.args == ["echo", "auth"]
    assert payload["is_active"] is False
    assert not OrgAgentDefinitionActivation.objects.filter(
        organization=org,
        agent_definition=copied,
    ).exists()


@pytest.mark.django_db
def test_non_staff_admin_cannot_toggle_standard_activation(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="org-admin-toggle@test.com", password="secret")
    org = Organization.objects.create(name="Gamma", slug="gamma-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE.value],
    )
    source = AgentDefinition.objects.create(name="codex-standard", organization=None)
    AgentCommand.objects.create(agent=source, phase="run", args=["codex", "{prompt}"], order=0)

    response = client.post(
        f"/api/v1/org-agent-definitions/{source.id}/activation/",
        data=json.dumps({"active": True}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"]
        == "Only staff users can modify standard agent definition activation"
    )


@pytest.mark.django_db
def test_staff_admin_can_toggle_standard_activation(client: Client):
    user_model = get_user_model()
    admin = user_model.objects.create_user(email="staff-admin-toggle@test.com", password="secret")
    admin.is_staff = True
    admin.save(update_fields=["is_staff"])
    org = Organization.objects.create(name="Delta", slug="delta-org")
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    token = _create_api_key(
        user=admin,
        permissions=[APIKeyPermission.ORG_AGENT_DEFINITIONS_WRITE.value],
    )
    source = AgentDefinition.objects.create(name="gemini-standard", organization=None)
    AgentCommand.objects.create(agent=source, phase="run", args=["gemini", "{prompt}"], order=0)

    response = client.post(
        f"/api/v1/org-agent-definitions/{source.id}/activation/",
        data=json.dumps({"active": True}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is True
