"""Tests for ProviderResolver and per-model adapter construction."""

from __future__ import annotations

import pytest

from apps.harness.engines.base import RunOptions
from apps.harness.providers.bedrock import BedrockAdapter
from apps.harness.providers.openrouter import OpenRouterAdapter
from apps.harness.providers.resolver import ProviderResolver, StaticModelResolver
from apps.harness.runner import HarnessRunner
from apps.harness.services import ProviderConfigService
from apps.harness.tests.test_runner_loop import FakeProvider, _text_step


@pytest.mark.django_db
def test_provider_resolver_openrouter_adapter(
    organization, provider_account_factory, provider_account_user
) -> None:
    """OpenRouter accounts build OpenRouterAdapter with stored secrets."""
    provider_account_factory(
        "openrouter",
        base_url="https://example.com/v1",
    )
    service = ProviderConfigService()
    resolver = service.build_resolver(organization.id, user_id=provider_account_user.id)
    resolved = resolver.resolve("openai/gpt-5")
    assert isinstance(resolved.adapter, OpenRouterAdapter)
    assert resolved.model_id == "openai/gpt-5"
    assert resolved.provider == "openrouter"
    assert resolved.adapter._base_url == "https://example.com/v1"


@pytest.mark.django_db
def test_prepared_callback_resolver_routes_without_database_queries(
    organization,
    provider_account_factory,
    provider_account_user,
    django_assert_num_queries,
) -> None:
    """Routing a prepared account-bound adapter is safe on the async run loop."""
    account = provider_account_factory("openrouter")
    resolver = ProviderResolver(
        organization.id,
        user_id=provider_account_user.id,
        account_ids={"openrouter": str(account.id)},
        on_account_used=lambda _account_id: None,
    )
    resolver.prepare_adapters()

    with django_assert_num_queries(0):
        resolved = resolver.resolve("openrouter/acme/test-model")

    assert resolved.model_id == "acme/test-model"
    assert resolved.provider == "openrouter"
    assert resolved.adapter.name == "openrouter"


@pytest.mark.django_db
def test_provider_resolver_missing_connection_raises(
    organization, provider_account_user
) -> None:
    """Missing provider connections raise a clear ValueError."""
    service = ProviderConfigService()
    resolver = service.build_resolver(organization.id, user_id=provider_account_user.id)
    with pytest.raises(ValueError, match="Provider 'chatgpt' is not connected"):
        resolver.resolve("chatgpt/gpt-5.5")


@pytest.mark.django_db
def test_provider_resolver_bedrock_region_model_id(
    organization, provider_account_factory, provider_account_user
) -> None:
    """Bedrock resolves bare catalog ids with the stored region."""
    service = ProviderConfigService()
    provider_account_factory(
        "amazon-bedrock",
        access_key_id="a",
        secret_access_key="b",
        region="us-east-1",
        auth_method="access_keys",
    )
    resolver = service.build_resolver(organization.id, user_id=provider_account_user.id)
    resolved = resolver.resolve(
        "amazon-bedrock/anthropic.claude-sonnet-4-5",
    )
    assert isinstance(resolved.adapter, BedrockAdapter)
    assert resolved.model_id.startswith("us.")
    assert resolved.provider == "amazon-bedrock"


@pytest.mark.asyncio
async def test_runner_resolves_per_model_adapter() -> None:
    """HarnessRunner passes bare model ids to the adapter for each step."""
    primary = FakeProvider([_text_step("primary")])
    small = FakeProvider([_text_step("title-result")])
    primary.name = "openrouter"
    small.name = "chatgpt"

    class RoutingResolver:
        def __call__(self, model_ref: str):
            if model_ref == "chatgpt/gpt-5.5":
                return StaticModelResolver(small, provider_name="chatgpt").resolve(
                    model_ref
                )
            return StaticModelResolver(primary, provider_name="openrouter").resolve(
                model_ref
            )

    runner = HarnessRunner(
        model_resolver=RoutingResolver(),
        tools=__import__(
            "apps.harness.tools", fromlist=["default_tool_registry"]
        ).default_tool_registry(),
    )
    result = await runner.run(
        "hello",
        "title",
        "openrouter/openai/gpt-5",
        "build",
        RunOptions(small_model="chatgpt/gpt-5.5", auto_approve=True),
    )
    assert result.output == "title-result"
    assert small.calls[0]["model"] == "gpt-5.5"
