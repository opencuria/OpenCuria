"""Provider, skill, ancestry and engine validation before run admission."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from apps.runners.services.workspace_admission import WorkspaceAdmissionService
from apps.skills.services import SkillService
from common.exceptions import AuthenticationError

from .models import (
    HarnessSession,
    HarnessSessionStatus,
)
from .providers.model_ref import parse_model_ref
from .repositories import HarnessSessionRepository
from .run_settings_snapshot import RunSettingsSnapshot
from .session_access_service import SessionAccessService
from .session_settings_service import SessionSettingsService

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


FRONTEND_EVENT_STATUS = "harness.session_status"
FRONTEND_EVENT_STEER_QUEUE = "harness.steer_queue"


@dataclass(frozen=True)
class RunPreparationPorts:
    """Authorization, engine readiness and immutable provider/skill selection."""

    accessor_factory: Callable[..., Any] | None
    available_session_skill_ids: Callable[..., Any]
    capture_provider_account_ids: Callable[..., Any]
    forget_run: Callable[..., Any]
    provider_turn_context: Callable[..., Any]
    remember_run: Callable[..., Any]
    reconcile_recovered_run: Callable[[uuid.UUID | str, uuid.UUID | str], None]
    resolve_engine_auth: Callable[..., Any]
    run_service_for_engine: Callable[..., Any]
    session_settings: SessionSettingsService
    sessions: type[HarnessSessionRepository]
    skills: SkillService
    user_abort_requested: Callable[..., Any]
    validate_provider_for_run: Callable[..., Any]
    workspace_admission: WorkspaceAdmissionService


@dataclass(frozen=True)
class PreparedRun:
    """Validated inputs for one transactional admission."""

    session: HarnessSession
    prompt: str
    org_id: uuid.UUID
    workspace_id: str
    run_user_id: int | None
    scheduled: bool
    account_ids: dict[str, str]
    max_depth: int
    depth: int
    edit_message_id: uuid.UUID | None
    key: str
    engine_id: str
    inherited_context: dict[str, Any]
    skill_bodies: list[str]
    edit_model: str
    edit_effort: str
    resolved_model: str
    resolved_provider: str


class RunPreparation:
    """Resolve and validate a run before reserving or persisting its turn."""

    def __init__(self, ports: RunPreparationPorts) -> None:
        self.ports = ports

    async def prepare(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        organization_id: uuid.UUID | None = None,
        workspace_id: str = "",
        user_id: int | None = None,
        skill_ids: list[str] | None = None,
        scheduled: bool = False,
        account_ids: dict[str, str] | None = None,
        max_depth: int | None = None,
        _edit_message_id: uuid.UUID | None = None,
    ) -> PreparedRun:
        """Validate the request and capture its execution dependencies."""
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        captured_settings = RunSettingsSnapshot.capture(session)
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        run_user_id = user_id
        inherited_context = self.ports.provider_turn_context(session)
        if inherited_context.get("provider_turn_id"):
            snapshot_owner = inherited_context.get("initiated_by_id")
            if (
                run_user_id is not None
                and snapshot_owner is not None
                and str(run_user_id) != str(snapshot_owner)
            ):
                raise AuthenticationError(
                    "Subagent provider accounts inherit their owner's snapshot"
                )
            run_user_id = snapshot_owner
        SessionAccessService.require_external_owner(
            engine_id=engine_id,
            owner_id=getattr(session, "created_by_id", None),
            user_id=run_user_id,
        )
        if engine_id == "codex":
            user_id = run_user_id
        self.ports.remember_run(session)
        # Parent stop already covers this session: do not spawn another run.
        if self.ports.user_abort_requested(session):
            self.ports.forget_run(str(session.id))
            raise asyncio.CancelledError()
        await sync_to_async(
            self.ports.workspace_admission.ensure_interactions_available
        )(session.workspace_id)
        key = str(session.id)
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        recovery_accessor = None
        if engine_id != "native":
            run_service = self.ports.run_service_for_engine()
            open_attempt = await sync_to_async(
                run_service.repository.get_open_for_session, thread_sensitive=True
            )(session.id)
            if open_attempt is not None:
                if open_attempt.lease_id is not None:
                    if self.ports.accessor_factory is None:
                        await run_service.prepare_session(session.id, None)
                    else:
                        recovery_accessor = await self.ports.accessor_factory(
                            str(session.workspace_id)
                        )
                        await run_service.prepare_session(session.id, recovery_accessor)
                else:
                    # Let RunOwnership apply its persisted staleness window; do
                    # not create runner connectivity for a healthy unresolved run.
                    await run_service.prepare_session(session.id, None)
                self.ports.reconcile_recovered_run(
                    session.id, open_attempt.assistant_message_id
                )
        if str(session.status) != HarnessSessionStatus.IDLE:
            # Self-heal a stale busy flag: no live task exists, so a
            # previous run died without reaching its finally block
            # (e.g. question answered after the waiter was gone) and the
            # session would otherwise reject every new prompt with 409.
            log.warning(
                "harness_stale_busy_reset",
                session_id=key,
                status=str(session.status),
            )
            await sync_to_async(self.ports.sessions.mark_status)(
                session, HarnessSessionStatus.IDLE
            )
            session.status = HarnessSessionStatus.IDLE
        org_id = organization_id or session.organization_id
        if account_ids is None:
            inherited_ids = inherited_context.get("account_ids")
            account_ids = dict(inherited_ids) if inherited_ids is not None else None
        if account_ids is None and run_user_id is not None:
            account_ids = await sync_to_async(self.ports.capture_provider_account_ids)(
                org_id, run_user_id
            )
        if account_ids is None:
            account_ids = {}
        if engine_id == "claude":
            selected_claude = account_ids.get("claude-agent")
            if not selected_claude:
                raise AuthenticationError(
                    "No Claude Agent account is selected for this organization"
                )
            await sync_to_async(self.ports.resolve_engine_auth)(
                org_id, run_user_id, selected_claude
            )
        if skill_ids is None and session.skill_ids:
            available = await sync_to_async(self.ports.available_session_skill_ids)(
                session, user_id=user_id, organization_id=org_id
            )
            if available != list(session.skill_ids):
                if _edit_message_id is not None:
                    # Edits commit session options only once admitted.
                    session.skill_ids = available
                else:
                    session = await sync_to_async(self.ports.sessions.set_skill_ids)(
                        session, available
                    )
        captured_settings.retain_execution_settings(session)
        if engine_id == "claude":
            effective_skill_ids = (
                SkillService.normalize_ids(skill_ids)
                if skill_ids is not None
                else SkillService.normalize_ids(list(session.skill_ids or []))
            )
            from .engines.claude.runtime_state import validate_claude_skills

            bodies = (
                await sync_to_async(self.ports.skills.resolve_bodies)(
                    effective_skill_ids,
                    user_id=run_user_id,
                    organization_id=org_id,
                )
                if effective_skill_ids
                else []
            )
            validate_claude_skills(bodies)
        from .services import SubagentConfigService

        depth = await sync_to_async(self.ports.sessions.get_depth)(session.id)
        if max_depth is None:
            config = await sync_to_async(SubagentConfigService().get_or_default)(org_id)
            max_depth = config["max_depth"]
        if depth > max_depth:
            raise ValueError(f"depth {depth} exceeds max_depth {max_depth}")
        if skill_ids is not None:
            if user_id is None:
                raise AuthenticationError(
                    "A user is required to validate selected skills"
                )
            if _edit_message_id is not None:
                session.skill_ids = SkillService.normalize_ids(skill_ids)
            else:
                session = await sync_to_async(
                    self.ports.session_settings.update_skill_ids
                )(
                    session.id,
                    skill_ids,
                    user_id=user_id,
                    organization_id=org_id,
                )
        captured_settings.retain_execution_settings(session)
        skill_bodies: list[str] = []
        if session.skill_ids:
            if user_id is None:
                raise AuthenticationError(
                    "A user is required to validate selected skills"
                )
            skill_bodies = await sync_to_async(self.ports.skills.resolve_bodies)(
                list(session.skill_ids or []),
                user_id=user_id,
                organization_id=org_id,
            )
        # Provider validation may fill per-turn defaults in memory. Do not
        # turn Auto selections into persisted edit overrides.
        edit_model, edit_effort = session.model, session.reasoning_effort
        resolved_model = await sync_to_async(self.ports.validate_provider_for_run)(
            org_id,
            session,
            user_id=run_user_id,
            account_ids=account_ids,
        )
        resolved_provider, _ = parse_model_ref(resolved_model)
        return PreparedRun(
            session=session,
            prompt=prompt,
            org_id=org_id,
            workspace_id=workspace_id,
            run_user_id=run_user_id,
            scheduled=scheduled,
            account_ids=account_ids,
            max_depth=max_depth,
            depth=depth,
            edit_message_id=_edit_message_id,
            key=key,
            engine_id=engine_id,
            inherited_context=inherited_context,
            skill_bodies=skill_bodies,
            edit_model=edit_model,
            edit_effort=edit_effort,
            resolved_model=resolved_model,
            resolved_provider=resolved_provider,
        )
