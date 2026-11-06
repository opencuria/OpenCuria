"""Engine run settings that are stored now and launched later without the owner.

Scheduled tasks persist an engine, model and reasoning effort and start the run
unattended. This service owns the engine-specific normalization and checks that
the owner can run the selection, so consumers never import engine internals.
Stored settings never bind provider accounts or carry secrets.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from common.exceptions import AuthenticationError, NotFoundError

from .engines.catalog import ENGINE_IDS, normalize_claude_effort, normalize_claude_model
from .engines.connections import EngineConnectionService
from .engines.service import EngineCatalogService
from .harness_service import HarnessService, get_harness_service
from .models import HarnessSession
from .providers.models_catalog import normalize_reasoning_effort


class RunSettingsService:
    """Normalize stored engine settings and check the owner's run readiness."""

    def __init__(self, harness: Callable[[], HarnessService] | None = None) -> None:
        """Resolve the harness lazily; it checks native provider readiness."""
        self._harness = harness or get_harness_service

    @staticmethod
    def normalize_harness_id(value: Any) -> str:
        """Return the engine id for *value*.

        Raises:
            ValueError: *value* is not ``native``, ``claude`` or ``codex``.
        """
        if not isinstance(value, str):
            raise ValueError("harness_id must be native, claude or codex")
        harness_id = value.strip().lower()
        if harness_id not in ENGINE_IDS:
            raise ValueError("harness_id must be native, claude or codex")
        return harness_id

    def validate_scheduled(
        self,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
        workspace_id: uuid.UUID,
        mode: str,
        harness_id: str,
        model: str,
        reasoning_effort: str,
        skill_ids: list[str],
    ) -> tuple[str, str]:
        """Return the model and effort to store once the owner can run them.

        Raises:
            ValueError: The engine, model or effort is invalid, or the owner
                lacks the engine authorization a scheduled run needs.
            NotFoundError: No native provider is connected for the model.
        """
        harness_id = self.normalize_harness_id(harness_id)
        raw_model = model.strip()
        raw_effort = reasoning_effort.strip()

        if harness_id == "native":
            effort = normalize_reasoning_effort(raw_effort)
            validation_session = HarnessSession(
                workspace_id=workspace_id,
                organization_id=organization_id,
                # Provider accounts are personal: validate the owner's selection.
                created_by_id=owner_id,
                mode=mode,
                agent_name=mode,
                model=raw_model,
                reasoning_effort=effort,
                skill_ids=skill_ids,
            )
            self._harness().validate_provider_for_run(
                organization_id, validation_session
            )
            return raw_model, effort

        if harness_id == "codex":
            try:
                normalized_model, effort = EngineCatalogService().validate_codex_config(
                    organization_id, owner_id, raw_model, raw_effort
                )
            except (AuthenticationError, NotFoundError) as exc:
                raise ValueError(
                    "Connect your selected ChatGPT or OpenRouter account "
                    "before scheduling Codex tasks"
                ) from exc
            return normalized_model, effort

        normalized_model = normalize_claude_model(raw_model or None)
        effort = normalize_claude_effort(raw_effort or None)
        # Resolve only to verify the owner's personal authorization. The token
        # is never copied into a task or occurrence snapshot; the engine resolves
        # the connection again for each actual run.
        try:
            EngineConnectionService().resolve(organization_id, owner_id)
        except NotFoundError as exc:
            raise ValueError(
                "Connect your personal Claude authorization before scheduling "
                "Claude tasks"
            ) from exc
        return normalized_model, effort
