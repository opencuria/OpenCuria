"""
Service layer for the harness provider configuration.

Handles org-wide small-model defaults and resolves account-bound
provider adapters via :class:`~apps.harness.providers.resolver.ProviderResolver`.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from apps.organizations.services import OrganizationService
from common.exceptions import NotFoundError

from .agents.definitions import AGENT_DEFINITIONS
from .constants import DEFAULT_MAX_DEPTH, MAX_SUBAGENT_DEPTH
from .models import ProviderConfig, RecentModel
from .provider_accounts import ProviderAccountService
from .providers.model_ref import parse_model_ref
from .providers.models_catalog import (
    ProviderModel,
    clear_models_cache,
    list_merged_provider_models,
    normalize_reasoning_effort,
)
from .providers.resolver import ProviderResolver
from .repositories import (
    AgentConfigRepository,
    ComputerUseConfigRepository,
    ProviderConfigRepository,
    RecentModelRepository,
    SubagentConfigRepository,
)

log = structlog.get_logger(__name__)


CONFIGURABLE_AGENTS = ("build", "plan", "general", "explore", "computeruse")
PRIMARY_AGENTS = ("build", "plan")
VALID_EFFORT_STRATEGIES = ("fixed", "inherit", "lowest", "medium", "highest")


class AgentConfigService:
    """Business logic for per-agent model/effort configuration."""

    def __init__(
        self,
        repository: type[AgentConfigRepository] | None = None,
    ) -> None:
        self.repository = repository or AgentConfigRepository
        self.organizations = OrganizationService()

    def list_configs(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return one row per configurable agent with stored values or defaults."""
        stored = {row.agent: row for row in self.repository.list_by_org(org_id)}
        rows: list[dict[str, Any]] = []
        for name in CONFIGURABLE_AGENTS:
            definition = AGENT_DEFINITIONS[name]
            row = stored.get(name)
            if definition.mode == "primary":
                rows.append(
                    {
                        "agent": name,
                        "mode": definition.mode,
                        "description": definition.description,
                        "model": (row.model if row else "") or "",
                        "effort": (row.effort if row else "") or "",
                        "inherit_model": False,
                        "effort_strategy": "fixed",
                    }
                )
            else:
                rows.append(
                    {
                        "agent": name,
                        "mode": definition.mode,
                        "description": definition.description,
                        "model": (row.model if row else "") or "",
                        "effort": (row.effort if row else "") or "",
                        "inherit_model": row.inherit_model if row else True,
                        "effort_strategy": (row.effort_strategy if row else "inherit"),
                    }
                )
        return rows

    def save_configs(
        self, org_id: uuid.UUID, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate and upsert per-agent configs, then return the list view."""
        org = self.organizations.require_organization(org_id)
        for item in items:
            agent = str(item.get("agent") or "").strip().lower()
            if agent not in CONFIGURABLE_AGENTS:
                raise ValueError(f"Unknown agent '{item.get('agent')}'")
            model = str(item.get("model") or "").strip()
            effort = str(item.get("effort") or "").strip()
            inherit_model = bool(item.get("inherit_model", False))
            effort_strategy = str(item.get("effort_strategy") or "fixed").strip()
            if effort_strategy not in VALID_EFFORT_STRATEGIES:
                raise ValueError(
                    f"Invalid effort_strategy '{effort_strategy}' for '{agent}'"
                )
            if agent in PRIMARY_AGENTS:
                inherit_model = False
                effort_strategy = "fixed"
                effort = normalize_reasoning_effort(effort)
            else:
                if inherit_model:
                    model = ""
                    effort = ""
                    if effort_strategy == "fixed":
                        effort_strategy = "inherit"
                else:
                    effort_strategy = "fixed"
                    effort = normalize_reasoning_effort(effort)
            self.repository.upsert(
                org,
                agent,
                model=model,
                effort=effort,
                inherit_model=inherit_model,
                effort_strategy=effort_strategy,
            )
            log.info("agent_config_saved", organization_id=str(org_id), agent=agent)
        return self.list_configs(org_id)


class SubagentConfigService:
    """Read and persist org-wide maximum subagent nesting depth."""

    def __init__(
        self, repository: type[SubagentConfigRepository] | None = None
    ) -> None:
        self.repository = repository or SubagentConfigRepository
        self.organizations = OrganizationService()

    def get_or_default(self, org_id: uuid.UUID) -> dict[str, int]:
        """Return the configured depth or defaults without creating a row."""
        row = self.repository.get_by_org(org_id)
        return {"max_depth": row.max_depth if row else DEFAULT_MAX_DEPTH}

    def save_config(self, org_id: uuid.UUID, max_depth: int) -> dict[str, int]:
        """Validate and upsert depth for an existing organization."""
        if type(max_depth) is not int or not 1 <= max_depth <= MAX_SUBAGENT_DEPTH:
            raise ValueError(f"max_depth must be an integer in 1..{MAX_SUBAGENT_DEPTH}")
        self.organizations.require_organization(org_id)
        row = self.repository.upsert(org_id, max_depth)
        return {"max_depth": row.max_depth}


class ComputerUseConfigService:
    """Validate the existing organization-scoped computer-use settings."""

    DEFAULTS = {
        "max_steps": 15,
        "enable_recording": False,
        "screenshot_max_dimension": 2400,
    }

    def __init__(self, repository=None) -> None:
        self.repository = repository or ComputerUseConfigRepository
        self.organizations = OrganizationService()

    def get_or_default(self, org_id: uuid.UUID) -> dict[str, Any]:
        """Read the settings without creating a database row."""
        row = self.repository.get_by_org(org_id)
        return {
            name: getattr(row, name) if row else default
            for name, default in self.DEFAULTS.items()
        }

    def save_config(self, org_id: uuid.UUID, values: dict[str, Any]) -> dict[str, Any]:
        """Merge a validated partial update over the current configuration."""
        if set(values) - self.DEFAULTS.keys():
            raise ValueError("Unknown computer-use setting")
        merged = {**self.get_or_default(org_id), **values}
        for name, minimum, maximum in (
            ("max_steps", 1, 1000),
            ("screenshot_max_dimension", 320, 7680),
        ):
            value = merged[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{name} must be an integer in {minimum}..{maximum}")
        if not isinstance(merged["enable_recording"], bool):
            raise ValueError("enable_recording must be a boolean")
        self.organizations.require_organization(org_id)
        row = self.repository.get_by_org(org_id)
        if row is None:
            self.repository.create(organization_id=org_id, **merged)
        else:
            self.repository.update(row, **merged)
        return merged


class ProviderConfigService:
    """Business logic for org-wide small-model configuration."""

    def __init__(
        self,
        repository: type[ProviderConfigRepository] | None = None,
    ) -> None:
        self.repository = repository or ProviderConfigRepository

    def save_config(
        self,
        *,
        organization_id: uuid.UUID,
        small_model: str = "",
        small_effort: str = "",
    ) -> ProviderConfig:
        """Create or update the small model and reasoning effort."""
        existing = self.repository.get_by_org(organization_id)

        if existing is None:
            config = self.repository.create(
                organization_id=organization_id,
                small_model=small_model.strip(),
                small_effort=small_effort.strip(),
            )
            log.info("provider_config_created", organization_id=str(organization_id))
        else:
            config = self.repository.update(
                existing,
                small_model=small_model.strip(),
                small_effort=small_effort.strip(),
            )
            log.info("provider_config_updated", organization_id=str(organization_id))

        clear_models_cache(str(organization_id))
        return config

    def find_config(self, organization_id: uuid.UUID) -> ProviderConfig | None:
        """Return the provider config for an organization, or None if unset."""
        return self.repository.get_by_org(organization_id)

    def get_config(self, organization_id: uuid.UUID) -> ProviderConfig:
        """Return the provider config for an organization or raise."""
        config = self.find_config(organization_id)
        if config is None:
            raise NotFoundError("ProviderConfig", str(organization_id))
        return config

    def delete_config(self, organization_id: uuid.UUID) -> None:
        """Delete the provider config for an organization."""
        deleted = self.repository.delete_by_org(organization_id)
        if deleted == 0:
            raise NotFoundError("ProviderConfig", str(organization_id))
        clear_models_cache(str(organization_id))
        log.info("provider_config_deleted", organization_id=str(organization_id))

    def build_resolver(
        self,
        organization_id: uuid.UUID,
        *,
        user_id: int,
        account_ids: dict[str, str] | None = None,
        on_account_used: Any | None = None,
    ) -> ProviderResolver:
        """Build a resolver for the user's active or answer-frozen accounts."""
        return ProviderResolver(
            organization_id,
            user_id=user_id,
            account_ids=account_ids,
            on_account_used=on_account_used,
        )

    def list_models(
        self,
        organization_id: uuid.UUID,
        *,
        user_id: int,
        account_ids: dict[str, str] | None = None,
    ) -> list[ProviderModel]:
        """List models offered by precisely the selected accounts."""
        from .providers.models_catalog import selected_catalog_connections

        if not selected_catalog_connections(organization_id, user_id, account_ids):
            raise NotFoundError("ProviderConnection", str(organization_id))
        return list_merged_provider_models(
            organization_id=organization_id,
            user_id=user_id,
            account_ids=account_ids,
        )

    def provider_connected_for_model(
        self,
        organization_id: uuid.UUID,
        model: str,
        *,
        user_id: int,
        account_ids: dict[str, str] | None = None,
    ) -> str:
        """Validate a provider against the caller's selected/frozen account."""
        provider_id, _ = parse_model_ref(model)
        account_service = ProviderAccountService()
        account_id = (account_ids or {}).get(provider_id)
        if account_ids is not None:
            connection = (
                account_service.get_account(organization_id, user_id, account_id)
                if account_id is not None
                else None
            )
        else:
            connection = account_service.selected_accounts(
                organization_id, user_id
            ).get(provider_id)
        if connection is None or connection.provider != provider_id:
            raise ValueError(f"Provider '{provider_id}' is not connected")
        return provider_id


class RecentModelService:
    """Per-user recently used composer models (LRU, org-scoped)."""

    def __init__(self, repository: type[RecentModelRepository] | None = None) -> None:
        self.repository = repository or RecentModelRepository

    def list_for_user(self, org_id: uuid.UUID, user_id: object) -> list[RecentModel]:
        """Return the user's recent models, newest first."""
        return self.repository.list_by_user(org_id, user_id)

    def record_usage(
        self, org_id: uuid.UUID, user, *, model: str, effort: str = ""
    ) -> RecentModel | None:
        """Record one used model; None when the model id is blank."""
        return self.repository.record_usage(org_id, user, model=model, effort=effort)
