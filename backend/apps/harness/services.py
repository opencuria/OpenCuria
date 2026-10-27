"""
Service layer for the harness provider configuration.

Handles org-wide small-model defaults and resolves account-bound
provider adapters via :class:`~apps.harness.providers.resolver.ProviderResolver`.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from apps.organizations.repositories import OrganizationRepository
from common.exceptions import NotFoundError

from .agent_s.config import AgentSRunConfig
from .agents.definitions import AGENT_DEFINITIONS
from .constants import DEFAULT_MAX_DEPTH, MAX_SUBAGENT_DEPTH
from .models import AgentSConfig, ProviderConfig, RecentModel
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
    AgentSConfigRepository,
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

    def list_configs(self, org_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return one row per configurable agent with stored values or defaults."""
        stored = {
            row.agent: row for row in self.repository.list_by_org(org_id)
        }
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
                        "effort_strategy": (
                            row.effort_strategy if row else "inherit"
                        ),
                    }
                )
        return rows

    def save_configs(
        self, org_id: uuid.UUID, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate and upsert per-agent configs, then return the list view."""
        org = OrganizationRepository.get_by_id(org_id)
        if org is None:
            raise NotFoundError("Organization", str(org_id))
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

    def get_or_default(self, org_id: uuid.UUID) -> dict[str, int]:
        """Return the configured depth or defaults without creating a row."""
        row = self.repository.get_by_org(org_id)
        return {"max_depth": row.max_depth if row else DEFAULT_MAX_DEPTH}

    def save_config(self, org_id: uuid.UUID, max_depth: int) -> dict[str, int]:
        """Validate and upsert depth for an existing organization."""
        if type(max_depth) is not int or not 1 <= max_depth <= MAX_SUBAGENT_DEPTH:
            raise ValueError(
                f"max_depth must be an integer in 1..{MAX_SUBAGENT_DEPTH}"
            )
        if OrganizationRepository.get_by_id(org_id) is None:
            raise NotFoundError("Organization", str(org_id))
        row = self.repository.upsert(org_id, max_depth)
        return {"max_depth": row.max_depth}


class AgentSConfigService:
    """Business logic for the org-wide Agent-S harness config."""

    DEFAULTS: dict[str, object] = {
        "grounding_model": "",
        "grounding_width": 1920,
        "grounding_height": 1080,
        "model_temperature": None,
        "max_steps": 15,
        "max_trajectory_length": 8,
        "enable_reflection": True,
        "enable_code_agent": True,
        "enable_recording": False,
        "screenshot_max_dimension": 2400,
        "action_pre_delay": 1.0,
        "action_post_delay": 1.0,
        "wait_delay": 5.0,
    }

    def __init__(
        self,
        repository: type[AgentSConfigRepository] | None = None,
    ) -> None:
        self.repository = repository or AgentSConfigRepository

    def _defaults_row(self, org_id: uuid.UUID) -> dict[str, Any]:
        """Return the default Agent-S config view for an org."""
        return {"organization_id": str(org_id), **dict(self.DEFAULTS)}

    def get_or_default(self, org_id: uuid.UUID) -> dict[str, Any]:
        """Return the stored config view, or defaults when unstored."""
        row = self.repository.get_by_org(org_id)
        if row is None:
            return self._defaults_row(org_id)
        return self._row_to_dict(row)

    def list_config(self, org_id: uuid.UUID) -> dict[str, Any]:
        """List-view alias for :meth:`get_or_default`."""
        return self.get_or_default(org_id)

    @staticmethod
    def _row_to_dict(row: AgentSConfig) -> dict[str, Any]:
        """Map an AgentSConfig ORM row to a plain view dict."""
        return {
            "organization_id": str(row.organization_id),
            "grounding_model": row.grounding_model or "",
            "grounding_width": int(row.grounding_width),
            "grounding_height": int(row.grounding_height),
            "model_temperature": (
                None if row.model_temperature is None else float(row.model_temperature)
            ),
            "max_steps": int(row.max_steps),
            "max_trajectory_length": int(row.max_trajectory_length),
            "enable_reflection": bool(row.enable_reflection),
            "enable_code_agent": bool(row.enable_code_agent),
            "enable_recording": bool(
                getattr(row, "enable_recording", False) or False
            ),
            "screenshot_max_dimension": int(row.screenshot_max_dimension),
            "action_pre_delay": float(row.action_pre_delay),
            "action_post_delay": float(row.action_post_delay),
            "wait_delay": float(row.wait_delay),
        }

    @staticmethod
    def _validate(values: dict[str, Any]) -> dict[str, Any]:
        """Validate raw values; return normalized fields (raises ValueError)."""
        normalized: dict[str, Any] = {}
        grounding_model = str(values.get("grounding_model", "") or "").strip()
        normalized["grounding_model"] = grounding_model
        for name in (
            "grounding_width",
            "grounding_height",
            "screenshot_max_dimension",
        ):
            raw = values.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int):
                raise ValueError(f"AgentSConfig.{name} must be a positive int")
            if raw < 1 or raw > 7680:
                raise ValueError(
                    f"AgentSConfig.{name} must be an int in 1..7680, got {raw!r}"
                )
            normalized[name] = raw
        for name in ("max_steps", "max_trajectory_length"):
            raw = values.get(name)
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                raise ValueError(f"AgentSConfig.{name} must be a positive int")
            normalized[name] = raw
        temperature = values.get("model_temperature", None)
        if temperature is not None:
            if (
                isinstance(temperature, bool)
                or not isinstance(temperature, (int, float))
                or temperature < 0
                or temperature > 2
            ):
                raise ValueError(
                    "AgentSConfig.model_temperature must be None or a number "
                    f"in 0..2, got {temperature!r}"
                )
            normalized["model_temperature"] = float(temperature)
        else:
            normalized["model_temperature"] = None
        for name in ("enable_reflection", "enable_code_agent", "enable_recording"):
            raw = values.get(name)
            if not isinstance(raw, bool):
                raise ValueError(f"AgentSConfig.{name} must be a boolean")
            normalized[name] = raw
        for name in ("action_pre_delay", "action_post_delay", "wait_delay"):
            raw = values.get(name)
            if (
                isinstance(raw, bool)
                or not isinstance(raw, (int, float))
                or raw < 0
                or raw > 600
            ):
                raise ValueError(
                    f"AgentSConfig.{name} must be a number in 0..600, "
                    f"got {raw!r}"
                )
            normalized[name] = float(raw)
        return normalized

    def save_config(
        self, org_id: uuid.UUID, values: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate and upsert the org Agent-S config, return the view.

        Partial payloads are merged over the stored view (or SDK defaults
        when unstored) so PUT/save accept any subset of fields; unknown
        keys (``organization_id``/timestamps) are ignored. Validation is
        atomic: invalid values raise before any row is created/updated.
        """
        if OrganizationRepository.get_by_id(org_id) is None:
            raise NotFoundError("Organization", str(org_id))
        allowed = set(self.DEFAULTS)
        current = self.get_or_default(org_id)
        incoming = {
            key: value for key, value in dict(values or {}).items() if key in allowed
        }
        merged = {**{k: current[k] for k in allowed}, **incoming}
        normalized = self._validate(merged)
        existing = self.repository.get_by_org(org_id)
        if existing is None:
            row = self.repository.create(
                organization_id=org_id, **normalized  # type: ignore[arg-type]
            )
        else:
            row = self.repository.update(existing, **normalized)  # type: ignore[arg-type]
        log.info("agent_s_config_saved", organization_id=str(org_id))
        return self._row_to_dict(row)

    def to_run_config(
        self, org_id: uuid.UUID, *, main_model: str
    ) -> AgentSRunConfig:
        """Build an AgentSRunConfig for *main_model* with grounding fallback.

        ``grounding_model`` falls back to *main_model* when unconfigured;
        ``model_temperature`` passes through verbatim (None = SDK default).
        """
        from .agent_s.config import AgentSRunConfig

        view = self.get_or_default(org_id)
        model = (main_model or "").strip()
        if not model:
            raise ValueError("Agent-S run requires an effective harness model")
        grounding = str(view.get("grounding_model") or "").strip() or model
        return AgentSRunConfig(
            main_model=model,
            grounding_model=grounding,
            grounding_width=int(view["grounding_width"]),
            grounding_height=int(view["grounding_height"]),
            model_temperature=view.get("model_temperature"),
            max_steps=int(view["max_steps"]),
            max_trajectory_length=int(view["max_trajectory_length"]),
            enable_reflection=bool(view["enable_reflection"]),
            enable_code_agent=bool(view["enable_code_agent"]),
            enable_recording=bool(view.get("enable_recording", False)),
            screenshot_max_dimension=int(view["screenshot_max_dimension"]),
            action_pre_delay=float(view["action_pre_delay"]),
            action_post_delay=float(view["action_post_delay"]),
            wait_delay=float(view["wait_delay"]),
        )


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

        if not selected_catalog_connections(
            organization_id, user_id, account_ids
        ):
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
