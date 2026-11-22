"""Claude runtime authentication from scoped provider accounts."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.harness import provider_accounts as provider_accounts_module
from apps.harness.engines.codex.auth import resolve_codex_auth
from apps.harness.engines.connections import EngineConnectionService
from apps.harness.engines.service import EngineCatalogService
from apps.harness.provider_accounts import (
    ProviderAccountService,
    WorkspaceAccountRequiredError,
)
from apps.harness.providers.chatgpt_credentials import get_fresh_chatgpt_credentials
from apps.organizations.repositories import MembershipRepository
from common.exceptions import NotFoundError


@pytest.fixture
def connection_owner(organization):
    """Create an active organization member for authentication tests."""
    user = get_user_model().objects.create_user(
        email=f"claude-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    MembershipRepository.create(user=user, organization=organization)
    return user


@pytest.mark.django_db
@pytest.mark.parametrize(
    "auth_type,env_name",
    [
        ("api_token", "ANTHROPIC_API_KEY"),
        ("subscription_token", "CLAUDE_CODE_OAUTH_TOKEN"),
    ],
)
def test_resolve_selected_and_explicit_account_auth(
    organization, connection_owner, auth_type, env_name
) -> None:
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"auth_type": auth_type, "token": "claude-secret", "label": "Work"},
    )
    service = EngineConnectionService()
    selected = service.resolve(organization.id, connection_owner.id)
    assert selected == service.resolve(organization.id, connection_owner.id, account.id)
    assert selected.auth_type == auth_type
    assert selected.token == "claude-secret"
    assert selected.label == "Work"
    assert selected.secret_env == {env_name: "claude-secret"}
    assert selected.connection_id == account.id
    assert "claude-secret" not in repr(selected)
    assert service.is_connected(organization.id, connection_owner.id)

    accounts.select_account(organization.id, connection_owner.id, "claude-agent", None)
    assert not service.is_connected(organization.id, connection_owner.id)
    with pytest.raises(NotFoundError):
        service.resolve(organization.id, connection_owner.id)
    # An admitted turn keeps its explicit account even after selection changes.
    assert service.resolve(organization.id, connection_owner.id, account.id) == selected


@pytest.mark.django_db
def test_resolution_rejects_foreign_deleted_and_non_claude_accounts(
    organization, connection_owner
) -> None:
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="claude-agent",
        payload={"token": "owner-token"},
    )
    other = get_user_model().objects.create_user(
        email=f"other-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    MembershipRepository.create(user=other, organization=organization)
    service = EngineConnectionService()
    assert not service.is_connected(organization.id, other.id)
    for org_id, user_id, account_id in (
        (organization.id, other.id, account.id),
        (uuid.uuid4(), connection_owner.id, account.id),
        (organization.id, other.id, None),
    ):
        with pytest.raises(NotFoundError):
            service.resolve(org_id, user_id, account_id)

    native = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider="openrouter",
        payload={"api_key": "native-secret"},
    )
    with pytest.raises(NotFoundError):
        service.resolve(organization.id, connection_owner.id, native.id)
    accounts.delete_account(organization.id, connection_owner.id, account.id)
    assert not service.is_connected(organization.id, connection_owner.id)
    with pytest.raises(NotFoundError):
        service.resolve(organization.id, connection_owner.id, account.id)


@pytest.fixture
def legacy_shared_claude_account(organization, provider_account_user, monkeypatch):
    """A shared Claude Agent account saved before sharing them was rejected."""
    with monkeypatch.context() as patch:
        patch.setattr(provider_accounts_module, "WORKSPACE_ONLY_PROVIDERS", frozenset())
        return ProviderAccountService().save_account(
            organization_id=organization.id,
            user=provider_account_user,
            provider="claude-agent",
            payload={"token": "shared-claude-token", "label": "Org", "shared": True},
        )


@pytest.mark.django_db
def test_claude_agent_accounts_cannot_be_shared(provider_account_factory) -> None:
    with pytest.raises(ValueError, match="Claude Agent accounts are personal"):
        provider_account_factory("claude-agent", token="shared-claude-token")


@pytest.mark.django_db
def test_shared_claude_account_never_reaches_a_workspace(
    organization, connection_owner, provider_account_user, legacy_shared_claude_account
) -> None:
    """Members and the sharing admin alike: org secrets stay on the server."""
    account = legacy_shared_claude_account
    service = EngineConnectionService()
    catalog = EngineCatalogService()
    for user in (connection_owner, provider_account_user):
        ProviderAccountService().select_account(
            organization.id, user.id, "claude-agent", account.id
        )
        assert not service.is_connected(organization.id, user.id)
        engines = {
            row["id"]: row for row in catalog.list_engines(organization.id, user.id)
        }
        assert engines["claude"]["connected"] is False
        for connection_id in (None, account.id):
            with pytest.raises(WorkspaceAccountRequiredError) as denied:
                service.resolve(organization.id, user.id, connection_id)
            assert "shared-claude-token" not in str(denied.value)


@pytest.mark.django_db
def test_unsharing_a_legacy_claude_account_makes_it_the_admins_own(
    organization, provider_account_user, legacy_shared_claude_account
) -> None:
    accounts = ProviderAccountService()
    accounts.save_account(
        organization_id=organization.id,
        user=provider_account_user,
        provider="claude-agent",
        account_id=legacy_shared_claude_account.id,
        payload={"shared": False},
    )
    resolved = EngineConnectionService().resolve(
        organization.id, provider_account_user.id, legacy_shared_claude_account.id
    )
    assert resolved.token == "shared-claude-token"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("provider", "model", "secret"),
    [
        (
            "chatgpt",
            "chatgpt/gpt-5.5",
            {"access": "a", "refresh": "r", "expires": 1, "account_id": "acct"},
        ),
        ("openrouter", "openrouter/openai/gpt-5", {"api_key": "sk-or-shared"}),
    ],
)
def test_codex_rejects_shared_accounts_but_uses_personal_ones(
    organization, connection_owner, provider_account_factory, provider, model, secret
) -> None:
    """Shared ChatGPT/OpenRouter accounts serve the native engine only."""
    accounts = ProviderAccountService()
    shared = provider_account_factory(provider, **secret)
    accounts.select_account(organization.id, connection_owner.id, provider, shared.id)
    catalog = EngineCatalogService()
    engines = {
        row["id"]: row
        for row in catalog.list_engines(organization.id, connection_owner.id)
    }
    assert engines["codex"]["connected"] is False
    assert catalog.list_models("codex", organization.id, connection_owner.id) == []
    for snapshot in (None, {provider: str(shared.id)}):
        with pytest.raises(WorkspaceAccountRequiredError):
            resolve_codex_auth(organization.id, connection_owner.id, snapshot, model)

    personal = accounts.save_account(
        organization_id=organization.id,
        user=connection_owner,
        provider=provider,
        payload={**secret, "label": "Mine"},
    )
    accounts.select_account(organization.id, connection_owner.id, provider, personal.id)
    auth = resolve_codex_auth(organization.id, connection_owner.id, None, model)
    assert auth.connection_id == personal.id
    engines = {
        row["id"]: row
        for row in catalog.list_engines(organization.id, connection_owner.id)
    }
    assert engines["codex"]["connected"] is True


@pytest.mark.django_db(transaction=True)
async def test_chatgpt_tokens_for_a_workspace_require_a_personal_account(
    organization, connection_owner, provider_account_factory
) -> None:
    """The Codex refresh path rechecks scope; the native path keeps working."""
    import time

    from asgiref.sync import sync_to_async

    fresh = int((time.time() + 3600) * 1000)
    shared = await sync_to_async(provider_account_factory)(
        "chatgpt", access="shared-access", refresh="r", expires=fresh
    )
    native = await get_fresh_chatgpt_credentials(
        organization.id, connection_owner.id, shared.id
    )
    assert native["access"] == "shared-access"
    with pytest.raises(WorkspaceAccountRequiredError):
        await get_fresh_chatgpt_credentials(
            organization.id, connection_owner.id, shared.id, for_workspace=True
        )
