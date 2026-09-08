"""Tests for ProviderResolver and per-model adapter construction."""

from __future__ import annotations

import json
import asyncio

import pytest

from apps.harness.providers.bedrock import BedrockAdapter
from apps.harness.providers.chatgpt import ChatGPTAdapter
from apps.harness.providers.openrouter import OpenRouterAdapter
from apps.harness.providers.resolver import ProviderResolver, StaticModelResolver
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.services import ProviderConfigService
from apps.harness.tests.test_runner_loop import FakeProvider, _text_step
from common.utils import decrypt_value


@pytest.mark.django_db
def test_provider_resolver_openrouter_adapter(organization) -> None:
    """OpenRouter connections build OpenRouterAdapter with stored secrets."""
    service = ProviderConfigService()
    service.save_config(
        organization_id=organization.id,
        api_key="sk-live",
        base_url="https://example.com/v1",
        default_model="openai/gpt-5",
    )
    resolver = service.build_resolver(organization.id)
    resolved = resolver.resolve("openai/gpt-5")
    assert isinstance(resolved.adapter, OpenRouterAdapter)
    assert resolved.model_id == "openai/gpt-5"
    assert resolved.provider == "openrouter"


@pytest.mark.django_db
def test_provider_resolver_missing_connection_raises(organization) -> None:
    """Missing provider connections raise a clear ValueError."""
    service = ProviderConfigService()
    service.save_config(organization_id=organization.id, default_model="m")
    resolver = service.build_resolver(organization.id)
    with pytest.raises(ValueError, match="Provider 'chatgpt' is not connected"):
        resolver.resolve("chatgpt/gpt-5.5")


@pytest.mark.django_db
def test_provider_resolver_bedrock_region_model_id(organization) -> None:
    """Bedrock resolves bare catalog ids with the stored region."""
    service = ProviderConfigService()
    service.save_config(organization_id=organization.id, default_model="m")
    service.save_connection(
        organization_id=organization.id,
        provider="amazon-bedrock",
        credentials={"access_key_id": "a", "secret_access_key": "b"},
        config={"region": "us-east-1"},
    )
    resolver = service.build_resolver(organization.id)
    resolved = resolver.resolve(
        "amazon-bedrock/anthropic.claude-sonnet-4-5",
    )
    assert isinstance(resolved.adapter, BedrockAdapter)
    assert resolved.model_id.startswith("us.")
    assert resolved.provider == "amazon-bedrock"


@pytest.mark.django_db(transaction=True)
def test_provider_resolver_chatgpt_token_refresh_persists(organization) -> None:
    """ChatGPT on_tokens_refreshed re-encrypts and stores updated credentials."""
    service = ProviderConfigService()
    service.save_config(organization_id=organization.id, default_model="m")
    service.save_connection(
        organization_id=organization.id,
        provider="chatgpt",
        credentials={
            "access": "old",
            "refresh": "refresh-token",
            "expires": 1,
            "account_id": "acct",
        },
        config={},
    )
    resolver = service.build_resolver(organization.id)
    resolved = resolver.resolve("chatgpt/gpt-5.5")
    assert isinstance(resolved.adapter, ChatGPTAdapter)
    connection = service.get_connection(organization.id, "chatgpt")
    assert connection is not None

    refreshed = {
        "access": "new-access",
        "refresh": "refresh-token",
        "expires": 999,
        "account_id": "acct",
    }
    asyncio.run(resolver._chatgpt_tokens_refreshed(refreshed))
    connection.refresh_from_db()
    assert connection is not None
    stored = json.loads(decrypt_value(connection.credentials_encrypted))
    assert stored["access"] == "new-access"
    assert stored["expires"] == 999


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
