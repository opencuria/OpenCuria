"""
Service layer for the harness provider configuration.

Handles org-wide default models, per-provider connections, and builds
provider adapters via :class:`~apps.harness.providers.resolver.ProviderResolver`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import structlog

from common.exceptions import ConflictError, NotFoundError
from common.utils import decrypt_value, encrypt_value

from .agent_s.config import AgentSRunConfig
from .agents.definitions import AGENT_DEFINITIONS
from .models import AgentSConfig, ProviderConfig, ProviderConnection
from .providers.base import ProviderAdapter
from .providers.bedrock import BedrockAdapter
from .providers.chatgpt import ChatGPTAdapter
from .providers.model_ref import parse_model_ref
from .providers.models_catalog import (
    ProviderModel,
    clear_models_cache,
    list_merged_provider_models,
    normalize_reasoning_effort,
)
from .providers.openai_compatible import OpenAICompatibleAdapter
from .providers.openrouter import DEFAULT_BASE_URL, OpenRouterAdapter
from .providers.registry import ProviderRegistry, default_registry
from .providers.resolver import ProviderResolver
from .repositories import (
    AgentConfigRepository,
    AgentSConfigRepository,
    ProviderConfigRepository,
    ProviderConnectionRepository,
)

log = structlog.get_logger(__name__)


def _ensure_default_adapters(registry: ProviderRegistry) -> None:
    """Register the built-in adapters if missing."""
    if "openrouter" not in registry:
        registry.register("openrouter", OpenRouterAdapter)
    if "chatgpt" not in registry:
        # ProviderConnection resolver constructs ChatGPTAdapter with OAuth
        # credentials; registration exposes the factory for discovery only.
        registry.register("chatgpt", ChatGPTAdapter)
    if "amazon-bedrock" not in registry:
        # ProviderConnection resolver constructs BedrockAdapter with AWS
        # credentials; registration exposes the factory for discovery only.
        registry.register("amazon-bedrock", BedrockAdapter)
    if "openai-compatible" not in registry:
        # ProviderConnection resolver constructs OpenAICompatibleAdapter
        # with base_url/api_key; registration exposes it for discovery only.
        registry.register("openai-compatible", OpenAICompatibleAdapter)


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
        from apps.organizations.models import Organization

        org = Organization.objects.filter(id=org_id).first()
        if org is None:
            from common.exceptions import NotFoundError

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
        for name in ("enable_reflection", "enable_code_agent"):
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
        from apps.organizations.models import Organization

        org = Organization.objects.filter(id=org_id).first()
        if org is None:
            from common.exceptions import NotFoundError

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
            screenshot_max_dimension=int(view["screenshot_max_dimension"]),
            action_pre_delay=float(view["action_pre_delay"]),
            action_post_delay=float(view["action_post_delay"]),
            wait_delay=float(view["wait_delay"]),
        )


class ProviderConfigService:
    """Business logic for org-wide provider configuration."""

    def __init__(
        self,
        repository: type[ProviderConfigRepository] | None = None,
        connection_repository: type[ProviderConnectionRepository] | None = None,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.repository = repository or ProviderConfigRepository
        self.connections = connection_repository or ProviderConnectionRepository
        self.registry = registry or default_registry
        _ensure_default_adapters(self.registry)

    def save_config(
        self,
        *,
        organization_id: uuid.UUID,
        api_key: str = "",
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = "",
        small_model: str = "",
        computer_use_model: str = "",
        default_effort: str = "",
        small_effort: str = "",
        computer_use_effort: str = "",
    ) -> ProviderConfig:
        """Create or update org-wide default models and reasoning efforts.

        ``api_key`` and ``base_url`` are accepted for backward compatibility
        and routed to the OpenRouter :class:`ProviderConnection` when provided.
        """
        normalized_url = (base_url or DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        existing = self.repository.get_by_org(organization_id)
        key_provided = bool(api_key and api_key.strip())

        if existing is None:
            config = self.repository.create(
                organization_id=organization_id,
                default_model=default_model.strip(),
                small_model=small_model.strip(),
                computer_use_model=computer_use_model.strip(),
                default_effort=default_effort.strip(),
                small_effort=small_effort.strip(),
                computer_use_effort=computer_use_effort.strip(),
            )
            log.info("provider_config_created", organization_id=str(organization_id))
        else:
            config = self.repository.update(
                existing,
                default_model=default_model.strip(),
                small_model=small_model.strip(),
                computer_use_model=computer_use_model.strip(),
                default_effort=default_effort.strip(),
                small_effort=small_effort.strip(),
                computer_use_effort=computer_use_effort.strip(),
            )
            log.info("provider_config_updated", organization_id=str(organization_id))

        if key_provided:
            self.save_connection(
                organization_id=organization_id,
                provider="openrouter",
                credentials={"api_key": api_key.strip()},
                config={"base_url": normalized_url},
            )
        elif existing is not None:
            connection = self.connections.get_by_org_and_provider(
                organization_id,
                "openrouter",
            )
            if connection is not None and normalized_url != DEFAULT_BASE_URL:
                self.save_connection(
                    organization_id=organization_id,
                    provider="openrouter",
                    credentials=self.get_connection_credentials(connection),
                    config={"base_url": normalized_url},
                )

        clear_models_cache(str(organization_id))
        return config

    def get_config(self, organization_id: uuid.UUID) -> ProviderConfig:
        """Return the provider config for an organization or raise."""
        config = self.repository.get_by_org(organization_id)
        if config is None:
            raise NotFoundError("ProviderConfig", str(organization_id))
        return config

    def get_decrypted_api_key(self, organization_id: uuid.UUID) -> str:
        """Decrypt and return the OpenRouter API key (never log it)."""
        connection = self.connections.get_by_org_and_provider(
            organization_id,
            "openrouter",
        )
        if connection is None:
            raise NotFoundError("ProviderConnection", "openrouter")
        credentials = self.get_connection_credentials(connection)
        return str(credentials.get("api_key", "") or "")

    def delete_config(self, organization_id: uuid.UUID) -> None:
        """Delete the provider config for an organization."""
        deleted = self.repository.delete_by_org(organization_id)
        if deleted == 0:
            raise NotFoundError("ProviderConfig", str(organization_id))
        clear_models_cache(str(organization_id))
        log.info("provider_config_deleted", organization_id=str(organization_id))

    def get_connection(
        self,
        organization_id: uuid.UUID,
        provider: str,
    ) -> ProviderConnection | None:
        """Return one provider connection for an organization."""
        return self.connections.get_by_org_and_provider(organization_id, provider)

    def list_connections(self, organization_id: uuid.UUID) -> list[ProviderConnection]:
        """Return all provider connections for an organization."""
        return self.connections.list_by_org(organization_id)

    @staticmethod
    def get_connection_credentials(connection: ProviderConnection) -> dict[str, Any]:
        """Decrypt stored provider credentials as a JSON object."""
        if not (connection.credentials_encrypted or "").strip():
            return {}
        payload = decrypt_value(connection.credentials_encrypted)
        parsed = json.loads(payload)
        return parsed if isinstance(parsed, dict) else {}

    def save_connection(
        self,
        *,
        organization_id: uuid.UUID,
        provider: str,
        credentials: dict[str, Any],
        config: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        """Create or update a provider connection and invalidate the catalog."""
        encrypted = encrypt_value(json.dumps(credentials))
        connection = self.connections.upsert(
            organization_id=organization_id,
            provider=provider,
            credentials_encrypted=encrypted,
            config=dict(config or {}),
        )
        clear_models_cache(str(organization_id))
        log.info(
            "provider_connection_saved",
            organization_id=str(organization_id),
            provider=provider,
        )
        return connection

    def delete_connection(
        self,
        organization_id: uuid.UUID,
        provider: str,
    ) -> None:
        """Delete a provider connection when present."""
        deleted = self.connections.delete_by_org_and_provider(organization_id, provider)
        if not deleted:
            raise NotFoundError("ProviderConnection", provider)
        clear_models_cache(str(organization_id))
        log.info(
            "provider_connection_deleted",
            organization_id=str(organization_id),
            provider=provider,
        )

    def build_resolver(self, organization_id: uuid.UUID) -> ProviderResolver:
        """Build a per-run resolver for the organization's connections."""
        return ProviderResolver(
            organization_id,
            registry=self.registry,
            connection_repository=self.connections,
        )

    def adapter_from_config(
        self,
        config: ProviderConfig,
        provider_name: str = "openrouter",
    ) -> ProviderAdapter:
        """Build a provider adapter from stored org connections.

        Legacy helper kept for tests; production runs should use
        :meth:`build_resolver`.
        """
        return self.build_adapter(config.organization_id, provider_name=provider_name)

    def build_adapter(
        self,
        organization_id: uuid.UUID,
        provider_name: str = "openrouter",
    ) -> ProviderAdapter:
        """Build a provider adapter from stored org connections.

        Raises:
            KeyError: If the provider name is not registered.
            NotFoundError: If no connection exists for the organization.
        """
        self.get_config(organization_id)
        connection = self.connections.get_by_org_and_provider(
            organization_id,
            provider_name,
        )
        if connection is None:
            raise NotFoundError("ProviderConnection", provider_name)
        credentials = self.get_connection_credentials(connection)
        config = dict(connection.config or {})
        if provider_name == "openrouter":
            adapter = OpenRouterAdapter(
                api_key=str(credentials.get("api_key", "") or ""),
                base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            )
        elif provider_name == "chatgpt":
            adapter = ChatGPTAdapter(credentials=credentials)
        elif provider_name == "amazon-bedrock":
            adapter = BedrockAdapter(
                credentials=credentials,
                region=str(config.get("region") or "us-east-1"),
            )
        elif provider_name == "openai-compatible":
            base_url = str(config.get("base_url", "") or "").strip()
            if not base_url:
                raise ValueError(
                    "Provider 'openai-compatible' requires base_url "
                    "in connection config"
                )
            adapter = OpenAICompatibleAdapter(
                base_url=base_url,
                api_key=str(credentials.get("api_key", "") or ""),
            )
        else:
            factory = self.registry.get(provider_name)
            adapter = factory(credentials=credentials, config=config)
        if not isinstance(adapter, ProviderAdapter):
            raise ConflictError(f"Provider {provider_name!r} did not build an adapter")
        return adapter

    def list_models(self, organization_id: uuid.UUID) -> list[ProviderModel]:
        """Return the merged catalog for all connected providers.

        Raises:
            NotFoundError: If no ProviderConfig exists for the organization.
        """
        self.get_config(organization_id)
        connections = self.connections.list_by_org(organization_id)
        if not connections:
            raise NotFoundError("ProviderConnection", str(organization_id))
        return list_merged_provider_models(organization_id=organization_id)

    def provider_connected_for_model(
        self, organization_id: uuid.UUID, model: str
    ) -> str:
        """Return the provider id for *model* when a connection exists."""
        provider_id, _ = parse_model_ref(model)
        connection = self.connections.get_by_org_and_provider(
            organization_id,
            provider_id,
        )
        if connection is None:
            raise ValueError(f"Provider '{provider_id}' is not connected")
        return provider_id

    # ------------------------------------------------------------------
    # OpenAI-compatible connection upsert (shared REST/MCP business logic)
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_openai_compatible_models(models: object) -> list[str]:
        """Deduplicate/trim manual model ids (empty list stays empty).

        Raises:
            ValueError: If any provided id is empty after trimming.
        """
        raw = models if isinstance(models, list) else []
        cleaned: list[str] = []
        seen: set[str] = set()
        for entry in raw:
            item = str(entry or "").strip()
            if not item:
                raise ValueError("models must not contain empty entries")
            if item in seen:
                continue
            seen.add(item)
            cleaned.append(item)
        return cleaned

    @staticmethod
    def validate_openai_compatible_base_url(base_url: str) -> str:
        """Validate an http/https base URL (raises ValueError)."""
        from .providers.openai_compatible import validate_base_url

        return validate_base_url(base_url)

    def upsert_openai_compatible_connection(
        self,
        organization_id: uuid.UUID,
        *,
        api_key: str = "",
        base_url: str = "",
        models: list[str] | None = None,
        models_provided: bool = False,
    ) -> ProviderConnection:
        """Create or update the generic OpenAI-compatible connection.

        Create requires ``base_url``; on update an empty/missing base URL
        keeps the stored value. ``models=None`` (or ``models_provided``
        False on update) keeps stored models; an explicit list — including
        ``[]`` — replaces them. Empty ids after trim raise; an empty total
        list is allowed. Empty ``api_key`` keeps the stored secret.
        """
        from .enums import ProviderType

        existing = self.get_connection(
            organization_id, ProviderType.OPENAI_COMPATIBLE
        )
        credentials = (
            self.get_connection_credentials(existing)
            if existing is not None
            else {}
        )
        stored_key = str(credentials.get("api_key", "") or "")
        if (api_key or "").strip():
            stored_key = api_key.strip()

        raw_url = (base_url or "").strip()
        if raw_url:
            resolved_base_url = self.validate_openai_compatible_base_url(raw_url)
        elif existing is not None:
            resolved_base_url = str(
                (existing.config or {}).get("base_url") or ""
            ).strip()
            if not resolved_base_url:
                raise ValueError("base_url is required")
        else:
            raise ValueError("base_url is required")

        if models_provided or models is not None:
            resolved_models = self.normalize_openai_compatible_models(
                list(models or [])
            )
        elif existing is not None:
            stored = (existing.config or {}).get("models", [])
            resolved_models = self.normalize_openai_compatible_models(stored)
        else:
            resolved_models = []

        return self.save_connection(
            organization_id=organization_id,
            provider=ProviderType.OPENAI_COMPATIBLE,
            credentials={"api_key": stored_key},
            config={"base_url": resolved_base_url, "models": resolved_models},
        )
