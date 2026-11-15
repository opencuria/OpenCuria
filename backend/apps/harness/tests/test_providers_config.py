"""Tests for organization-wide provider defaults."""

from __future__ import annotations

import pytest
from django.db import IntegrityError

from apps.harness.models import ProviderConfig
from apps.harness.services import ProviderConfigService
from common.exceptions import NotFoundError


@pytest.mark.django_db
def test_save_config_upserts_defaults_and_efforts(organization) -> None:
    """Defaults and reasoning efforts are saved on create and updated on upsert."""
    service = ProviderConfigService()
    config = service.save_config(
        organization_id=organization.id,
        small_model="model-b",
        small_effort="low",
    )
    assert config.small_model == "model-b"
    assert config.small_effort == "low"

    updated = service.save_config(
        organization_id=organization.id,
        small_model="model-d",
        small_effort="medium",
    )
    assert updated.id == config.id
    assert updated.small_model == "model-d"
    assert updated.small_effort == "medium"
    assert ProviderConfig.objects.filter(organization_id=organization.id).count() == 1


@pytest.mark.django_db
def test_org_unique_constraint(organization) -> None:
    """A second config for the same org violates the OneToOne mapping."""
    ProviderConfig.objects.create(organization=organization)
    with pytest.raises(IntegrityError):
        ProviderConfig.objects.create(organization=organization)


@pytest.mark.django_db
def test_get_missing_config_raises(organization) -> None:
    """Missing config raises NotFoundError."""
    service = ProviderConfigService()
    with pytest.raises(NotFoundError):
        service.get_config(organization.id)
