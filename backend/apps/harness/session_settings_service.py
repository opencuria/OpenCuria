"""Saved session settings and engine-specific validation, independent of live runs.

REST, MCP and run admission share this service. Repositories own persistence,
skills own visibility, and conversation events publish root feed invalidations.
"""

from __future__ import annotations

import uuid

from apps.skills.services import SkillService
from common.exceptions import NotFoundError

from .conversation_events import ConversationEvents
from .engines.catalog import normalize_claude_effort, normalize_claude_model
from .engines.codex.catalog import codex_provider
from .engines.service import EngineCatalogService
from .models import HarnessSession
from .providers.models_catalog import normalize_reasoning_effort
from .repositories import HarnessSessionRepository


class SessionSettingsService:
    """Validate and persist session settings without constructing agent execution."""

    def __init__(
        self,
        *,
        sessions: type[HarnessSessionRepository] | None = None,
        skills: SkillService | None = None,
        events: ConversationEvents | None = None,
    ) -> None:
        """Inject persistence, skill visibility and conversation notifications."""
        self.sessions = sessions or HarnessSessionRepository
        self.skills = skills or SkillService()
        self.events = events or ConversationEvents()

    def _editable_session(self, session_id: uuid.UUID) -> HarnessSession:
        """Load current persisted state and enforce the workspace interaction fence."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        self.sessions.ensure_interactions_available(session.workspace_id)
        return session

    def set_mode(self, session_id: uuid.UUID, mode: str) -> HarnessSession:
        """Persist a mode change (plan|build) and align primary agent_name."""
        normalized = (mode or "").strip().lower()
        if normalized not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        session = self._editable_session(session_id)
        updated = self.sessions.set_mode(session, normalized)
        if updated.parent_id is None:
            self.events.emit_sync(updated.workspace_id)
        return updated

    def validate_codex_config(
        self,
        session: HarnessSession,
        model: str | None,
        effort: str | None,
        *,
        user_id: int | None = None,
        account_ids: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """Validate the candidate without mutating model/provider binding."""
        provider = codex_provider(session.model, session.engine_state)
        return EngineCatalogService().validate_codex_config(
            session.organization_id,
            user_id or getattr(session, "created_by_id", None),
            model,
            effort,
            provider=provider,
            account_ids=account_ids,
        )

    def set_model(self, session_id: uuid.UUID, model: str) -> HarnessSession:
        """Persist a model override for subsequent runs."""
        session = self._editable_session(session_id)
        value = (model or "").strip()
        if getattr(session, "harness_id", "native") == "claude":
            value = normalize_claude_model(value)
        if getattr(session, "harness_id", "native") == "codex":
            value, default_effort = self.validate_codex_config(session, value, "")
            # Model changes within a provider remain possible when the old
            # model supported an effort (e.g. ultra) that the new one does not.
            try:
                self.validate_codex_config(session, value, session.reasoning_effort)
            except ValueError:
                session = self.sessions.set_reasoning_effort(session, default_effort)
        return self.sessions.set_model(session, value)

    def set_reasoning_effort(
        self, session_id: uuid.UUID, reasoning_effort: str
    ) -> HarnessSession:
        """Persist a reasoning-effort override for subsequent runs."""
        session = self._editable_session(session_id)
        if getattr(session, "harness_id", "native") == "claude":
            effort = normalize_claude_effort(reasoning_effort or None)
        elif getattr(session, "harness_id", "native") == "codex":
            _, effort = self.validate_codex_config(
                session, session.model, reasoning_effort
            )
        else:
            effort = normalize_reasoning_effort(reasoning_effort)
        return self.sessions.set_reasoning_effort(session, effort)

    def update_title(self, session_id: uuid.UUID, title: str) -> HarnessSession:
        """Rename a session (title only)."""
        session = self._editable_session(session_id)
        normalized = (title or "").strip()
        if not normalized:
            raise ValueError("title must not be empty")
        updated = self.sessions.set_title(session, normalized)
        if updated.parent_id is None:
            self.events.emit_sync(updated.workspace_id)
        return updated

    def update_skill_ids(
        self,
        session_id: uuid.UUID,
        skill_ids: list[str],
        *,
        user_id: int,
        organization_id: uuid.UUID,
    ) -> HarnessSession:
        """Persist skill selection after validating visibility."""
        session = self._editable_session(session_id)
        normalized = SkillService.normalize_ids(skill_ids)
        if normalized:
            self.skills.resolve_bodies(
                normalized,
                user_id=user_id,
                organization_id=organization_id,
            )
        return self.sessions.set_skill_ids(session, normalized)
