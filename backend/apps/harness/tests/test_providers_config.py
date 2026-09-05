"""Tests for ProviderConfig encryption, uniqueness and service."""

from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.harness.models import ProviderConfig
from apps.harness.providers.openrouter import OpenRouterAdapter
from apps.harness.services import ProviderConfigService
from common.exceptions import NotFoundError
from common.utils import decrypt_value


@pytest.mark.django_db
def test_encrypt_decrypt_roundtrip(organization) -> None:
    """API key survives save/load without plaintext in the DB row."""
    service = ProviderConfigService()
    config = service.save_config(
        organization_id=organization.id,
        api_key="sk-secret-123",
        default_model="model-a",
        small_model="model-b",
    )
    assert config.api_key_encrypted != "sk-secret-123"
    assert "sk-secret-123" not in config.api_key_encrypted
    assert service.get_decrypted_api_key(organization.id) == "sk-secret-123"

    raw = ProviderConfig.objects.get(id=config.id)
    assert raw.api_key_encrypted != "sk-secret-123"
    assert decrypt_value(raw.api_key_encrypted) == "sk-secret-123"


@pytest.mark.django_db
def test_org_unique_constraint(organization) -> None:
    """A second config for the same org violates the OneToOne mapping."""
    ProviderConfig.objects.create(
        organization=organization,
        api_key_encrypted="enc",
        base_url="https://openrouter.ai/api/v1",
    )
    with pytest.raises(IntegrityError):
        ProviderConfig.objects.create(
            organization=organization,
            api_key_encrypted="enc2",
            base_url="https://openrouter.ai/api/v1",
        )


@pytest.mark.django_db
def test_save_config_upserts(organization) -> None:
    """Saving twice updates instead of duplicating."""
    service = ProviderConfigService()
    first = service.save_config(organization_id=organization.id, api_key="key-1")
    second = service.save_config(organization_id=organization.id, api_key="key-2")
    assert first.id == second.id
    assert service.get_decrypted_api_key(organization.id) == "key-2"
    assert ProviderConfig.objects.filter(organization_id=organization.id).count() == 1


@pytest.mark.django_db
def test_get_missing_config_raises(organization) -> None:
    """Missing config raises NotFoundError."""
    service = ProviderConfigService()
    with pytest.raises(NotFoundError):
        service.get_config(organization.id)


@pytest.mark.django_db
def test_build_adapter_uses_stored_config(organization) -> None:
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

    with pytest.raises(KeyError):
        service.build_adapter(organization.id, provider_name="nope")
