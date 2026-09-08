"""Tests for multi-provider catalog merge and model ref parsing."""

from __future__ import annotations

import json

import httpx
import pytest

from apps.harness.providers.bedrock import resolve_bedrock_model_id
from apps.harness.providers.model_ref import namespaced_model_id, parse_model_ref
from apps.harness.providers.models_catalog import (
    bedrock_models,
    chatgpt_models,
    clear_models_cache,
    list_merged_provider_models,
)
from apps.harness.services import ProviderConfigService
from common.utils import decrypt_value


def test_parse_model_ref_namespaced_and_legacy() -> None:
    """Namespaced refs split on the first slash; legacy ids stay OpenRouter."""
    assert parse_model_ref("openrouter/openai/gpt-5") == (
        "openrouter",
        "openai/gpt-5",
    )
    assert parse_model_ref("chatgpt/gpt-5.5") == ("chatgpt", "gpt-5.5")
    assert parse_model_ref("amazon-bedrock/us.anthropic.claude-sonnet-4-5") == (
        "amazon-bedrock",
        "us.anthropic.claude-sonnet-4-5",
    )
    assert parse_model_ref("openai/gpt-5") == ("openrouter", "openai/gpt-5")
    assert namespaced_model_id("openai/gpt-5") == "openrouter/openai/gpt-5"


def test_chatgpt_models_static_catalog() -> None:
    """ChatGPT allowlist is namespaced and tool-capable."""
    models = chatgpt_models()
    assert len(models) == 4
    assert models[0].id == "chatgpt/gpt-5.5"
    assert models[0].provider == "chatgpt"
    assert models[0].supports_tools is True
    assert models[0].context_length == 400_000
    assert models[0].max_output_tokens == 128_000
    assert "xhigh" in models[0].reasoning_efforts


def test_bedrock_models_region_prefixing() -> None:
    """Bedrock catalog ids are region-resolved then namespaced."""
    models = bedrock_models("us-east-1")
    sonnet = next(model for model in models if "claude-sonnet-4-5" in model.id)
    expected = resolve_bedrock_model_id(
        "anthropic.claude-sonnet-4-5",
        "us-east-1",
    )
    assert sonnet.id == f"amazon-bedrock/{expected}"
    assert sonnet.provider == "amazon-bedrock"
    assert sonnet.supports_tools is True


@pytest.mark.django_db
def test_list_merged_provider_models_only_connected(organization) -> None:
    """Merged catalog includes only providers with stored connections."""
    clear_models_cache()
    service = ProviderConfigService()
    service.save_config(organization_id=organization.id, default_model="m")
    service.save_connection(
        organization_id=organization.id,
        provider="chatgpt",
        credentials={"access": "a", "refresh": "r", "expires": 1},
        config={},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "acme/fast", "name": "Fast"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    models = list_merged_provider_models(
        organization_id=organization.id,
        client=client,
    )
    providers = {model.provider for model in models}
    assert "chatgpt" in providers
    assert "openrouter" not in providers
    assert any(model.id == "chatgpt/gpt-5.5" for model in models)
    clear_models_cache(str(organization.id))


@pytest.mark.django_db
def test_list_merged_provider_models_openrouter_and_bedrock(
    organization,
) -> None:
    """OpenRouter live fetch and Bedrock static catalogs merge together."""
    clear_models_cache()
    service = ProviderConfigService()
    service.save_config(
        organization_id=organization.id,
        api_key="sk-live",
        default_model="openrouter/acme/fast",
    )
    service.save_connection(
        organization_id=organization.id,
        provider="amazon-bedrock",
        credentials={"access_key_id": "a", "secret_access_key": "b"},
        config={"region": "us-west-2"},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"id": "acme/fast", "name": "Fast"}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    models = list_merged_provider_models(
        organization_id=organization.id,
        client=client,
    )
    ids = {model.id for model in models}
    assert "openrouter/acme/fast" in ids
    assert any(model.id.startswith("amazon-bedrock/") for model in models)
    clear_models_cache(str(organization.id))
