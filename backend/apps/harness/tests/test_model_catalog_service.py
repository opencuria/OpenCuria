"""Account authorization and cache behavior shared by catalog consumers."""

from __future__ import annotations

import httpx
import pytest

from apps.harness.model_catalog_service import (
    CACHE_TTL_SECONDS,
    ModelsCatalogCache,
    clear_models_cache,
    list_merged_provider_models,
)
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.providers.base import ProviderAuthError
from apps.organizations.models import Membership
from common.exceptions import AuthenticationError


@pytest.fixture(autouse=True)
def empty_catalog_cache():
    """Keep account catalogs isolated even when a characterization fails."""
    clear_models_cache()
    yield
    clear_models_cache()


@pytest.mark.django_db
def test_catalog_tracks_selection_frozen_accounts_and_config_revisions(
    organization, provider_account_factory, provider_account_user
) -> None:
    """Selections and revisions cannot reuse another account's cached models."""
    first = provider_account_factory(
        "openai-compatible",
        label="First",
        base_url="https://first.example/v1",
        models=["first"],
    )
    second = provider_account_factory(
        "openai-compatible",
        label="Second",
        base_url="https://second.example/v1",
        models=["second"],
    )
    accounts = ProviderAccountService()
    kwargs = {
        "organization_id": organization.id,
        "user_id": provider_account_user.id,
    }
    accounts.select_account(
        organization.id, provider_account_user.id, "openai-compatible", first.id
    )
    assert [m.id for m in list_merged_provider_models(**kwargs)] == [
        "openai-compatible/first"
    ]

    accounts.select_account(
        organization.id, provider_account_user.id, "openai-compatible", second.id
    )
    assert [m.id for m in list_merged_provider_models(**kwargs)] == [
        "openai-compatible/second"
    ]
    frozen = {"openai-compatible": str(first.id)}
    assert [
        m.id for m in list_merged_provider_models(**kwargs, account_ids=frozen)
    ] == ["openai-compatible/first"]
    assert list_merged_provider_models(**kwargs, account_ids={}) == []

    accounts.save_account(
        organization_id=organization.id,
        user=provider_account_user,
        provider="openai-compatible",
        account_id=first.id,
        payload={"models": ["revised"]},
    )
    assert [
        m.id for m in list_merged_provider_models(**kwargs, account_ids=frozen)
    ] == ["openai-compatible/revised"]


@pytest.mark.django_db
@pytest.mark.parametrize("frozen", [False, True])
def test_cached_catalog_reauthorizes_membership(
    organization, provider_account_factory, provider_account_user, frozen
) -> None:
    """A populated cache never bypasses current organization authorization."""
    account = provider_account_factory("chatgpt", access="a", refresh="r", expires=1)
    kwargs = {
        "organization_id": organization.id,
        "user_id": provider_account_user.id,
        "account_ids": {"chatgpt": str(account.id)} if frozen else None,
    }
    assert list_merged_provider_models(**kwargs)
    Membership.objects.filter(
        organization=organization, user=provider_account_user
    ).delete()

    with pytest.raises(AuthenticationError, match="Active organization membership"):
        list_merged_provider_models(**kwargs)


@pytest.mark.django_db
def test_frozen_catalog_rejects_provider_mismatch(
    organization, provider_account_factory, provider_account_user
) -> None:
    """An authorized account cannot be presented as a different provider."""
    account = provider_account_factory("chatgpt", access="a", refresh="r", expires=1)
    with pytest.raises(ValueError, match="does not match the requested provider"):
        list_merged_provider_models(
            organization_id=organization.id,
            user_id=provider_account_user.id,
            account_ids={"openrouter": str(account.id)},
        )


@pytest.mark.django_db
def test_catalog_failure_retry_cache_hit_and_forced_refresh(
    organization, provider_account_factory, provider_account_user
) -> None:
    """Failed fetches are retried; only explicit refresh bypasses a valid cache."""
    provider_account_factory(
        "openrouter", api_key="account-key", base_url="https://router.example/v1"
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url == "https://router.example/v1/models"
        assert request.headers["Authorization"] == "Bearer account-key"
        if len(calls) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "vendor/model"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        kwargs = {
            "organization_id": organization.id,
            "user_id": provider_account_user.id,
            "client": client,
        }
        with pytest.raises(ProviderAuthError):
            list_merged_provider_models(**kwargs)
        models = list_merged_provider_models(**kwargs)
        assert [m.id for m in models] == ["openrouter/vendor/model"]
        assert list_merged_provider_models(**kwargs) == models
        assert len(calls) == 2
        assert list_merged_provider_models(**kwargs, force_refresh=True) == models
        assert len(calls) == 3


def test_catalog_cache_expiry_and_organization_invalidation(monkeypatch) -> None:
    """Expiry and organization-prefix invalidation leave other tenants intact."""
    now = 100.0
    monkeypatch.setattr(
        "apps.harness.model_catalog_service.time.monotonic", lambda: now
    )
    cache = ModelsCatalogCache()
    for key in ("org", "org:accounts:one", "other:accounts:two"):
        cache.set(key, [])
    cache.invalidate("org")
    assert cache.get("org") is None
    assert cache.get("org:accounts:one") is None
    assert cache.get("other:accounts:two") == []
    now += CACHE_TTL_SECONDS
    assert cache.get("other:accounts:two") is None
