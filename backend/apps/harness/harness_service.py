"""Application facade for saved conversations and composed run services.

Preparation, admission, execution, provider scopes, subagents and cancellation
have independent collaborators. RunRegistry is the single local ownership model;
repositories remain the authority for cross-process admission and recovery.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING, Any

import anyio
import structlog
from asgiref.sync import ThreadSensitiveContext, sync_to_async

from apps.runners.services import get_runner_service
from apps.runners.services.workspace_admission import WorkspaceAdmissionService
from apps.runners.services.workspace_plugin_runtime import (
    WorkspacePluginRuntimeService,
)
from apps.skills.services import SkillService
from common import realtime
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .agents.definitions import get_agent
from .conversation_events import ConversationEvents
from .conversation_service import ConversationService
from .interaction_service import InteractionService
from .models import (
    HarnessMessage,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
)
from .permissions.service import PermissionService
from .provider_history_service import ProviderHistoryService
from .providers.base import LLMMessage, ProviderAdapter
from .providers.model_ref import namespaced_model_id
from .providers.models_catalog import normalize_reasoning_effort
from .redaction import redact_text
from .repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from .run_events import publish_assistant_run_finished, publish_session_activity
from .run_timeline import (
    FRONTEND_EVENT_PART,
    FRONTEND_EVENT_SUBTASK_STARTED,
    RunTimeline,
    tail_remainder,
)
from .runner import HarnessRunner
from .session_access_service import SessionAccessService
from .session_metadata_service import SessionMetadataService, title_from_prompt
from .session_settings_service import SessionSettingsService
from .steering import NO_ACTIVE_RUN_CODE, SteerEntry, SteeringChannel
from .todos.repositories import TodoRepository
from .tools.subagents import TaskArgs

if TYPE_CHECKING:
    from .command_service import RemoteHarnessService

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


FRONTEND_EVENT_STATUS = "harness.session_status"
FRONTEND_EVENT_STEER_QUEUE = "harness.steer_queue"


class HarnessService:
    """Persistent session lifecycle + run orchestration for the harness."""

    def __init__(
        self,
        *,
        sessions: type[HarnessSessionRepository] | None = None,
        messages: type[HarnessMessageRepository] | None = None,
        parts: type[HarnessPartRepository] | None = None,
        todos: type[TodoRepository] | None = None,
        permissions: PermissionService | None = None,
        skills: SkillService | None = None,
        workspace_admission: WorkspaceAdmissionService | None = None,
        plugin_runtime: WorkspacePluginRuntimeService | None = None,
        emit: EmitFn | None = None,
        runner_factory: Callable[..., HarnessRunner] | None = None,
        provider_factory: Callable[[uuid.UUID], ProviderAdapter] | None = None,
        accessor_factory: Callable[[str], Any] | None = None,
        process_cleanup: Callable[..., Any] | None = None,
        engine_registry: Any | None = None,
        engine_connections: Any | None = None,
        run_service: Any | None = None,
    ) -> None:
        """Create the service with injectable seams (tests fake them).

        ``process_cleanup`` stops session-scoped temp processes when a
        run finishes (called with ``workspace_id``, ``session_id`` and
        ``reason``); production wires the runners ``RunnerService``,
        tests inject a fake.
        """
        self.sessions = sessions or HarnessSessionRepository
        self.messages = messages or HarnessMessageRepository
        self.parts = parts or HarnessPartRepository
        self.history = ProviderHistoryService(messages=self.messages, parts=self.parts)
        self.todos = todos or TodoRepository
        self.permissions = permissions or PermissionService()
        self.skills = skills or SkillService()
        self.workspace_admission = workspace_admission or WorkspaceAdmissionService()
        self.plugin_runtime = plugin_runtime or WorkspacePluginRuntimeService()
        self._emit = emit
        self.conversation_events = ConversationEvents(emit=self._emit_frontend)
        self.conversations = ConversationService(
            sessions=self.sessions,
            messages=self.messages,
            parts=self.parts,
            todos=self.todos,
            events=self.conversation_events,
            workspace_admission=self.workspace_admission,
        )
        self.session_settings = SessionSettingsService(
            sessions=self.sessions,
            skills=self.skills,
            events=self.conversation_events,
            workspace_admission=self.workspace_admission,
        )
        self.interactions = InteractionService(
            sessions=self.sessions,
            workspace_admission=self.workspace_admission,
            permissions=self.permissions,
            emit=self._emit_frontend,
            is_running=self.is_running,
        )
        self.timeline = RunTimeline(
            messages=self.messages,
            parts=self.parts,
            run_context=lambda session_id: self.run_registry.get_context(session_id),
            emit=self._emit_frontend,
        )
        self._runner_factory = runner_factory
        self._provider_factory = provider_factory
        self._accessor_factory = accessor_factory
        from .run_state import RunRegistry

        self.run_registry = RunRegistry()
        from .background_tasks import BackgroundTaskSupervisor

        self._background = BackgroundTaskSupervisor()
        self._process_cleanup = process_cleanup
        self._engine_registry = engine_registry
        self._engine_connections = engine_connections
        self._run_service = run_service
        self._provider_turns = None
        self._provider_turn_roots: dict[str, dict[str, Any]] = {}
        self.metadata = SessionMetadataService(
            sessions=self.sessions,
            messages=self.messages,
            provider_factory=provider_factory,
            emit=self._emit_frontend,
            conversations_changed=self.conversation_events.emit,
        )

    def _run_cancellation(self) -> Any:
        """Compose RunCancellation with the current application ports."""
        from .run_cancellation import RunCancellation, RunCancellationPorts

        return RunCancellation(
            RunCancellationPorts(
                close_steering=self._close_steering,
                emit_empty_steer_queue=self._emit_empty_steer_queue,
                ensure_external_attempt_reconciled=self._ensure_external_attempt_reconciled,
                run_service_for_engine=self._run_service_for_engine,
                spawn_background=self._spawn_background,
                conversations=self.conversations,
                interactions=self.interactions,
                run_registry=self.run_registry,
                sessions=self.sessions,
            )
        )

    def _provider_turn_lifecycle(self) -> Any:
        """Compose ProviderTurnLifecycle with the current application ports."""
        from .provider_turn_lifecycle import (
            ProviderTurnLifecycle,
            ProviderTurnLifecyclePorts,
        )

        return ProviderTurnLifecycle(
            ProviderTurnLifecyclePorts(
                cancel_owned_task=self._cancel_owned_task,
                descendant_keys=self._descendant_keys,
                owned_run_task=self._owned_run_task,
                provider_turn_roots=self._provider_turn_roots,
                provider_turn_service=self._provider_turn_service,
                interactions=self.interactions,
                run_registry=self.run_registry,
            )
        )

    def _subagent_coordinator(self) -> Any:
        """Compose SubagentCoordinator with the current application ports."""
        from .subagent_coordinator import SubagentCoordinator, SubagentCoordinatorPorts

        return SubagentCoordinator(
            SubagentCoordinatorPorts(
                agent_configs_map=self._agent_configs_map,
                catalog_efforts_for_model=self._catalog_efforts_for_model,
                on_runner_event=self._on_runner_event,
                parent_stop_in_progress=self._parent_stop_in_progress,
                provider_turn_context=self._provider_turn_context,
                create_session=self.create_session,
                is_running=self.is_running,
                messages=self.messages,
                run_registry=self.run_registry,
                session_settings=self.session_settings,
                sessions=self.sessions,
                start_run=self.start_run,
            )
        )

    def _engine_registry_for_run(self) -> Any:
        """Return the configured factory or the default native/Claude adapter."""
        if self._engine_registry is None:
            from .engines.service_runtime import default_engine_registry

            self._engine_registry = default_engine_registry()
        return self._engine_registry

    def _engine_connection_service(self) -> Any:
        if self._engine_connections is None:
            from .engines.connections import EngineConnectionService

            self._engine_connections = EngineConnectionService()
        return self._engine_connections

    def _run_service_for_engine(self) -> Any:
        if self._run_service is None:
            from .engines.runs import HarnessRunService

            self._run_service = HarnessRunService()
        return self._run_service

    def _resolve_engine_auth(
        self,
        organization_id: uuid.UUID,
        user_id: int,
        connection_id: uuid.UUID | str | None,
    ) -> Any:
        """Resolve owned personal authorization without persisting credentials."""
        return self._engine_connection_service().resolve(
            organization_id, user_id, connection_id
        )

    @property
    def _provider_turn_service(self):
        """Lazily construct and cache the per-answer domain service."""
        if self._provider_turns is None:
            from .provider_turns import ProviderTurnService

            self._provider_turns = ProviderTurnService()
        return self._provider_turns

    def _capture_provider_account_ids(
        self, organization_id: uuid.UUID, user_id: int
    ) -> dict[str, str]:
        """Capture active selections for the authenticated initiating user."""
        return self._provider_turn_service.capture_account_ids(organization_id, user_id)

    def _provider_turn_context(self, session: HarnessSession) -> dict[str, Any]:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return self._provider_turn_lifecycle()._provider_turn_context(session)

    async def _register_provider_use(
        self, turn_id: uuid.UUID | str | None, account_id: uuid.UUID | str | None
    ) -> None:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return await self._provider_turn_lifecycle()._register_provider_use(
            turn_id, account_id
        )

    async def _provider_turn_revocation_message(self, session: HarnessSession) -> str:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return await self._provider_turn_lifecycle()._provider_turn_revocation_message(
            session
        )

    async def _finish_provider_turn(self, context: dict[str, Any]) -> None:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return await self._provider_turn_lifecycle()._finish_provider_turn(context)

    async def _finish_provider_hold(
        self, turn_id: uuid.UUID | str, holder: str
    ) -> None:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return await self._provider_turn_lifecycle()._finish_provider_hold(
            turn_id, holder
        )

    async def _maybe_finish_provider_monitor(self, root: dict[str, Any]) -> None:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return await self._provider_turn_lifecycle()._maybe_finish_provider_monitor(
            root
        )

    def _watch_provider_followup(
        self, context: dict[str, Any], task: asyncio.Task, holder: str
    ) -> None:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return self._provider_turn_lifecycle()._watch_provider_followup(
            context, task, holder
        )

    async def _monitor_provider_turn(
        self,
        *,
        session: HarnessSession,
        context: dict[str, Any],
        event: asyncio.Event,
        watched_accounts: set[uuid.UUID],
    ) -> None:
        """Delegate to the ProviderTurnLifecycle domain boundary."""
        return await self._provider_turn_lifecycle()._monitor_provider_turn(
            session=session,
            context=context,
            event=event,
            watched_accounts=watched_accounts,
        )

    # -- session lifecycle ------------------------------------------------

    def create_session(
        self,
        *,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        prompt: str,
        agent_name: str = "build",
        mode: str = "build",
        model: str = "",
        reasoning_effort: str = "",
        title: str = "",
        parent_id: uuid.UUID | None = None,
        skill_ids: list[str] | None = None,
        user_id: int | None = None,
        harness_id: str = "native",
        connection_id: uuid.UUID | str | None = None,
    ) -> HarnessSession:
        """Create a session row (prompt persisted on run start)."""
        self.workspace_admission.ensure_interactions_available(workspace_id)
        if parent_id is not None:
            parent_session = self.conversations.get_session(parent_id)
            harness_id = getattr(parent_session, "harness_id", "native") or "native"
            if (
                parent_session.workspace_id != workspace_id
                or parent_session.organization_id != organization_id
            ):
                raise ValueError(
                    "Child sessions must share their parent workspace and organization"
                )
            if harness_id in {"claude", "codex"}:
                created_by_id = getattr(parent_session, "created_by_id", None)
                if user_id is not None and str(user_id) != str(created_by_id):
                    raise AuthenticationError(
                        "External child sessions inherit their owner's scope"
                    )
                if created_by_id is None:
                    raise AuthenticationError(
                        "External child sessions require an owner"
                    )
                user_id = created_by_id
                connection_id = None
                if harness_id == "codex":
                    model = model or parent_session.model
                    if skill_ids is None:
                        skill_ids = list(parent_session.skill_ids or [])
        from .engines.catalog import (
            DEFAULT_CLAUDE_EFFORT,
            DEFAULT_CLAUDE_MODEL,
            ENGINE_IDS,
            normalize_claude_effort,
            normalize_claude_model,
        )

        engine_id = (harness_id or "native").strip().lower()
        if engine_id not in ENGINE_IDS:
            raise ValueError(f"Unknown harness engine {harness_id!r}")
        normalized_mode = (mode or "build").strip().lower()
        if normalized_mode not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        requested = (agent_name or "").strip().lower()
        if engine_id in {"claude", "codex"} and requested in {
            "title",
            "summary",
            "compaction",
        }:
            raise ValueError(f"{engine_id} engine cannot use hidden harness agents")
        # Root sessions: agent_name follows mode (plan|build). Child sessions
        # honor subagent agent_name while keeping parent plan|build mode.
        resolved_agent = normalized_mode
        if parent_id is not None and requested:
            definition = get_agent(requested)
            if definition.mode == "hidden":
                raise ValueError(
                    f"Agent '{requested}' cannot be used as a subagent child"
                )
            if definition.mode == "subagent":
                resolved_agent = requested
        get_agent(resolved_agent)  # raises KeyError for unknown agents
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        normalized_skills = SkillService.normalize_ids(skill_ids)
        if normalized_skills and user_id is not None:
            self.skills.resolve_bodies(
                normalized_skills,
                user_id=user_id,
                organization_id=organization_id,
            )
        codex_provider_id = None
        if engine_id == "codex":
            from .engines.codex.catalog import codex_provider
            from .engines.service import EngineCatalogService

            if user_id is None:
                raise AuthenticationError("A user is required for Codex authorization")
            parent_provider = (
                codex_provider(parent_session.model, parent_session.engine_state)
                if parent_id is not None
                else None
            )
            inherited = self._provider_turn_context(parent_session) if parent_id else {}
            model, reasoning_effort = EngineCatalogService().validate_codex_config(
                organization_id,
                user_id,
                model,
                reasoning_effort,
                provider=parent_provider,
                account_ids=inherited.get("account_ids"),
            )
            codex_provider_id = codex_provider(model)
        normalized_effort = (
            normalize_claude_effort(reasoning_effort or DEFAULT_CLAUDE_EFFORT)
            if engine_id == "claude"
            else reasoning_effort
            if engine_id == "codex"
            else normalize_reasoning_effort(reasoning_effort)
        )
        normalized_model = (
            normalize_claude_model(model or DEFAULT_CLAUDE_MODEL)
            if engine_id == "claude"
            else (model or "").strip()
        )
        if engine_id == "native" and not normalized_effort:
            # Fall back to the AgentConfig fixed effort of the resolved agent
            # (inherit-mode leaves "" so the caller decides).
            try:
                defaults = HarnessService._agent_defaults(
                    organization_id, resolved_agent
                )
                if not defaults["inherit_model"]:
                    normalized_effort = normalize_reasoning_effort(defaults["effort"])
            except Exception:
                normalized_effort = ""
        session_kwargs: dict[str, Any] = {
            "workspace_id": workspace_id,
            "organization_id": organization_id,
            "title": title or title_from_prompt(prompt),
            "mode": normalized_mode,
            "agent_name": resolved_agent,
            "model": normalized_model,
            "reasoning_effort": normalized_effort,
            "parent_id": parent_id,
            "skill_ids": normalized_skills,
        }
        if engine_id != "native":
            if user_id is None:
                raise AuthenticationError("An engine provider account is required")
            if engine_id == "codex":
                if connection_id is not None:
                    raise ValueError(
                        "Codex uses the selected provider account, not connection_id"
                    )
            else:
                from .provider_accounts import ProviderAccountService

                accounts = ProviderAccountService()
                if connection_id is not None:
                    account = accounts.get_account(
                        organization_id, user_id, connection_id
                    )
                    if account.provider != "claude-agent":
                        raise NotFoundError("ProviderAccount", str(connection_id))
                else:
                    account = accounts.selected_accounts(organization_id, user_id).get(
                        "claude-agent"
                    )
                if account is None:
                    raise NotFoundError("ProviderAccount", str(user_id))
            session_kwargs.update(
                harness_id=engine_id,
                created_by_id=user_id,
                external_session_id="",
                engine_state={"provider_id": codex_provider_id}
                if codex_provider_id
                else {},
            )
        if user_id is not None:
            session_kwargs["created_by_id"] = user_id
        session = self.sessions.create(**session_kwargs)
        log.info("harness_session_created", session_id=str(session.id))
        if session.parent_id is None:
            self.conversation_events.emit_sync(session.workspace_id)
        return session

    @staticmethod
    def _codex_provider_for_session(session) -> str:
        """Read the immutable non-secret provider binding."""
        from .engines.codex.catalog import codex_provider

        return codex_provider(session.model, session.engine_state)

    def _available_session_skill_ids(
        self,
        session: HarnessSession,
        *,
        user_id: int | None,
        organization_id: uuid.UUID,
    ) -> list[str]:
        """Return the session's persisted skills that still resolve.

        Runs inherit the selection, so skills deleted since must not wedge
        every later prompt of the chat.
        """
        stored = SkillService.normalize_ids(list(session.skill_ids or []))
        if not stored or user_id is None:
            return stored
        return self.skills.filter_available_ids(
            stored, user_id=user_id, organization_id=organization_id
        )

    async def delete_session(self, session_id: uuid.UUID) -> None:
        """Delete a session only after external process ownership is settled."""
        session = await sync_to_async(self.conversations.get_session)(session_id)
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        if (
            session.parent_id is not None
            and getattr(session, "harness_id", "native") == "claude"
        ):
            parent = await sync_to_async(self.sessions.get_by_id)(session.parent_id)
            if (
                parent is not None
                and getattr(parent, "harness_id", "native") == "claude"
            ):
                if self.is_running(parent.id):
                    await self.abort_run(parent.id)
                await self._ensure_external_attempt_reconciled(parent, force=True)
        if self.is_running(session.id):
            await self.abort_run(session.id)
        # A closing attempt with unconfirmed runner cleanup must survive deletion.
        await self._ensure_external_attempt_reconciled(session, force=True)
        await sync_to_async(self.sessions.delete)(session)
        self.run_registry.clear_abort(str(session.id))
        self._forget_run(str(session.id))
        if session.parent_id is None:
            await self.conversation_events.emit(session.workspace_id)

    async def _ensure_external_attempt_reconciled(
        self, session: HarnessSession, *, force: bool = False
    ) -> bool:
        """Reject mutations that could erase a durable external process owner."""
        if (getattr(session, "harness_id", "native") or "native") == "native":
            return True
        run_service = self._run_service_for_engine()
        attempt = await sync_to_async(
            run_service.repository.get_open_for_session, thread_sensitive=True
        )(session.id)
        if attempt is None:
            return True
        accessor = None
        if attempt.lease_id is not None:
            if self._accessor_factory is None:
                raise ConflictError("Previous engine process cleanup is pending")
            accessor = self._accessor_factory(str(session.workspace_id))
            if asyncio.iscoroutine(accessor):
                accessor = await accessor
        if force or attempt.status == HarnessRunStatus.CLOSING:
            await run_service.prepare_session(session.id, accessor)
            self.reconcile_recovered_run(session.id, attempt.assistant_message_id)
            return True
        raise ConflictError("Harness session already has an active run")

    async def edit_user_message(
        self,
        session_id: uuid.UUID,
        *,
        message_id: uuid.UUID,
        prompt: str,
        mode: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        skill_ids: list[str] | None = None,
        organization_id: uuid.UUID | None = None,
        workspace_id: str = "",
        user_id: int | None = None,
    ) -> HarnessMessage:
        """Edit a user message, drop the suffix, and rerun with *prompt*."""
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        session = await sync_to_async(self.conversations.get_session)(session_id)
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        SessionAccessService.require_user_promptable(session.parent_id)
        if self.is_running(session.id):
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        owner_id = getattr(session, "created_by_id", None)
        SessionAccessService.require_external_owner(
            engine_id=getattr(session, "harness_id", "native"),
            owner_id=owner_id,
            user_id=user_id,
            message="Only the session owner may edit an external conversation",
        )
        await self._ensure_external_attempt_reconciled(session, force=True)
        normalized_mode = None
        normalized_model = None
        normalized_effort = None
        if mode is not None and mode.strip():
            normalized_mode = mode.strip().lower()
            if normalized_mode not in ("plan", "build"):
                raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        if model is not None and model.strip():
            normalized_model = model.strip()
            if getattr(session, "harness_id", "native") == "claude":
                from .engines.catalog import normalize_claude_model

                normalized_model = normalize_claude_model(normalized_model)
        if getattr(session, "harness_id", "native") == "codex":
            normalized_model, normalized_effort = await sync_to_async(
                self.session_settings.validate_codex_config
            )(
                session,
                normalized_model or session.model,
                reasoning_effort
                if reasoning_effort is not None
                else session.reasoning_effort,
            )
        if (
            getattr(session, "harness_id", "native") != "codex"
            and reasoning_effort is not None
            and reasoning_effort.strip()
        ):
            if getattr(session, "harness_id", "native") == "claude":
                from .engines.catalog import normalize_claude_effort

                normalized_effort = normalize_claude_effort(reasoning_effort)
            else:
                normalized_effort = normalize_reasoning_effort(reasoning_effort)
        if getattr(session, "harness_id", "native") == "claude":
            await sync_to_async(self._resolve_engine_auth)(
                session.organization_id,
                owner_id,
                None,
            )
        effective_skill_ids = (
            SkillService.normalize_ids(skill_ids)
            if skill_ids is not None
            else await sync_to_async(self._available_session_skill_ids)(
                session,
                user_id=user_id,
                organization_id=organization_id or session.organization_id,
            )
        )
        claude_session = getattr(session, "harness_id", "native") == "claude"
        if (
            claude_session
            or getattr(session, "harness_id", "native") == "codex"
            or skill_ids is not None
        ):
            if user_id is None and (effective_skill_ids or skill_ids is not None):
                raise AuthenticationError(
                    "A user is required to validate selected skills"
                )
            effective_skill_bodies: list[str] = []
            if user_id is not None:
                resolve_bodies = sync_to_async(self.skills.resolve_bodies)
                effective_skill_bodies = await resolve_bodies(
                    effective_skill_ids,
                    user_id=user_id,
                    organization_id=organization_id or session.organization_id,
                )
            if getattr(session, "harness_id", "native") == "claude":
                from .engines.claude.runtime_state import validate_claude_skills

                validate_claude_skills(effective_skill_bodies)
        stored = await sync_to_async(self.messages.list_for_session)(session.id)
        target = next((row for row in stored if row.id == message_id), None)
        if target is None:
            raise NotFoundError("HarnessMessage", str(message_id))
        if target.role != "user":
            raise ValueError("Only user messages can be edited")
        # Validate the candidate options without changing the persisted session.
        # start_run owns the admission fence and commits the edit only after all
        # prerequisites (including provider-turn admission) have succeeded.
        if normalized_mode is not None:
            session.mode = normalized_mode
            session.agent_name = normalized_mode
        if normalized_model is not None:
            session.model = normalized_model
        if normalized_effort is not None:
            session.reasoning_effort = normalized_effort
        return await self.start_run(
            session,
            prompt.strip(),
            organization_id=organization_id or session.organization_id,
            workspace_id=workspace_id or str(session.workspace_id),
            user_id=user_id,
            skill_ids=skill_ids,
            _edit_message_id=message_id,
        )

    async def _commit_message_edit(
        self,
        session: HarnessSession,
        suffix_ids: list[uuid.UUID],
        *,
        model: str,
        reasoning_effort: str,
    ) -> None:
        """Replace an admitted edit's suffix before its runner can start."""
        child_ids = await self._collect_suffix_child_ids(suffix_ids)
        for child_id in child_ids:
            aborted_child = await self.abort_run(child_id)
            await self._ensure_external_attempt_reconciled(aborted_child, force=True)
        permission_ids, question_ids = await sync_to_async(
            self.sessions.commit_message_edit
        )(
            session,
            suffix_ids=suffix_ids,
            child_ids=child_ids,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        self.interactions.cancel_waiters(permission_ids, question_ids)
        if getattr(session, "harness_id", "native") != "native":
            session.external_session_id = ""
            session.engine_state = (
                {"provider_id": self._codex_provider_for_session(session)}
                if session.harness_id == "codex"
                else {}
            )

    async def _collect_suffix_child_ids(
        self, suffix_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Return child session ids referenced by subtask parts in suffix."""
        parts = await sync_to_async(self.parts.list_for_messages)(suffix_ids)
        child_ids: list[uuid.UUID] = []
        seen: set[str] = set()
        for part in parts:
            if part.type != "subtask":
                continue
            raw = str((part.meta or {}).get("child_session_id", "") or "").strip()
            if not raw or raw in seen:
                continue
            seen.add(raw)
            try:
                child_ids.append(uuid.UUID(raw))
            except ValueError:
                continue
        return child_ids

    @staticmethod
    def _agent_configs_map(organization_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        """Return AgentConfig rows keyed by agent name (best-effort)."""
        try:
            from .services import AgentConfigService

            return {
                str(row.get("agent", "")): row
                for row in AgentConfigService().list_configs(organization_id)
            }
        except Exception:
            return {}

    @staticmethod
    def _agent_defaults(organization_id: uuid.UUID, agent_name: str) -> dict[str, Any]:
        """Return AgentConfig defaults for *agent_name* (best-effort).

        Keys: ``model``, ``effort``, ``inherit_model``, ``effort_strategy``.
        Missing rows fall back to ``""``/inherit for subagents.
        """
        agent = (agent_name or "").strip().lower() or "build"
        try:
            from .services import AgentConfigService

            for row in AgentConfigService().list_configs(organization_id):
                if str(row.get("agent", "")) == agent:
                    return {
                        "model": str(row.get("model") or "").strip(),
                        "effort": normalize_reasoning_effort(
                            str(row.get("effort") or "")
                        ),
                        "inherit_model": bool(row.get("inherit_model", False)),
                        "effort_strategy": str(
                            row.get("effort_strategy") or "fixed"
                        ).strip()
                        or "fixed",
                    }
        except Exception:
            pass
        return {
            "model": "",
            "effort": "",
            "inherit_model": True,
            "effort_strategy": "inherit",
        }

    def validate_provider_for_run(
        self,
        organization_id: uuid.UUID,
        session: HarnessSession,
        *,
        user_id: int | None = None,
        account_ids: dict[str, str] | None = None,
    ) -> str:
        """Ensure provider config and model are present before starting a run.

        Returns the resolved model id for this turn. An empty session model
        (Auto) is filled from the agent config.

        Raises:
            NotFoundError: When no provider connection exists for the model.
            ValueError: When API key or model is missing.
        """
        session_model = (session.model or "").strip()
        if getattr(session, "harness_id", "native") == "codex":
            session.model, session.reasoning_effort = (
                self.session_settings.validate_codex_config(
                    session,
                    session_model,
                    session.reasoning_effort,
                    user_id=user_id,
                    account_ids=account_ids,
                )
            )
            return session.model
        if getattr(session, "harness_id", "native") == "claude":
            from .engines.catalog import (
                DEFAULT_CLAUDE_MODEL,
                normalize_claude_effort,
                normalize_claude_model,
            )

            session.model = normalize_claude_model(
                session_model or DEFAULT_CLAUDE_MODEL
            )
            session.reasoning_effort = normalize_claude_effort(
                session.reasoning_effort or "high"
            )
            return session.model
        if self._provider_factory is not None:
            return session_model
        from .services import ProviderConfigService

        config_service = ProviderConfigService()
        agent_name = (session.agent_name or session.mode or "build").strip().lower()
        agent_defaults = self._agent_defaults(organization_id, agent_name)
        agent_model = "" if agent_defaults["inherit_model"] else agent_defaults["model"]
        model = session_model or agent_model
        if not model:
            raise NotFoundError("ProviderConnection", str(organization_id))
        account_user_id = user_id or getattr(session, "created_by_id", None)
        config_service.provider_connected_for_model(
            organization_id, model, user_id=account_user_id, account_ids=account_ids
        )
        if not (session.reasoning_effort or "").strip():
            agent_effort = (
                "" if agent_defaults["inherit_model"] else agent_defaults["effort"]
            )
            session.reasoning_effort = normalize_reasoning_effort(agent_effort)
        return model

    # -- run orchestration --------------------------------------------------

    def is_running(self, session_id: uuid.UUID) -> bool:
        """Return True when a run task is active for *session_id*."""
        key = str(session_id)
        record = self.run_registry.get(key)
        return record is not None and record.is_running

    def reserve_run(self, session: HarnessSession, *, scheduled: bool = False) -> bool:
        """Mark *session* busy under the workspace admission locks and fence (sync).

        Manual roots and children stay concurrent; a scheduled launch requires
        an idle running workspace without another busy root.
        """
        return self.workspace_admission.admit_run(
            session.workspace_id,
            scheduled=scheduled,
            reserve=partial(self.sessions.reserve_run, session.id, scheduled=scheduled),
        )

    async def start_run(
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
    ) -> HarnessMessage:
        """Compose the RunAdmission boundary with its explicit ports."""
        if self._background.closing:
            raise ConflictError("Agent execution worker is stopping")
        from .run_admission import RunAdmission, RunAdmissionPorts
        from .run_preparation import RunPreparation, RunPreparationPorts

        preparation = RunPreparation(
            RunPreparationPorts(
                accessor_factory=self._accessor_factory,
                available_session_skill_ids=self._available_session_skill_ids,
                capture_provider_account_ids=self._capture_provider_account_ids,
                forget_run=self._forget_run,
                provider_turn_context=self._provider_turn_context,
                remember_run=self._remember_run,
                reconcile_recovered_run=self.reconcile_recovered_run,
                resolve_engine_auth=self._resolve_engine_auth,
                run_service_for_engine=self._run_service_for_engine,
                session_settings=self.session_settings,
                sessions=self.sessions,
                skills=self.skills,
                user_abort_requested=self._user_abort_requested,
                validate_provider_for_run=self.validate_provider_for_run,
                workspace_admission=self.workspace_admission,
            )
        )
        ports = RunAdmissionPorts(
            run_registry=self.run_registry,
            cancel_owned_task=self._cancel_owned_task,
            commit_message_edit=self._commit_message_edit,
            deliver_steers=self._deliver_steers,
            emit_frontend=self._emit_frontend,
            execute_run=self._execute_run,
            finalize_unstarted_run=self._finalize_unstarted_run,
            finish_provider_hold=self._finish_provider_hold,
            forget_run=self._forget_run,
            monitor_provider_turn=self._monitor_provider_turn,
            on_run_task_done=self._on_run_task_done,
            provider_turn_roots=self._provider_turn_roots,
            provider_turn_service=self._provider_turn_service,
            run_service_for_engine=self._run_service_for_engine,
            session_status_payload=self._session_status_payload,
            spawn_background=self._spawn_background,
            user_abort_requested=self._user_abort_requested,
            watch_provider_followup=self._watch_provider_followup,
            history=self.history,
            messages=self.messages,
            metadata=self.metadata,
            reserve_run=self.reserve_run,
            sessions=self.sessions,
        )
        return await RunAdmission(ports, preparation).start(
            session=session,
            prompt=prompt,
            organization_id=organization_id,
            workspace_id=workspace_id,
            user_id=user_id,
            skill_ids=skill_ids,
            scheduled=scheduled,
            account_ids=account_ids,
            max_depth=max_depth,
            _edit_message_id=_edit_message_id,
        )

    def reconcile_recovered_run(
        self, session_id: uuid.UUID | str, assistant_message_id: uuid.UUID | str
    ) -> None:
        """Reconcile local stop/projection state after confirmed durable recovery."""
        self._run_cancellation().reconcile_recovered_run(
            session_id, assistant_message_id
        )

    async def shutdown_runs(self) -> None:
        """Close worker admission and settle owned runs without user-stop intent."""
        await self._background.shutdown(self._shutdown_owned_work)

    async def _shutdown_owned_work(self) -> None:
        """Stop provider producers, join executions, then release their holders."""
        providers = self._provider_turn_lifecycle()
        contexts = providers.cancel_background_work()
        try:
            await self._run_cancellation().shutdown()
        finally:
            await providers.shutdown(contexts)

    async def request_stop(self, session_id: uuid.UUID) -> None:
        """Fence an older claimed admission before its worker waiter is cancelled.

        The command supervisor calls this before joining startup waiters; normal
        abort then joins execution and releases the stop intent. Validate the
        workspace interaction fence before mutating or cancelling local work.
        """
        session = await sync_to_async(self.conversations.get_session)(session_id)
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        self.run_registry.request_abort(str(session_id))

    async def abort_run(self, session_id: uuid.UUID) -> HarnessSession:
        """Cancel the active run tree, reject pending user gates, and mark aborted."""
        session = await sync_to_async(self.conversations.get_session)(session_id)
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        # Stop means stop: queued steers must not survive as a follow-up run.
        await self._discard_steers(session)
        engine_id = getattr(session, "harness_id", "native") or "native"
        if engine_id != "native" and not self.is_running(session.id):
            await self._ensure_external_attempt_reconciled(session, force=True)
            session = await sync_to_async(self.conversations.get_session)(session.id)
        if session.parent_id is not None:
            child_context = self.run_registry.get_context(str(session.id))
            if child_context.get("external_projection"):
                # The native runtime owns the child internally and has no
                # separately addressable OpenCuria task handle. Stop the
                # enclosing root run rather than leaving the subagent alive.
                parent = await sync_to_async(self.sessions.get_by_id)(session.parent_id)
                if parent is not None:
                    return await self._abort_run_tree(parent)
        return await self._abort_run_tree(session)

    # -- live steering ------------------------------------------------------

    def _steering_channel(self, session_id: uuid.UUID | str) -> SteeringChannel | None:
        """Return the open steering channel of an active run, if any."""
        channel = self.run_registry.get_context(str(session_id)).get("steering")
        return channel if isinstance(channel, SteeringChannel) else None

    def list_pending_steers(self, session_id: uuid.UUID) -> list[SteerEntry]:
        """Return queued steers a running turn has not picked up yet."""
        channel = self._steering_channel(session_id)
        return channel.pending() if channel is not None else []

    async def steer_run(
        self, session: HarnessSession, prompt: str, *, user_id: int | None = None
    ) -> SteerEntry:
        """Queue *prompt* so the active run receives it at its next boundary.

        Raises:
            ConflictError: ``no_active_run`` when nothing can take the message
                (idle, stopping, or already settled).
            ValueError: For empty/oversized prompts and subagent sessions.
        """
        SessionAccessService.require_user_promptable(session.parent_id)
        channel = self._steering_channel(session.id)
        if (
            self._background.closing
            or channel is None
            or channel.closed
            or not self.is_running(session.id)
            or self._user_abort_requested(session)
        ):
            raise ConflictError(
                f"Harness session '{session.id}' has no active run to steer",
                code=NO_ACTIVE_RUN_CODE,
            )
        entry = channel.submit(prompt, user_id=user_id)
        await self._emit_steer_queue(session)
        log.info("harness_steer_queued", session_id=str(session.id), steer_id=entry.id)
        return entry

    async def withdraw_steer(
        self, session: HarnessSession, steer_id: str
    ) -> SteerEntry:
        """Remove a queued steer the run has not consumed yet."""
        channel = self._steering_channel(session.id)
        entry = channel.withdraw(steer_id) if channel is not None else None
        if entry is None:
            raise NotFoundError("Steer", steer_id)
        await self._emit_steer_queue(session)
        return entry

    async def _discard_steers(self, session: HarnessSession) -> None:
        """Drop queued steers (user stop) and publish the empty queue."""
        channel = self._steering_channel(session.id)
        if channel is not None and channel.discard():
            await self._emit_steer_queue(session)

    async def _emit_steer_queue(self, session: HarnessSession) -> None:
        """Commit and publish the owner's replaceable reconnect projection."""
        from .command_repository import SteeringSnapshotRepository

        entries = [entry.to_payload() for entry in self.list_pending_steers(session.id)]
        await sync_to_async(SteeringSnapshotRepository.set)(session.id, entries)
        await self._emit_frontend(
            FRONTEND_EVENT_STEER_QUEUE,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": str(session.id),
                "steers": entries,
            },
            str(session.workspace_id),
        )

    async def _emit_empty_steer_queue(self, session_id: str, workspace_id: str) -> None:
        """Clear the saved queue before telling connected clients it is empty."""
        from .command_repository import SteeringSnapshotRepository

        await sync_to_async(SteeringSnapshotRepository.set)(uuid.UUID(session_id), [])
        await self._emit_frontend(
            FRONTEND_EVENT_STEER_QUEUE,
            {"workspace_id": workspace_id, "session_id": session_id, "steers": []},
            workspace_id,
        )

    async def _deliver_steers(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        entries: list[SteerEntry],
    ) -> None:
        """Persist steers the engine took as ordered parts of the live turn.

        The part is created where the engine actually received the message so
        the transcript reads exactly as the model saw it. Running text streams
        are closed first: the answer continues in a fresh part after it.
        """
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        async with self._event_lock(session_id):
            await self.timeline.close_stream_parts(session)
            for entry in entries:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="steer",
                    state="completed",
                    call_id=entry.id,
                    output=entry.content,
                    meta={"steer_id": entry.id},
                )
                await self._emit_frontend(
                    FRONTEND_EVENT_PART,
                    {
                        "workspace_id": workspace_id,
                        "session_id": session_id,
                        "message_id": str(assistant.id),
                        "delta": {"steer": entry.content, "steer_id": entry.id},
                        "part_id": str(part.id),
                    },
                    workspace_id,
                )
        await self._emit_steer_queue(session)

    def _close_steering(self, key: str) -> list[SteerEntry]:
        """Close the run's channel; return entries nobody consumed."""
        channel = self.run_registry.get_context(key).pop("steering", None)
        return channel.close() if isinstance(channel, SteeringChannel) else []

    def _schedule_steer_followup(
        self, session: HarnessSession, entries: list[SteerEntry]
    ) -> None:
        """Turn steers left over after a settled run into the next run."""
        if not entries or self._background.closing:
            return
        self._spawn_background(self._start_steer_followup(session, entries))

    async def _start_steer_followup(
        self, session: HarnessSession, entries: list[SteerEntry]
    ) -> None:
        """Start a normal run with steers that arrived too late to flow in."""
        try:
            fresh = await sync_to_async(self.conversations.get_session)(session.id)
            await self.start_run(
                fresh,
                "\n\n".join(entry.content for entry in entries),
                organization_id=fresh.organization_id,
                workspace_id=str(fresh.workspace_id),
                user_id=entries[-1].user_id,
            )
        except Exception:
            log.exception("harness_steer_followup_failed", session_id=str(session.id))
        finally:
            await self._emit_steer_queue(session)

    def _remember_run(self, session: HarnessSession) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._remember_run(session)

    def _forget_run(self, key: str) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._forget_run(key)

    def _drop_run_tracking(self, key: str) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._drop_run_tracking(key)

    def _descendant_keys(self, root: str) -> set[str]:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._descendant_keys(root)

    def _owned_run_task(self, key: str) -> asyncio.Task | None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._owned_run_task(key)

    def _startup_admission(self, key: str) -> asyncio.Task | None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._startup_admission(key)

    def _cancel_owned_task(self, key: str) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._cancel_owned_task(key)

    def _arm_abort_keys(self, keys: set[str]) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._arm_abort_keys(keys)

    async def _join_owned_task(self, key: str, task: asyncio.Task) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return await self._run_cancellation()._join_owned_task(key, task)

    async def _join_abort_tree(self, root: str, closure: set[str]) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return await self._run_cancellation()._join_abort_tree(root, closure)

    async def _settle_abort_tree(
        self, session: HarnessSession, closure: set[str]
    ) -> HarnessSession:
        """Delegate to the RunCancellation domain boundary."""
        return await self._run_cancellation()._settle_abort_tree(session, closure)

    async def _abort_run_tree(self, session: HarnessSession) -> HarnessSession:
        """Delegate to the RunCancellation domain boundary."""
        return await self._run_cancellation()._abort_run_tree(session)

    async def abort_busy_computeruse_for_workspace(
        self, workspace_id: uuid.UUID
    ) -> list[HarnessSession]:
        """Abort busy computer-use sessions for *workspace_id*.

        Parent build/plan sessions are left running. Each aborted child
        still releases its desktop lease in the computer-use ``finally``.
        """
        sessions = await sync_to_async(
            self.sessions.list_busy_computeruse_for_workspace
        )(workspace_id)
        aborted: list[HarnessSession] = []
        for session in sessions:
            aborted.append(await self.abort_run(session.id))
        return aborted

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _resolve_run_model_limits(
        organization_id: uuid.UUID,
        model_id: str,
        user_id: int | None = None,
        account_ids: dict[str, str] | None = None,
    ) -> tuple[int, int]:
        """Return model limits from the user's selected or frozen accounts."""
        from .services import ProviderConfigService

        service = ProviderConfigService()
        try:
            models = service.list_models(
                organization_id, user_id=user_id, account_ids=account_ids
            )
        except Exception:
            return 0, 0
        namespaced = namespaced_model_id(model_id)
        for model in models:
            if model.id in {model_id, namespaced}:
                return model.context_length, model.max_output_tokens
        return 0, 0

    @staticmethod
    def _last_assistant_step_tokens(
        session_id: uuid.UUID, exclude_message_id: uuid.UUID
    ) -> tuple[int, int, int]:
        """Return last-step prompt/completion/total from the prior assistant turn."""
        prev = HarnessMessageRepository.get_latest_assistant(
            session_id, exclude_id=exclude_message_id
        )
        if prev is None:
            return 0, 0, 0
        part = HarnessPartRepository.get_latest_for_message(prev.id, type="step-finish")
        if part is not None:
            meta = dict(part.meta or {})
            tokens = dict(meta.get("tokens") or {})
            return (
                int(tokens.get("prompt_tokens", 0) or 0),
                int(tokens.get("completion_tokens", 0) or 0),
                int(tokens.get("total_tokens", 0) or 0),
            )
        message_tokens = dict(prev.tokens or {})
        return (
            int(message_tokens.get("prompt", 0) or 0),
            int(message_tokens.get("completion", 0) or 0),
            int(message_tokens.get("total", 0) or 0),
        )

    def _spawn_background(self, coro: Awaitable[None]) -> asyncio.Task[None]:
        """Start *coro* detached from the HTTP request's asgiref executor.

        ``sync_to_async`` inherits the request's ``CurrentThreadExecutor``
        via contextvars. When the response is sent that executor is marked
        broken, so a fire-and-forget run would fail with
        ``CurrentThreadExecutor already quit or is broken``. A fresh
        context plus ``ThreadSensitiveContext`` gives the task its own
        ORM thread for the rest of the run.
        """
        entered = False

        async def owned() -> None:
            nonlocal entered
            entered = True
            await self._await_in_thread_sensitive_context(coro)

        task = asyncio.create_task(owned(), context=contextvars.Context())

        def close_unstarted(done: asyncio.Task) -> None:
            if done.cancelled() and not entered and hasattr(coro, "close"):
                coro.close()

        task.add_done_callback(close_unstarted)
        self._background.track(task)
        return task

    def _on_run_task_done(self, task: asyncio.Task[None], key: str) -> None:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._on_run_task_done(task, key)

    @staticmethod
    async def _await_in_thread_sensitive_context(coro: Awaitable[None]) -> None:
        """Await *coro* with a dedicated thread-sensitive ORM executor."""
        async with ThreadSensitiveContext():
            await coro

    async def _execute_run(
        self,
        *,
        session: HarnessSession,
        prompt: str,
        history: list[LLMMessage],
        assistant: HarnessMessage,
        organization_id: uuid.UUID,
        depth: int,
        max_depth: int,
    ) -> None:
        """Compose the RunExecution boundary with its explicit ports."""
        from .run_execution import RunExecution, RunExecutionPorts

        ports = RunExecutionPorts(
            run_registry=self.run_registry,
            accessor_factory=self._accessor_factory,
            cleanup_session_processes=self._cleanup_session_processes,
            engine_registry_for_run=self._engine_registry_for_run,
            execute_external_run=self._execute_external_run,
            finalize_run=self._finalize_run,
            finish_mcp_recordings=self._finish_mcp_recordings,
            last_assistant_step_tokens=self._last_assistant_step_tokens,
            on_runner_event=self._on_runner_event,
            provider_factory=self._provider_factory,
            provider_turn_revocation_message=self._provider_turn_revocation_message,
            provider_turn_service=self._provider_turn_service,
            record_run_interruption=self._record_run_interruption,
            resolve_run_model_limits=self._resolve_run_model_limits,
            run_subagent_tool=self._run_subagent_tool,
            runner_factory=self._runner_factory,
            user_abort_requested=self._user_abort_requested,
            interactions=self.interactions,
            messages=self.messages,
            plugin_runtime=self.plugin_runtime,
            sessions=self.sessions,
            timeline=self.timeline,
            agent_configs=self._agent_configs_map,
        )
        return await RunExecution(ports).execute(
            session=session,
            prompt=prompt,
            history=history,
            assistant=assistant,
            organization_id=organization_id,
            depth=depth,
            max_depth=max_depth,
        )

    async def _finish_mcp_recordings(
        self, runtime: Any, session: Any, assistant: Any
    ) -> None:
        """Retain confirmed recordings after success, failure and interruption."""
        from .computer_use.recordings import append_video_to_output

        paths = await runtime.finish()
        if not paths:
            return
        await sync_to_async(self.messages.refresh, thread_sensitive=True)(assistant)
        for path in paths:
            existing = assistant.content or ""
            output = append_video_to_output(existing, path)
            remainder = tail_remainder(existing, output)
            if remainder:
                await sync_to_async(self.messages.append_content)(assistant, remainder)
                await self.timeline.append_tail_text(session, assistant, remainder)

    @staticmethod
    def _redact_claude_error(error: object, auth: Any | None) -> str:
        """Redact the resolved per-run secret before logging SDK failures."""
        value = str(error or "Claude run failed")
        token = str(getattr(auth, "token", "") or "")
        if token:
            value = value.replace(token, "[redacted]")
        return redact_text(value)[:1000]

    async def _execute_external_run(
        self,
        *,
        engine_id: str,
        session: HarnessSession,
        prompt: str,
        history: list[LLMMessage],
        assistant: HarnessMessage,
        organization_id: uuid.UUID,
        depth: int,
        max_depth: int,
    ) -> None:
        """Delegate external engine lifecycle work to its execution module."""
        from .engines.execution import ExternalExecutionPorts, execute_external_run

        ports = ExternalExecutionPorts(
            begin_finalization=lambda: self.run_registry.ensure(
                str(session.id)
            ).begin_finalization(),
            accessor_factory=self._accessor_factory,
            cancel_owned_task=self._cancel_owned_task,
            cleanup_session_processes=self._cleanup_session_processes,
            drop_run_tracking=self._drop_run_tracking,
            engine_registry_for_run=self._engine_registry_for_run,
            finalize_external_children=self._finalize_external_children,
            finalize_owned_claude_error=self._finalize_owned_claude_error,
            finalize_run=self._finalize_run,
            finish_mcp_recordings=self._finish_mcp_recordings,
            on_external_child_event=self._on_external_child_event,
            on_external_root_event=self._on_external_root_event,
            provider_turn_revocation_message=self._provider_turn_revocation_message,
            run_subagent_tool=self._run_subagent_tool,
            user_abort_requested=self._user_abort_requested,
            interactions=self.interactions,
            messages=self.messages,
            permissions=self.permissions,
            plugin_runtime=self.plugin_runtime,
            register_provider_use=self._register_provider_use,
            resolve_engine_auth=self._resolve_engine_auth,
        )
        await execute_external_run(
            ports,
            context=self.run_registry.get_context(str(session.id)),
            engine_id=engine_id,
            session=session,
            prompt=prompt,
            history=history,
            assistant=assistant,
            organization_id=organization_id,
            depth=depth,
            max_depth=max_depth,
        )

    async def _finalize_owned_claude_error(
        self, session: HarnessSession, assistant: HarnessMessage
    ) -> None:
        """Publish idle bookkeeping after atomic engine finalization."""
        key = str(session.id)
        try:
            with anyio.CancelScope(shield=True):
                await publish_assistant_run_finished(assistant.id, session_id=key)
                await self._emit_frontend(
                    FRONTEND_EVENT_STATUS,
                    self._session_status_payload(
                        session,
                        "idle",
                        model=assistant.model or "",
                        assistant=assistant,
                    ),
                    str(session.workspace_id),
                )
                if session.parent_id is None:
                    await self.conversation_events.emit(session.workspace_id)
                    await self._schedule_summary(session, assistant)
        finally:
            context = self.run_registry.get_context(key)
            await self._finish_provider_turn(context)
            root = self._provider_turn_roots.get(
                str(context.get("provider_turn_id")), context
            )
            await self._maybe_finish_provider_monitor(root)
            leftovers = self._close_steering(key)
            followup = bool(leftovers) and not self._user_abort_requested(session)
            self._drop_run_tracking(key)
            if followup:
                self._schedule_steer_followup(session, leftovers)

    async def _on_external_root_event(
        self, session: HarnessSession, assistant: HarnessMessage, event: dict[str, Any]
    ) -> None:
        """Project native task notifications to persisted OpenCuria child sessions."""
        if event.get("type") == "subtask_started":
            content = str(event.get("description") or "")
            engine_meta: dict[str, Any] = {}
            # Older adapters use description as the prompt; an explicit null
            # instead means upstream did not expose the delegated instructions.
            if "prompt" in event:
                prompt = event["prompt"]
                available = isinstance(prompt, str) and bool(prompt.strip())
                content = (
                    prompt
                    if available
                    else "The agent did not provide readable task instructions "
                    "for this subagent."
                )
                engine_meta["delegation"] = {
                    "prompt_available": available,
                    "agent_path": str(event.get("agent_path") or ""),
                }
            raw_agent = str(event.get("agent") or "general").strip().lower()
            agent = "explore" if raw_agent in {"explore", "explorer"} else "general"
            child = await sync_to_async(self.sessions.create, thread_sensitive=True)(
                workspace_id=session.workspace_id,
                organization_id=session.organization_id,
                title=str(event.get("description") or agent)[:255],
                mode=session.mode,
                agent_name=agent,
                model=str(event.get("model") or session.model or "sonnet"),
                reasoning_effort=str(
                    event.get("reasoning_effort") or session.reasoning_effort or "high"
                ),
                parent_id=session.id,
                skill_ids=list(session.skill_ids or []),
                harness_id=session.harness_id,
                created_by_id=getattr(session, "created_by_id", None),
            )
            child_user = await sync_to_async(
                self.messages.create, thread_sensitive=True
            )(
                session_id=child.id,
                role="user",
                content=content,
                engine_meta=engine_meta,
                skill_ids=list(session.skill_ids or []),
                harness_id=session.harness_id,
            )
            child_assistant = await sync_to_async(
                self.messages.create, thread_sensitive=True
            )(
                session_id=child.id,
                role="assistant",
                content="",
                model=child.model,
                reasoning_effort=child.reasoning_effort or "high",
                provider=session.harness_id,
                harness_id=session.harness_id,
            )
            child_key = str(child.id)
            self.run_registry.attach_context(
                child_key,
                {
                    "session_id": child_key,
                    "message_id": str(child_assistant.id),
                    "user_message_id": str(child_user.id),
                    "text_part_id": None,
                    "reasoning_part_id": None,
                    "tool_parts": {},
                    "step_parts": {},
                    "subtask_parts": {},
                    "skill_bodies": [],
                    "external_projection": True,
                    "external_parent_session_id": str(session.id),
                    "external_parent_message_id": str(assistant.id),
                },
            )
            await sync_to_async(self.sessions.mark_status)(
                child, HarnessSessionStatus.BUSY
            )
            root_context = self.run_registry.get_context(str(session.id))
            root_context.setdefault("external_children", {})[
                str(event.get("subtask_id", ""))
            ] = {
                "parent_call_id": str(event.get("parent_tool_use_id", "")),
                "session": child,
                "assistant": child_assistant,
                "agent": agent,
                "subtask_id": str(event.get("subtask_id", "")),
            }
            event = {**event, "agent": agent, "child_session_id": child_key}
        elif event.get("type") == "subtask_finished":
            child_ctx = self._external_child_for_event(session, event)
            if child_ctx is not None:
                await self._finish_external_child(child_ctx, event)
                event = {**event, "child_session_id": str(child_ctx["session"].id)}
        await self._on_runner_event(session, assistant, event)

    def _external_child_for_event(
        self, session: HarnessSession, event: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Find a projected child by task id or parent tool-use id."""
        children = self.run_registry.get_context(str(session.id)).get(
            "external_children", {}
        )
        task_id = str(event.get("subtask_id", ""))
        if task_id in children:
            return children[task_id]
        call_id = str(event.get("parent_tool_use_id", ""))
        return next(
            (
                item
                for item in children.values()
                if item.get("parent_call_id") == call_id
            ),
            None,
        )

    async def _on_external_child_event(
        self, root_key: str, parent_call_id: str, event: dict[str, Any]
    ) -> None:
        """Persist native-forwarded child events into their own message timeline."""
        root_context = self.run_registry.get_context(root_key)
        children = root_context.get("external_children", {})
        child_ctx = next(
            (
                item
                for item in children.values()
                if item.get("parent_call_id") == parent_call_id
            ),
            None,
        )
        if child_ctx is None:
            return
        await self._on_runner_event(child_ctx["session"], child_ctx["assistant"], event)

    async def _finish_external_child(
        self, child_ctx: dict[str, Any], event: dict[str, Any]
    ) -> None:
        """Settle a native subagent transcript without duplicating root text."""
        session = child_ctx["session"]
        assistant = child_ctx["assistant"]
        status = str(event.get("status") or "completed")
        await sync_to_async(self.messages.refresh, thread_sensitive=True)(assistant)
        if not assistant.content and event.get("summary"):
            text = str(event["summary"])
            await sync_to_async(self.messages.append_content)(assistant, text)
            await self.timeline.append_tail_text(session, assistant, text)
        await sync_to_async(self.messages.complete)(
            assistant,
            finish=(
                "stop"
                if status == "completed"
                else "aborted"
                if status == "aborted"
                else "error"
            ),
            error=""
            if status == "completed"
            else str(event.get("summary") or "Subagent ended"),
        )
        if status != "completed":
            await self.timeline.fail_open_parts(
                assistant,
                state="error",
                output=str(event.get("summary") or "Subagent ended"),
                session=session,
            )
        else:
            await self.timeline.settle_open_stream_parts(assistant)
        await sync_to_async(self.sessions.mark_status)(
            session, HarnessSessionStatus.IDLE
        )
        child_ctx["finished"] = True
        self._drop_run_tracking(str(session.id))

    async def _finalize_external_children(
        self, session: HarnessSession, assistant: HarnessMessage
    ) -> None:
        """Close any child projections left open by a cancelled native stream."""
        children = self.run_registry.get_context(str(session.id)).get(
            "external_children", {}
        )
        for child_ctx in children.values():
            if child_ctx.get("finished"):
                continue
            try:
                await self._finish_external_child(
                    child_ctx,
                    {
                        "status": "aborted"
                        if self._user_abort_requested(session)
                        else "error",
                        "summary": "Parent external run ended",
                    },
                )
            except Exception:
                log.warning(
                    "external_child_projection_finalize_failed",
                    child_session_id=str(child_ctx.get("session").id),
                )

    def _user_abort_requested(self, session: HarnessSession) -> bool:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._user_abort_requested(session)

    def _parent_stop_in_progress(self, session: HarnessSession) -> bool:
        """Delegate to the RunCancellation domain boundary."""
        return self._run_cancellation()._parent_stop_in_progress(session)

    async def _record_run_interruption(
        self, session: HarnessSession, assistant: HarnessMessage, *, phase: str
    ) -> None:
        """Persist interruption provenance even inside a cancelled MCP scope."""
        key = str(session.id)
        user_abort = self._user_abort_requested(session)
        error = "aborted by user" if user_abort else "Run interrupted unexpectedly"
        with anyio.CancelScope(shield=True):
            await sync_to_async(self.messages.complete)(
                assistant, finish="aborted" if user_abort else "error", error=error
            )
            await self.timeline.fail_open_parts(
                assistant, state="error", output=error, session=session
            )
        if not user_abort:
            log.warning(
                "harness_run_interrupted",
                session_id=key,
                parent_session_id=str(session.parent_id or ""),
                phase=phase,
            )

    async def _finalize_unstarted_run(
        self, session: HarnessSession, assistant: HarnessMessage
    ) -> None:
        """Settle an admitted background task cancelled before execution entry."""
        context = self.run_registry.get_context(str(session.id))
        ownership = context.get("ownership")
        if ownership is not None:
            # Execution never entered, so no runner lease or CLI process could
            # have been created. Fence the durable attempt and shell together.
            user_abort = self._user_abort_requested(session)
            finalized = await ownership.finalize_turn(
                status=HarnessRunStatus.INTERRUPTED,
                finish="aborted" if user_abort else "error",
                error=(
                    "aborted by user" if user_abort else "Run interrupted unexpectedly"
                ),
                assistant_content=assistant.content or "",
            )
            if finalized:
                await self._finalize_run(session, assistant)
            else:
                self._drop_run_tracking(str(session.id))
            return
        try:
            await self._record_run_interruption(session, assistant, phase="startup")
        finally:
            await self._finalize_run(session, assistant)

    async def _finalize_run(
        self, session: HarnessSession, assistant: HarnessMessage
    ) -> None:
        """Release tracking and publish idle after the owning run has settled."""
        key = str(session.id)
        self.run_registry.ensure(key).begin_finalization()
        try:
            with anyio.CancelScope(shield=True):
                await sync_to_async(self.sessions.mark_status)(
                    session, HarnessSessionStatus.IDLE
                )
                # Receiver failures are isolated and logged; they must never
                # prevent idle emission or tracking cleanup.
                await publish_assistant_run_finished(assistant.id, session_id=key)
                if session.parent_id is None:
                    await self._schedule_summary(session, assistant)
                context = self.run_registry.get_context(key)
                await self._finish_provider_turn(context)
                turn_id = context.get("provider_turn_id")
                if turn_id:
                    state = await sync_to_async(self._provider_turn_service.get)(
                        turn_id
                    )
                    if (
                        state is not None
                        and state.active
                        and state.cancel_requested
                        and not state.holds
                    ):
                        await sync_to_async(
                            self._provider_turn_service.clear_monitor_required
                        )(turn_id)
                        state = await sync_to_async(self._provider_turn_service.get)(
                            turn_id
                        )
                    if state is None or not state.active:
                        root = self._provider_turn_roots.get(str(turn_id), context)
                        await self._maybe_finish_provider_monitor(root)
                await self._emit_frontend(
                    FRONTEND_EVENT_STATUS,
                    self._session_status_payload(
                        session,
                        "idle",
                        model=assistant.model or "",
                        assistant=assistant,
                    ),
                    str(session.workspace_id),
                )
                if session.parent_id is None:
                    await self.conversation_events.emit(session.workspace_id)
        finally:
            leftovers = self._close_steering(key)
            followup = bool(leftovers) and not self._user_abort_requested(session)
            self.run_registry.release_context(key)
            self.run_registry.release_event_lock(key)
            self.run_registry.clear_abort(key)
            # Keep task ownership until all persistence/emission awaits have finished.
            if self.run_registry.get_task(key) is asyncio.current_task():
                self.run_registry.release_task(key)
            self._forget_run(key)
            if followup:
                self._schedule_steer_followup(session, leftovers)

    async def _cleanup_session_processes(
        self,
        *,
        workspace_id: str,
        session_id: str,
        reason: str,
    ) -> None:
        """Stop all running temp processes of one finished session.

        Solid by construction, not by subscription:

        - Runs in ``_execute_run.finally`` — the single funnel every run
          passes through, covering success, error, and abort (user stop
          cancels the run task, which still lands here; child/subagent
          runs have their own ``_execute_run`` frame and clean their own
          session scope). ``delete_session``/edit reruns inherit it via
          ``abort_run`` + this finally.
        - Shielded + best-effort (Agent-S ``_run_cleanup_rpc`` pattern):
          a cancellation arriving during cleanup is remembered and
          re-raised afterwards instead of aborting the stops mid-way;
          per-process stop failures are logged and skipped, never
          raised, so cleanup cannot fail the run or swallow the
          original abort.
        - No-op safe: without a cleanup seam (unit tests) or without
          running temps, this returns immediately. Finished rows are
          kept in the DB (never deleted here) — the user list only
          shows running temps, so stopped temps vanish from the UI
          while their history stays queryable for the agent.
        """
        cleanup = self._process_cleanup
        if cleanup is None:
            return
        parsed_workspace: uuid.UUID | None = None
        parsed_session: uuid.UUID | None = None
        try:
            parsed_workspace = uuid.UUID(str(workspace_id))
            parsed_session = uuid.UUID(str(session_id))
        except (ValueError, TypeError, AttributeError):
            log.warning(
                "harness_process_cleanup_skipped",
                workspace_id=str(workspace_id),
                session_id=str(session_id),
                reason="invalid_id",
            )
            return

        async def _run() -> Any:
            result = cleanup(
                parsed_workspace,
                parsed_session,
                reason=reason,
            )
            if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                return await result
            return result

        task: asyncio.Task[Any] = asyncio.ensure_future(_run())
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                current = asyncio.current_task()
                cancelled = cancelled or bool(current and current.cancelling())
                if task.done():
                    if task.cancelled():
                        log.warning(
                            "harness_process_cleanup_interrupted",
                            workspace_id=str(workspace_id),
                            session_id=str(session_id),
                            reason=reason,
                        )
                    break
                continue
            except Exception:
                break
        if task.done() and not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.exception(
                    "harness_process_cleanup_failed",
                    workspace_id=str(workspace_id),
                    session_id=str(session_id),
                    reason=reason,
                    error=str(exc),
                )
        if cancelled:
            raise asyncio.CancelledError()

    async def _on_runner_event(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Persist a runner event; preflight and monitor own account revocation."""
        advances_run = event.get("type") in {
            "step_start",
            "tool_queued",
            "tool_started",
            "part_updated",
        }
        if advances_run and self._user_abort_requested(session):
            raise asyncio.CancelledError()
        context = self._provider_turn_context(session)
        if context.get("provider_revocation_message"):
            from .provider_turns import ProviderAccountRevokedError

            raise ProviderAccountRevokedError()
        async with self._event_lock(str(session.id)):
            await self.timeline.record(session, assistant, event)
        # A dependency can consume Task.cancel() while persisting or emitting.
        # Keep explicit stop intent authoritative before control returns to the loop.
        if advances_run and self._user_abort_requested(session):
            raise asyncio.CancelledError()

    def _event_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock for runner-event persistence."""
        return self.run_registry.event_lock(session_id)

    def _session_status_payload(
        self,
        session: HarnessSession,
        status: str,
        *,
        model: str = "",
        assistant: HarnessMessage | None = None,
        user_message_id: str = "",
    ) -> dict[str, Any]:
        """Shape a ``harness.session_status`` frontend payload.

        ``message_id`` is always the *current* assistant message id so the
        frontend never attaches a fresh turn to an older answer. Callers
        pass the run's assistant shell explicitly; the ``_runs`` context is
        the fallback (busy path after refresh, idle path before cleanup).
        """
        assistant_id = ""
        if assistant is not None:
            assistant_id = str(assistant.id)
        else:
            assistant_id = str(
                self.run_registry.get_context(str(session.id)).get(
                    "message_id", ""
                )
                or ""
            )
        if not user_message_id:
            user_message_id = str(
                self.run_registry.get_context(str(session.id)).get(
                    "user_message_id", ""
                )
                or ""
            )
        payload: dict[str, Any] = {
            "workspace_id": str(session.workspace_id),
            "session_id": str(session.id),
            "status": status,
            "model": (model or session.model or "").strip(),
            "reasoning_effort": session.reasoning_effort or "",
        }
        if assistant_id:
            payload["message_id"] = assistant_id
        if user_message_id:
            payload["user_message_id"] = user_message_id
        return payload

    async def _emit_frontend(
        self, event: str, data: dict[str, Any], workspace_id: str
    ) -> None:
        """Forward an event to subscribed frontend clients (never breaks)."""
        await publish_session_activity(event, data)
        # Cache each part's immutable position: text/reasoning emit many
        # token deltas, so querying the database on every socket emit would
        # add an avoidable round trip to the streaming hot path.
        part_id = data.get("part_id")
        if part_id and event in (FRONTEND_EVENT_PART, FRONTEND_EVENT_SUBTASK_STARTED):
            run_ctx = self.run_registry.get_context(str(data.get("session_id", "")))
            positions = run_ctx.setdefault("part_positions", {})
            position = positions.get(str(part_id))
            if position is None:
                try:
                    position = await sync_to_async(self.parts.get_position)(part_id)
                except (ValueError, TypeError):
                    position = None
                if position is not None:
                    positions[str(part_id)] = position
            if position is not None:
                data["part_position"] = position
        try:
            if self._emit is not None:
                await self._emit(event, data)
                return
            await realtime.emit_to_frontend(event, data, workspace_id)
        except Exception:  # pragma: no cover - forwarding is best-effort
            log.warning("harness_frontend_emit_failed", event_name=event)

    async def _schedule_summary(
        self, session: HarnessSession, assistant: HarnessMessage
    ) -> None:
        """Reserve the answer snapshot before spawning its summary follower."""
        if self._background.closing:
            return
        context = self.run_registry.get_context(str(session.id))
        turn_id = context.get("provider_turn_id")
        if context.get("provider_revocation_message"):
            return
        if not await self.metadata.is_enabled(session.organization_id):
            return
        holder = f"summary:{uuid.uuid4()}"
        if turn_id and not await sync_to_async(self._provider_turn_service.add_hold)(
            turn_id, holder
        ):
            return
        self._spawn_summary(session, assistant, holder=holder)

    def _spawn_summary(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        *,
        holder: str | None = None,
    ) -> None:
        """Generate the user-goal recap with the completed answer's accounts."""
        context = self.run_registry.get_context(str(session.id))
        turn_id = context.get("provider_turn_id")
        reserved = holder is not None
        holder = holder or f"summary:{uuid.uuid4()}"

        async def generate() -> None:
            if turn_id and not reserved:
                if not await sync_to_async(self._provider_turn_service.add_hold)(
                    turn_id, holder
                ):
                    return
            await self.metadata.generate_summary(
                session_id=session.id,
                assistant_id=assistant.id,
                workspace_id=session.workspace_id,
                organization_id=session.organization_id,
                provider_turn_id=turn_id,
                account_ids=dict(context.get("account_ids") or {}),
                initiated_by_id=context.get("initiated_by_id"),
            )

        task = self._spawn_background(generate())
        if turn_id:
            self._watch_provider_followup(context, task, holder)
            task.add_done_callback(
                lambda _task: self._spawn_background(
                    self._finish_provider_hold(turn_id, holder)
                )
            )

    @staticmethod
    def _catalog_efforts_for_model(
        organization_id: uuid.UUID,
        model_id: str,
        user_id: int | None = None,
        account_ids: dict[str, str] | None = None,
    ) -> list[str]:
        """Return catalog reasoning efforts for *model_id* (best-effort)."""
        try:
            from .services import ProviderConfigService

            service = ProviderConfigService()
            models = service.list_models(
                organization_id, user_id=user_id, account_ids=account_ids
            )
        except Exception:
            return []
        target = (model_id or "").strip()
        ns = namespaced_model_id(target)
        for item in models:
            if item.id in {target, ns}:
                return list(getattr(item, "reasoning_efforts", None) or [])
        return []

    async def _run_subagent_tool(
        self,
        *,
        parent: HarnessSession,
        args: TaskArgs,
        ctx: Any,
        subtask_id: str,
        organization_id: uuid.UUID,
        agent_configs: dict[str, dict[str, Any]] | None = None,
    ) -> Any:
        """Delegate to the SubagentCoordinator domain boundary."""
        return await self._subagent_coordinator()._run_subagent_tool(
            parent=parent,
            args=args,
            ctx=ctx,
            subtask_id=subtask_id,
            organization_id=organization_id,
            agent_configs=agent_configs,
        )

    async def _resolve_resume_child(
        self, parent: HarnessSession, task_id: str | None
    ) -> HarnessSession | None:
        """Delegate to the SubagentCoordinator domain boundary."""
        return await self._subagent_coordinator()._resolve_resume_child(parent, task_id)


_default_harness_service: HarnessService | RemoteHarnessService | None = None


def create_default_harness_service() -> HarnessService:
    """Build the production service with runner-backed workspace access.

    Only the execution process installs this live service. API adapters use
    the remote facade and persist commands instead of creating local tasks.
    """
    from .access.runner_accessor import create_harness_accessor

    runner_service = get_runner_service()

    async def accessor_factory(workspace_id: str) -> Any:
        return await create_harness_accessor(runner_service, workspace_id)

    async def process_cleanup(
        workspace_id: uuid.UUID, session_id: uuid.UUID, *, reason: str = ""
    ) -> Any:
        return await runner_service.stop_session_processes(
            workspace_id, session_id, reason=reason or "run_finished"
        )

    return HarnessService(
        accessor_factory=accessor_factory, process_cleanup=process_cleanup
    )


def get_harness_service() -> HarnessService | RemoteHarnessService:
    """Resolve API command submission or the explicitly installed execution owner."""
    global _default_harness_service
    if _default_harness_service is None:
        from .command_service import RemoteHarnessService

        _default_harness_service = RemoteHarnessService(
            create_default_harness_service()
        )
    return _default_harness_service


def configure_execution_service(service: HarnessService) -> None:
    """Install live orchestration only in the independent execution process."""
    global _default_harness_service
    if _default_harness_service is not None and _default_harness_service is not service:
        raise RuntimeError("Harness service already configured in this process")
    _default_harness_service = service


def reset_default_harness_service() -> None:
    """Drop the process singleton (tests)."""
    global _default_harness_service
    _default_harness_service = None
