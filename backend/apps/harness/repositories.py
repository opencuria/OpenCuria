"""
Repository layer for the harness app.

Encapsulates all database queries. Services never use the ORM directly.
"""

from __future__ import annotations

import uuid

from .models import ProviderConfig


class ProviderConfigRepository:
    """Data access for ProviderConfig records."""

    @staticmethod
    def get_by_org(org_id: uuid.UUID) -> ProviderConfig | None:
        """Fetch the provider config for an organization."""
        return ProviderConfig.objects.filter(organization_id=org_id).first()

    @staticmethod
    def get_by_id(config_id: uuid.UUID) -> ProviderConfig | None:
        """Fetch a provider config by ID."""
        return ProviderConfig.objects.filter(id=config_id).first()

    @staticmethod
    def create(
        *,
        organization_id: uuid.UUID,
        api_key_encrypted: str,
        base_url: str,
        default_model: str = "",
        small_model: str = "",
    ) -> ProviderConfig:
        """Create a provider config for an organization."""
        return ProviderConfig.objects.create(
            organization_id=organization_id,
            api_key_encrypted=api_key_encrypted,
            base_url=base_url,
            default_model=default_model,
            small_model=small_model,
        )

    @staticmethod
    def update(
        config: ProviderConfig,
        *,
        api_key_encrypted: str | None = None,
        base_url: str | None = None,
        default_model: str | None = None,
        small_model: str | None = None,
    ) -> ProviderConfig:
        """Update provider config fields."""
        update_fields = ["updated_at"]
        if api_key_encrypted is not None:
            config.api_key_encrypted = api_key_encrypted
            update_fields.append("api_key_encrypted")
        if base_url is not None:
            config.base_url = base_url
            update_fields.append("base_url")
        if default_model is not None:
            config.default_model = default_model
            update_fields.append("default_model")
        if small_model is not None:
            config.small_model = small_model
            update_fields.append("small_model")
        config.save(update_fields=update_fields)
        return config

    @staticmethod
    def delete_by_org(org_id: uuid.UUID) -> int:
        """Delete the provider config for an organization."""
        count, _ = ProviderConfig.objects.filter(organization_id=org_id).delete()
        return count
