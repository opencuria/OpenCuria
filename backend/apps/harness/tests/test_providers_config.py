"""Tests for ProviderConfig encryption, uniqueness and service."""

from __future__ import annotations

import json

import pytest
from django.db import IntegrityError

from apps.harness.models import ProviderConfig
from apps.harness.providers.openrouter import OpenRouterAdapter
from apps.harness.services import ProviderConfigService
from common.exceptions import NotFoundError
from common.utils import decrypt_value


@pytest.mark.django_db
def test_save_config_routes_openrouter_credentials(organization) -> None:
    """OpenRouter API keys are stored on ProviderConnection, not ProviderConfig."""
    service = ProviderConfigService()
    config = service.save_config(
        organization_id=organization.id,
        api_key="sk-secret-123",
        default_model="model-a",
        small_model="model-b",
    )
    connection = service.get_connection(organization.id, "openrouter")
    assert connection is not None
    assert connection.credentials_encrypted != "sk-secret-123"
    stored = json.loads(decrypt_value(connection.credentials_encrypted))
    assert stored["api_key"] == "sk-secret-123"
    assert service.get_decrypted_api_key(organization.id) == "sk-secret-123"
    assert not hasattr(config, "api_key_encrypted")


@pytest.mark.django_db
def test_org_unique_constraint(organization) -> None:
    """A second config for the same org violates the OneToOne mapping."""
    ProviderConfig.objects.create(
        organization=organization,
        default_model="m1",
    )
    with pytest.raises(IntegrityError):
        ProviderConfig.objects.create(
            organization=organization,
            default_model="m2",
        )


@pytest.mark.django_db
def test_save_config_upserts(organization) -> None:
    """Saving twice updates instead of duplicating."""
    service = ProviderConfigService()
    first = service.save_config(
        organization_id=organization.id,
        api_key="key-1",
        default_model="m1",
    )
    second = service.save_config(
        organization_id=organization.id,
        api_key="key-2",
        default_model="m2",
    )
    assert first.id == second.id
    assert service.get_decrypted_api_key(organization.id) == "key-2"
    assert ProviderConfig.objects.filter(organization_id=organization.id).count() == 1


@pytest.mark.django_db
def test_save_config_create_without_api_key(organization) -> None:
    """Default models can be saved before any provider connection exists."""
    service = ProviderConfigService()
    config = service.save_config(
        organization_id=organization.id,
        default_model="model-a",
    )
    assert config.default_model == "model-a"
    assert service.get_connection(organization.id, "openrouter") is None


@pytest.mark.django_db
def test_get_missing_config_raises(organization) -> None:
    """Missing config raises NotFoundError."""
    service = ProviderConfigService()
    with pytest.raises(NotFoundError):
        service.get_config(organization.id)


@pytest.mark.django_db
def test_build_adapter_uses_stored_connection(organization) -> None:
    """build_adapter decrypts the key and honors the stored base URL."""
    service = ProviderConfigService()
    service.save_config(
        organization_id=organization.id,
        api_key="sk-live",
        base_url="https://example.com/v1",
        default_model="m",
    )
    adapter = service.build_adapter(organization.id)
    assert isinstance(adapter, OpenRouterAdapter)
    assert adapter._base_url == "https://example.com/v1"

    with pytest.raises(NotFoundError):
        service.build_adapter(organization.id, provider_name="nope")


@pytest.mark.django_db
def test_save_config_persists_reasoning_efforts(organization) -> None:
    """Effort defaults are stored on create and updated on upsert."""
    service = ProviderConfigService()
    config = service.save_config(
        organization_id=organization.id,
        default_model="m1",
        default_effort="high",
        small_effort="low",
        computer_use_effort="medium",
    )
    assert config.default_effort == "high"
    assert config.small_effort == "low"
    assert config.computer_use_effort == "medium"

    updated = service.save_config(
        organization_id=organization.id,
        default_model="m1",
        default_effort="low",
        small_effort="medium",
        computer_use_effort="high",
    )
    assert updated.id == config.id
    assert updated.default_effort == "low"
    assert updated.small_effort == "medium"
    assert updated.computer_use_effort == "high"
