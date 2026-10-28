"""
Harness session lifecycle service (M6, additive).

``HarnessService`` owns the persistent side of agent runs while
``HarnessRunner`` (M4/M5) owns the streaming loop. The service:

- creates/gets/lists ``HarnessSession`` rows per workspace,
- persists a user message + assistant message shell on run start,
- delegates persisted provider-history replay to ``ProviderHistoryService``,
- starts ``HarnessRunner.run`` as an ``asyncio.Task`` tracked per
  session (one active run per session; a second start is rejected
  with :class:`ConflictError`, surfaced as HTTP 409),
- serializes runner events per session and delegates their timeline
  persistence and live deltas to ``RunTimeline``,
- aborts runs by cancelling the task and marking message/parts aborted,
- delegates user permission and question gates to ``InteractionService``.

No ORM outside repositories. No real LLM calls (provider injected).
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

import anyio
import structlog
from asgiref.sync import ThreadSensitiveContext, sync_to_async

from apps.runners.services import get_runner_service
from apps.runners.services.infra import realtime
from apps.runners.services.workspace_admission import WorkspaceAdmissionService
from apps.skills.services import SkillService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .agents.definitions import get_agent
from .conversation_events import ConversationEvents
from .engines.base import RunOptions
from .images import hydrate_user_messages
from .interaction_service import InteractionService
from .models import (
    HarnessMessage,
    HarnessPart,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
)
from .permissions.service import PermissionService
from .provider_account_events import (
    register_revocation_listener,
    unregister_revocation_listener,
)
from .provider_history_service import ProviderHistoryService
from .providers.base import ChatOptions, LLMMessage, ProviderAdapter
from .providers.model_ref import namespaced_model_id, parse_model_ref
from .providers.models_catalog import normalize_reasoning_effort
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
from .todos.service import TodoService
from .tools.subagents import TaskArgs, validate_subagent_delegation
from .tools.todos import TodoWriteTool

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
        self._emit = emit
        self.conversation_events = ConversationEvents(emit=self._emit_frontend)
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
            run_context=lambda session_id: self._runs.get(session_id, {}),
            emit=self._emit_frontend,
        )
        self._runner_factory = runner_factory
        self._provider_factory = provider_factory
        self._accessor_factory = accessor_factory
        self._tasks: dict[str, asyncio.Task] = {}
        self._admissions: dict[str, asyncio.Task] = {}
        # session id -> parent session id for live runs. Abort walks this
        # instead of waiting for a descendant to notice a shielded cancel.
        self._run_parents: dict[str, str | None] = {}
        # Cancellation is not proof of user intent (e.g. an MCP deadline).
        self._abort_requested: set[str] = set()
        self._event_locks: dict[str, asyncio.Lock] = {}
        # run context kept in memory: session_id -> dict
        self._runs: dict[str, dict[str, Any]] = {}
        self._process_cleanup = process_cleanup
        self._engine_registry = engine_registry
        self._engine_connections = engine_connections
        self._run_service = run_service
        self._provider_turns = None
        self._provider_turn_roots: dict[str, dict[str, Any]] = {}
        self._claude_child_contexts: dict[str, dict[str, Any]] = {}
        self.metadata = SessionMetadataService(
            sessions=self.sessions,
            messages=self.messages,
            provider_factory=provider_factory,
            emit=self._emit_frontend,
            conversations_changed=self.conversation_events.emit,
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
        """Find the snapshot context at this session or its live ancestor."""
        key = str(session.id)
        seen: set[str] = set()
        parent = str(session.parent_id) if session.parent_id else None
        while key and key not in seen:
            seen.add(key)
            context = self._runs.get(key, {})
            if context.get("provider_turn_id"):
                return context
            parent = self._run_parents.get(key, parent)
            if parent is None:
                return {}
            key, parent = parent, self._run_parents.get(parent)
        return {}

    async def _register_provider_use(
        self, turn_id: uuid.UUID | str | None, account_id: uuid.UUID | str | None
    ) -> None:
        """Fence a provider request just before its async network operation."""
        if turn_id is not None and account_id is not None:
            await sync_to_async(self._provider_turn_service.register_use)(
                turn_id, account_id
            )

    async def _provider_turn_revocation_message(self, session: HarnessSession) -> str:
        """Read durable revocation only at run control/terminal boundaries."""
        with anyio.CancelScope(shield=True):
            context = self._provider_turn_context(session)
            if context.get("provider_revocation_message"):
                return str(context["provider_revocation_message"])
            turn_id = context.get("provider_turn_id")
            if turn_id and await sync_to_async(
                self._provider_turn_service.cancellation_reason
            )(turn_id):
                return (
                    "This response was stopped because its selected provider account "
                    "was removed or is no longer shared."
                )
            return ""

    async def _finish_provider_turn(self, context: dict[str, Any]) -> None:
        """Idempotently release this run's named answer holder."""
        turn_id = context.get("provider_turn_id")
        holder = context.get("provider_turn_holder")
        if turn_id and context.get("provider_revocation_message"):
            root = self._provider_turn_roots.get(str(turn_id), context)
            followers = tuple(root.get("provider_followup_tasks", {}).items())
            for _, task in followers:
                if not task.done() and task is not asyncio.current_task():
                    task.cancel()
            for token, task in followers:
                if task is asyncio.current_task():
                    continue
                try:
                    await asyncio.shield(task)
                except (asyncio.CancelledError, Exception):
                    pass
                await sync_to_async(self._provider_turn_service.finish_once)(
                    turn_id, token
                )
        if turn_id and holder and not context.get("provider_turn_finished"):
            context["provider_turn_finished"] = True
            await sync_to_async(self._provider_turn_service.finish_once)(
                turn_id, holder
            )

    async def _finish_provider_hold(
        self, turn_id: uuid.UUID | str, holder: str
    ) -> None:
        """Idempotently release a named title/summary holder."""
        await sync_to_async(self._provider_turn_service.finish_once)(turn_id, holder)
        root = self._provider_turn_roots.get(str(turn_id))
        if root is not None:
            root.setdefault("provider_followup_tasks", {}).pop(holder, None)
            await self._maybe_finish_provider_monitor(root)

    async def _maybe_finish_provider_monitor(self, root: dict[str, Any]) -> None:
        """Stop the root monitor only after the durable answer has no holders."""
        turn_id = root.get("provider_turn_id")
        if not turn_id:
            return
        state = await sync_to_async(self._provider_turn_service.get)(turn_id)
        if (
            state is None
            or not state.active
            or (not state.monitor_required and not state.holds)
        ):
            monitor = root.get("provider_monitor_task")
            if (
                monitor is not None
                and not monitor.done()
                and monitor is not asyncio.current_task()
            ):
                monitor.cancel()
            watched = root.get("provider_revocation_accounts")
            event = root.get("provider_revocation_event")
            if watched and event is not None:
                unregister_revocation_listener(watched, event=event)
            self._provider_turn_roots.pop(str(turn_id), None)

    def _watch_provider_followup(
        self, context: dict[str, Any], task: asyncio.Task, holder: str
    ) -> None:
        """Keep follow-up tasks visible to revocation after root cleanup."""
        tasks = context.setdefault("provider_followup_tasks", {})
        tasks[holder] = task

    async def _monitor_provider_turn(
        self,
        *,
        session: HarnessSession,
        context: dict[str, Any],
        event: asyncio.Event,
        watched_accounts: set[uuid.UUID],
    ) -> None:
        """Poll revocation and abort the root tree, then cancel its followers."""
        turn_id = context.get("provider_turn_id")
        try:
            while turn_id:
                state = await sync_to_async(self._provider_turn_service.get)(turn_id)
                if state is None or not state.active:
                    return
                if await sync_to_async(self._provider_turn_service.cancellation_reason)(
                    turn_id
                ):
                    message = (
                        "This response was stopped because its selected provider "
                        "account was removed or is no longer shared."
                    )
                    context["provider_revocation_message"] = message
                    await sync_to_async(
                        self._provider_turn_service.set_monitor_required
                    )(turn_id)
                    for key in self._descendant_keys(str(session.id)):
                        child = self._runs.get(key)
                        if child is None or child.get("provider_turn_id") != turn_id:
                            continue
                        if child is not None:
                            child["provider_revocation_message"] = message
                        task = self._owned_run_task(key)
                        if (
                            task is not None
                            and not task.done()
                            and not task.cancelling()
                        ):
                            task.cancel()
                    await self.interactions.reject_sessions(
                        {
                            key
                            for key in self._descendant_keys(str(session.id))
                            if self._runs.get(key, {}).get("provider_turn_id")
                            == turn_id
                        }
                    )
                    root = self._provider_turn_roots.get(str(turn_id), context)
                    root["provider_revocation_message"] = message
                    await sync_to_async(
                        self._provider_turn_service.set_monitor_required
                    )(turn_id)
                    followers = tuple(root.get("provider_followup_tasks", {}).values())
                    for task in followers:
                        if not task.done() and not task.cancelling():
                            task.cancel()
                    for task in followers:
                        if task is asyncio.current_task() or task.done():
                            continue
                        try:
                            await asyncio.shield(task)
                        except (asyncio.CancelledError, Exception):
                            pass
                    while True:
                        state = await sync_to_async(self._provider_turn_service.get)(
                            turn_id
                        )
                        if state is None or not state.active or not state.holds:
                            await sync_to_async(
                                self._provider_turn_service.clear_monitor_required
                            )(turn_id)
                            return
                        await asyncio.sleep(0.1)
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("provider_turn_monitor_failed", session_id=str(session.id))
            for key, live in tuple(self._runs.items()):
                if live.get("provider_turn_id") == turn_id:
                    self._cancel_owned_task(key)
        finally:
            unregister_revocation_listener(watched_accounts, event=event)
            if turn_id:
                await self._maybe_finish_provider_monitor(context)
                state = await sync_to_async(self._provider_turn_service.get)(turn_id)
                if state is None or not state.active:
                    self._provider_turn_roots.pop(str(turn_id), None)

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
            parent_session = self.get_session(parent_id)
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

    def get_session(self, session_id: uuid.UUID) -> HarnessSession:
        """Return a session or raise NotFoundError."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return session

    def get_session_for_workspace(
        self,
        session_id: uuid.UUID,
        workspace_id: uuid.UUID,
    ) -> HarnessSession:
        """Return a workspace-scoped session or raise NotFoundError."""
        session = self.sessions.get_for_workspace(session_id, workspace_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
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
        session = await sync_to_async(self.get_session)(session_id)
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
            return True
        raise ConflictError("Harness session already has an active run")

    async def fork_session(
        self,
        session_id: uuid.UUID,
        message_id: uuid.UUID | None = None,
        *,
        include_message: bool = False,
    ) -> HarnessSession:
        """Fork a root session, copying messages before *message_id*.

        By default, *message_id* is excluded; set *include_message* to copy it
        too. Including a message requires a message ID. No file rollback or
        snapshot is performed here.
        """
        if include_message and message_id is None:
            raise ValueError("message_id is required when include_message is true")
        session = await sync_to_async(self.get_session)(session_id)
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        SessionAccessService.require_user_promptable(session.parent_id)
        # Fork is read-only (no assertNotBusy, like OpenCode): it must
        # also work while the source session has an active run.
        if message_id is not None:
            ids = await sync_to_async(self.messages.list_ids_for_session)(session.id)
            if message_id not in ids:
                raise ValueError(f"Message '{message_id}' not in session")
        title = _forked_title(session.title or "")
        fork_kwargs: dict[str, Any] = {
            "workspace_id": session.workspace_id,
            "organization_id": session.organization_id,
            "title": title,
            "mode": session.mode,
            "agent_name": session.agent_name,
            "model": session.model or "",
            "reasoning_effort": session.reasoning_effort or "",
            "skill_ids": list(session.skill_ids or []),
        }
        if getattr(session, "harness_id", "native") != "native":
            fork_kwargs.update(
                harness_id=session.harness_id,
                created_by_id=getattr(session, "created_by_id", None),
                external_session_id="",
                engine_state=(
                    {"provider_id": self._codex_provider_for_session(session)}
                    if session.harness_id == "codex"
                    else {}
                ),
            )
        forked = await sync_to_async(self.sessions.create_fork)(**fork_kwargs)
        await sync_to_async(self.messages.copy_prefix)(
            session.id,
            forked.id,
            message_id,
            include_message=include_message,
        )
        log.info(
            "harness_session_forked",
            session_id=str(session.id),
            fork_id=str(forked.id),
        )
        await self.conversation_events.emit(forked.workspace_id)
        return forked

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
        session = await sync_to_async(self.get_session)(session_id)
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
        provider: ProviderAdapter | None = None,
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
        if provider is not None or self._provider_factory is not None:
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

    def list_sessions(self, workspace_id: uuid.UUID) -> list[HarnessSession]:
        """Return all sessions of a workspace."""
        return self.sessions.list_for_workspace(workspace_id)

    def list_timeline_parts(self, session_id: uuid.UUID) -> list[HarnessPart]:
        """Return lightweight timeline parts without loading large detail fields."""
        self.get_session(session_id)
        return self.parts.list_timeline_for_session(session_id)

    def get_part(self, session_id: uuid.UUID, part_id: uuid.UUID) -> HarnessPart:
        """Return a full part only when it belongs to the supplied session."""
        self.get_session(session_id)
        part = self.parts.get_for_session(session_id, part_id)
        if part is None:
            raise NotFoundError("HarnessPart", str(part_id))
        return part

    def list_timeline_messages(self, session_id: uuid.UUID) -> list[HarnessMessage]:
        """Return message headers without duplicate assistant text bodies."""
        self.get_session(session_id)
        return self.messages.list_timeline_for_session(session_id)

    def list_todos(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return persisted todos of a session as plain dicts."""
        session = self.get_session(session_id)
        rows = self.todos.list_for_session(session.id)
        return [
            {
                "id": str(row.id),
                "content": row.content,
                "status": row.status,
                "priority": row.priority,
                "order": row.order,
            }
            for row in rows
        ]

    # -- run orchestration --------------------------------------------------

    def is_running(self, session_id: uuid.UUID) -> bool:
        """Return True when a run task is active for *session_id*."""
        key = str(session_id)
        task = self._tasks.get(key) or self._admissions.get(key)
        return task is not None and not task.done()

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
        provider: ProviderAdapter | None = None,
        workspace_id: str = "",
        user_id: int | None = None,
        skill_ids: list[str] | None = None,
        scheduled: bool = False,
        account_ids: dict[str, str] | None = None,
        max_depth: int | None = None,
        _edit_message_id: uuid.UUID | None = None,
    ) -> HarnessMessage:
        """Persist user+assistant messages and start the runner task.

        Depth comes from persisted ancestry, including resumed children.
        Child runs inherit the root run's maximum; root runs read org settings.

        Raises:
            ConflictError: When a run is already active for the session.
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt must not be empty")
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        run_user_id = user_id
        inherited_context = self._provider_turn_context(session)
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
        self._remember_run(session)
        # Parent stop already covers this session: do not spawn another run.
        if self._user_abort_requested(session):
            self._forget_run(str(session.id))
            raise asyncio.CancelledError()
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        key = str(session.id)
        if self.is_running(session.id):
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        recovery_accessor = None
        if engine_id != "native":
            run_service = self._run_service_for_engine()
            open_attempt = await sync_to_async(
                run_service.repository.get_open_for_session, thread_sensitive=True
            )(session.id)
            if open_attempt is not None:
                if open_attempt.lease_id is not None:
                    if self._accessor_factory is None:
                        await run_service.prepare_session(session.id, None)
                    else:
                        recovery_accessor = await self._accessor_factory(
                            str(session.workspace_id)
                        )
                        await run_service.prepare_session(session.id, recovery_accessor)
                else:
                    # Let RunOwnership apply its persisted staleness window; do
                    # not create runner connectivity for a healthy unresolved run.
                    await run_service.prepare_session(session.id, None)
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
            await sync_to_async(self.sessions.mark_status)(
                session, HarnessSessionStatus.IDLE
            )
            session.status = HarnessSessionStatus.IDLE
        org_id = organization_id or session.organization_id
        if account_ids is None:
            inherited_ids = inherited_context.get("account_ids")
            account_ids = dict(inherited_ids) if inherited_ids is not None else None
        if account_ids is None and run_user_id is not None:
            account_ids = await sync_to_async(self._capture_provider_account_ids)(
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
            await sync_to_async(self._resolve_engine_auth)(
                org_id, run_user_id, selected_claude
            )
        if skill_ids is None and session.skill_ids:
            available = await sync_to_async(self._available_session_skill_ids)(
                session, user_id=user_id, organization_id=org_id
            )
            if available != list(session.skill_ids):
                if _edit_message_id is not None:
                    # Edits commit session options only once admitted.
                    session.skill_ids = available
                else:
                    session = await sync_to_async(self.sessions.set_skill_ids)(
                        session, available
                    )
        if engine_id == "claude":
            effective_skill_ids = (
                SkillService.normalize_ids(skill_ids)
                if skill_ids is not None
                else SkillService.normalize_ids(list(session.skill_ids or []))
            )
            from .engines.claude.runtime_state import validate_claude_skills

            bodies = (
                await sync_to_async(self.skills.resolve_bodies)(
                    effective_skill_ids,
                    user_id=run_user_id,
                    organization_id=org_id,
                )
                if effective_skill_ids
                else []
            )
            validate_claude_skills(bodies)
        from .services import SubagentConfigService

        depth = await sync_to_async(self.sessions.get_depth)(session.id)
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
                session = await sync_to_async(self.session_settings.update_skill_ids)(
                    session.id,
                    skill_ids,
                    user_id=user_id,
                    organization_id=org_id,
                )
        skill_bodies: list[str] = []
        if session.skill_ids:
            if user_id is None:
                raise AuthenticationError(
                    "A user is required to validate selected skills"
                )
            skill_bodies = await sync_to_async(self.skills.resolve_bodies)(
                list(session.skill_ids or []),
                user_id=user_id,
                organization_id=org_id,
            )
        # Provider validation may fill per-turn defaults in memory. Do not
        # turn Auto selections into persisted edit overrides.
        edit_model, edit_effort = session.model, session.reasoning_effort
        resolved_model = await sync_to_async(self.validate_provider_for_run)(
            org_id,
            session,
            provider=provider,
            user_id=run_user_id,
            account_ids=account_ids,
        )
        resolved_provider = ""
        if provider is not None:
            resolved_provider = provider.name
        else:
            provider_id, _ = parse_model_ref(resolved_model)
            resolved_provider = provider_id
        admitted = await sync_to_async(self.reserve_run)(session, scheduled=scheduled)
        if not admitted:
            detail = (
                "Another chat is already active in this workspace"
                if scheduled
                else f"Harness session '{session.id}' already has an active run"
            )
            raise ConflictError(detail)

        assistant: HarnessMessage | None = None
        user_message: HarnessMessage | None = None
        edit_suffix_ids: list[uuid.UUID] = []
        edit_committed = False
        edit_commit_task: asyncio.Task[None] | None = None
        original_last_message_at = session.last_message_at
        original_updated_at = session.updated_at
        spawned_task: asyncio.Task | None = None
        provider_turn_id: uuid.UUID | None = None
        provider_turn_holder: str | None = None
        title_holder: str | None = None
        inherited_turn_hold = False
        owner = asyncio.current_task()
        if owner is not None:
            self._admissions[key] = owner
        try:
            if _edit_message_id is not None:
                # Read the suffix under the same reservation as normal sends;
                # never delete the provisional messages used for admission.
                stored = await sync_to_async(self.messages.list_for_session)(session.id)
                index = next(
                    (i for i, row in enumerate(stored) if row.id == _edit_message_id),
                    None,
                )
                if index is None:
                    raise NotFoundError("HarnessMessage", str(_edit_message_id))
                edit_suffix_ids = [row.id for row in stored[index:]]
            prior_user_messages = await sync_to_async(self.messages.count_for_session)(
                session.id, role="user"
            )
            user_message = await sync_to_async(self.messages.create)(
                session_id=session.id,
                role="user",
                content=prompt.strip(),
                skill_ids=list(session.skill_ids or []),
                harness_id=engine_id,
            )
            assistant = await sync_to_async(self.messages.create)(
                session_id=session.id,
                role="assistant",
                content="",
                model=resolved_model,
                reasoning_effort=session.reasoning_effort or "",
                provider=("claude" if engine_id == "claude" else resolved_provider),
                harness_id=engine_id,
            )
            if _edit_message_id is None:
                if session.parent_id is None:
                    await sync_to_async(self.sessions.clear_summary)(session.id)
                # Edits defer changes to the existing timeline until admission.
                await sync_to_async(self.messages.dismiss_prior_notices)(
                    session.id, exclude_ids=[assistant.id]
                )
            await sync_to_async(self.sessions.mark_status)(
                session, HarnessSessionStatus.BUSY
            )
            session.status = HarnessSessionStatus.BUSY
            history = await self.history.build(
                session,
                exclude_message_id=assistant.id,
                exclude_user_message_id=user_message.id,
                exclude_message_ids=edit_suffix_ids,
            )
            run_context = {
                "session_id": key,
                "workspace_id": workspace_id or str(session.workspace_id),
                "organization_id": str(org_id),
                "message_id": str(assistant.id),
                "user_message_id": str(user_message.id),
                "text_part_id": None,
                "reasoning_part_id": None,
                "tool_parts": {},
                "step_parts": {},
                "subtask_parts": {},
                "skill_bodies": skill_bodies,
                "initiated_by_id": run_user_id,
                "account_ids": dict(account_ids),
            }
            if session.parent_id is None:
                run_context["steering"] = SteeringChannel(
                    deliver=lambda entries: self._deliver_steers(
                        session, assistant, entries
                    )
                )
            self._runs[key] = run_context
            inherited_turn_id = inherited_context.get("provider_turn_id")
            if inherited_turn_id:
                turn = await sync_to_async(self._provider_turn_service.get)(
                    inherited_turn_id
                )
                if turn is None or not turn.active or turn.cancel_requested:
                    raise ValueError(
                        "The provider account for this answer is no longer available"
                    )
                provider_turn_id = inherited_turn_id
                provider_turn_holder = str(uuid.uuid4())
                inherited_turn_hold = await sync_to_async(
                    self._provider_turn_service.add_hold
                )(provider_turn_id, provider_turn_holder)
                if not inherited_turn_hold:
                    raise ValueError(
                        "The provider account for this answer is no longer available"
                    )
                account_ids = dict(inherited_context.get("account_ids") or {})
                run_context["account_ids"] = account_ids
                run_context["provider_turn_id"] = provider_turn_id
                run_context["provider_turn_holder"] = provider_turn_holder
                # Root monitor already watches the complete answer tree.
                run_context["provider_monitor_task"] = None
            elif run_user_id is not None:
                sidecar = await sync_to_async(self._provider_turn_service.admit)(
                    assistant_message_id=assistant.id,
                    organization_id=org_id,
                    user_id=run_user_id,
                    account_ids=account_ids,
                )
                provider_turn_id = sidecar.id
                provider_turn_holder = str(sidecar.holder_tokens[0])
                revocation_event = asyncio.Event()
                watched_accounts = register_revocation_listener(
                    account_ids.values(),
                    loop=asyncio.get_running_loop(),
                    event=revocation_event,
                )
                run_context.update(
                    provider_turn_id=provider_turn_id,
                    provider_turn_holder=provider_turn_holder,
                    provider_revocation_event=revocation_event,
                    provider_revocation_accounts=watched_accounts,
                    provider_session=session,
                    provider_followup_tasks={},
                )
                self._provider_turn_roots[str(provider_turn_id)] = run_context
                monitor_task = self._spawn_background(
                    self._monitor_provider_turn(
                        session=session,
                        context=run_context,
                        event=revocation_event,
                        watched_accounts=watched_accounts,
                    )
                )
                run_context["provider_monitor_task"] = monitor_task
            if engine_id != "native":
                run_context["initiated_by_id"] = run_user_id
                run_context["engine_id"] = engine_id
                run_context["cleanup_confirmed"] = True
                run_context["ownership"] = await sync_to_async(
                    self._run_service_for_engine().create_attempt,
                    thread_sensitive=True,
                )(
                    session_id=session.id,
                    assistant_message_id=assistant.id,
                    user_id=run_user_id,
                    harness_id=engine_id,
                )
            if self._user_abort_requested(session):
                raise asyncio.CancelledError()
            if _edit_message_id is not None:
                # A cancelled sync_to_async waiter does not roll back its DB
                # thread. Keep ownership until the commit outcome is known.
                edit_commit_task = asyncio.create_task(
                    self._commit_message_edit(
                        session,
                        edit_suffix_ids,
                        model=edit_model,
                        reasoning_effort=edit_effort,
                    )
                )
                await asyncio.shield(edit_commit_task)
                edit_committed = True
            if (
                _edit_message_id is None
                and session.parent_id is None
                and prior_user_messages == 0
                and provider_turn_id
                and not scheduled
            ):
                # Reserve before spawning: a fast answer may release its last
                # holder before the title task gets a chance to start.
                title_holder = f"title:{uuid.uuid4()}"
                if not await sync_to_async(self._provider_turn_service.add_hold)(
                    provider_turn_id, title_holder
                ):
                    title_holder = None
            run_coro = self._execute_run(
                session=session,
                prompt=prompt.strip(),
                history=history,
                assistant=assistant,
                provider=provider,
                organization_id=org_id,
                depth=depth,
                max_depth=max_depth,
            )
            spawned_task = self._spawn_background(run_coro)
            self._tasks[key] = spawned_task
            self._admissions.pop(key, None)

            def run_done(task: asyncio.Task) -> None:
                # A cancelled task may never execute its first instruction, so
                # neither the run's except nor finally would finalize its shell.
                if task.cancelled() and not run_context.get("execution_started"):
                    run_coro.close()
                    run_context["finalizing"] = True
                    finalizer = self._spawn_background(
                        self._finalize_unstarted_run(session, assistant)
                    )
                    self._tasks[key] = finalizer
                    finalizer.add_done_callback(
                        lambda done: self._on_run_task_done(done, key)
                    )
                self._on_run_task_done(task, key)

            spawned_task.add_done_callback(run_done)
            if title_holder is not None:
                title_task = self._spawn_background(
                    self.metadata.generate_title(
                        session_id=session.id,
                        prompt=prompt.strip(),
                        organization_id=org_id,
                        provider_turn_id=provider_turn_id,
                        account_ids=dict(account_ids),
                        initiated_by_id=run_user_id,
                    )
                )
                self._watch_provider_followup(run_context, title_task, title_holder)
                title_task.add_done_callback(
                    lambda _task, turn_id=provider_turn_id, holder=title_holder: (
                        self._spawn_background(
                            self._finish_provider_hold(turn_id, holder)
                        )
                    )
                )
            if self._user_abort_requested(session):
                # Stop arrived while this run was being admitted. Cancel a
                # still-running owner. A task already cancelled before its
                # callback was installed must keep settling and return.
                already_stopping = spawned_task.done() or bool(
                    spawned_task.cancelling()
                )
                self._cancel_owned_task(key)
                if not already_stopping and (
                    spawned_task.cancelling() or spawned_task.done()
                ):
                    raise asyncio.CancelledError()
            await self._emit_frontend(
                FRONTEND_EVENT_STATUS,
                self._session_status_payload(
                    session,
                    "busy",
                    model=resolved_model,
                    assistant=assistant,
                    user_message_id=str(user_message.id),
                ),
                str(session.workspace_id),
            )
            log.info(
                "harness_run_started",
                session_id=key,
                user_message_id=str(user_message.id),
            )
            return assistant
        except BaseException as exc:
            if spawned_task is not None:
                # A dropped HTTP caller must not erase a live background turn.
                # User stop cancels that owner instead of leaving it running.
                if isinstance(exc, asyncio.CancelledError):
                    if self._user_abort_requested(session):
                        self._cancel_owned_task(key)
                    else:
                        log.exception("harness_run_admitted_with_caller_error")
                    raise
                log.exception("harness_run_admitted_with_caller_error")
                return assistant
            if edit_commit_task is not None:
                while not edit_commit_task.done():
                    try:
                        await asyncio.shield(edit_commit_task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                edit_committed = (
                    not edit_commit_task.cancelled()
                    and edit_commit_task.exception() is None
                )
            user_abort = self._user_abort_requested(session)
            # sync_to_async re-raises CancelledError after a shielded write
            # when this task is already stopping. Retry until the shell is
            # durable, then propagate the original cancellation.
            with anyio.CancelScope(shield=True):
                for _ in range(4):
                    try:
                        await sync_to_async(self.sessions.mark_status)(
                            session, HarnessSessionStatus.IDLE
                        )
                        session.status = HarnessSessionStatus.IDLE
                        if assistant is not None:
                            await sync_to_async(self.messages.complete)(
                                assistant,
                                finish="aborted" if user_abort else "error",
                                error=(
                                    "aborted by user"
                                    if user_abort
                                    else "Run admission failed before the "
                                    "harness started: "
                                    + (str(exc) or type(exc).__name__)
                                ),
                            )
                        break
                    except asyncio.CancelledError:
                        continue
            run_context = self._runs.get(key, {})
            if provider_turn_id and provider_turn_holder and spawned_task is None:
                await sync_to_async(self._provider_turn_service.finish_once)(
                    provider_turn_id, provider_turn_holder
                )
            if provider_turn_id and title_holder and spawned_task is None:
                await self._finish_provider_hold(provider_turn_id, title_holder)
            monitor_task = run_context.get("provider_monitor_task")
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                watched = run_context.get("provider_revocation_accounts")
                event = run_context.get("provider_revocation_event")
                if watched and event is not None:
                    unregister_revocation_listener(watched, event=event)
            durable_owner = run_context.get("ownership")
            if durable_owner is not None:
                try:
                    await durable_owner.stop_heartbeat()
                    await durable_owner.begin_close()
                    await durable_owner.finalize_turn(
                        status=(
                            HarnessRunStatus.INTERRUPTED
                            if user_abort
                            else HarnessRunStatus.ERROR
                        ),
                        finish="aborted" if user_abort else "error",
                        error=(
                            "aborted by user" if user_abort else "Run admission failed"
                        ),
                    )
                except Exception:
                    log.warning("harness_run_attempt_finalize_failed", session_id=key)
            if _edit_message_id is not None and not edit_committed:
                # Failed admission must not leave a replacement turn or alter
                # the original timeline's activity timestamp.
                with anyio.CancelScope(shield=True):
                    await sync_to_async(self.messages.discard_edit_run)(
                        session.id,
                        message_ids=[
                            row.id
                            for row in (user_message, assistant)
                            if row is not None
                        ],
                        last_message_at=original_last_message_at,
                        updated_at=original_updated_at,
                    )
                self._runs.pop(key, None)
            self._forget_run(key)
            raise
        finally:
            if self._admissions.get(key) is owner:
                self._admissions.pop(key, None)

    async def abort_run(self, session_id: uuid.UUID) -> HarnessSession:
        """Cancel the active run tree, reject pending user gates, and mark aborted."""
        session = await sync_to_async(self.get_session)(session_id)
        await sync_to_async(self.workspace_admission.ensure_interactions_available)(
            session.workspace_id
        )
        # Stop means stop: queued steers must not survive as a follow-up run.
        await self._discard_steers(session)
        engine_id = getattr(session, "harness_id", "native") or "native"
        if engine_id != "native" and not self.is_running(session.id):
            await self._ensure_external_attempt_reconciled(session, force=True)
            session = await sync_to_async(self.get_session)(session.id)
        if session.parent_id is not None:
            child_context = self._runs.get(str(session.id), {})
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
        channel = self._runs.get(str(session_id), {}).get("steering")
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
            channel is None
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
        """Publish the full queue snapshot (replace semantics for clients)."""
        await self._emit_frontend(
            FRONTEND_EVENT_STEER_QUEUE,
            {
                "workspace_id": str(session.workspace_id),
                "session_id": str(session.id),
                "steers": [
                    entry.to_payload() for entry in self.list_pending_steers(session.id)
                ],
            },
            str(session.workspace_id),
        )

    async def _emit_empty_steer_queue(self, session_id: str, workspace_id: str) -> None:
        """Tell clients that no steer is queued for *session_id* anymore."""
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
        channel = self._runs.get(key, {}).pop("steering", None)
        return channel.close() if isinstance(channel, SteeringChannel) else []

    def _schedule_steer_followup(
        self, session: HarnessSession, entries: list[SteerEntry]
    ) -> None:
        """Turn steers left over after a settled run into the next run."""
        if not entries:
            return
        self._spawn_background(self._start_steer_followup(session, entries))

    async def _start_steer_followup(
        self, session: HarnessSession, entries: list[SteerEntry]
    ) -> None:
        """Start a normal run with steers that arrived too late to flow in."""
        try:
            fresh = await sync_to_async(self.get_session)(session.id)
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
        """Index a live session under its parent before the next await."""
        key = str(session.id)
        parent = str(session.parent_id) if session.parent_id else None
        self._run_parents[key] = parent

    def _forget_run(self, key: str) -> None:
        """Drop a finished session once nothing still points at it."""
        parent = self._run_parents.get(key)
        if any(owner == key for owner in self._run_parents.values()):
            return
        self._run_parents.pop(key, None)
        if (
            parent
            and parent not in self._tasks
            and parent not in self._runs
            and parent not in self._admissions
        ):
            self._forget_run(parent)

    def _drop_run_tracking(self, key: str) -> None:
        """Release local task bookkeeping without publishing a terminal status."""
        workspace_id = str(self._runs.get(key, {}).get("workspace_id") or "")
        dropped = self._close_steering(key)
        if dropped and workspace_id:
            # No follow-up here (the run's cleanup is unconfirmed), but clients
            # must not keep showing messages that will never be delivered.
            log.warning("harness_steers_discarded", session_id=key, count=len(dropped))
            try:
                self._spawn_background(self._emit_empty_steer_queue(key, workspace_id))
            except RuntimeError:  # pragma: no cover - no running loop
                pass
        self._runs.pop(key, None)
        self._event_locks.pop(key, None)
        if self._tasks.get(key) is asyncio.current_task():
            self._tasks.pop(key, None)
        self._forget_run(key)

    def _descendant_keys(self, root: str) -> set[str]:
        """Return *root* and in-memory descendant session ids."""
        keys = {root}
        changed = True
        while changed:
            changed = False
            for key, parent in self._run_parents.items():
                if parent in keys and key not in keys:
                    keys.add(key)
                    changed = True
        return keys

    def _owned_run_task(self, key: str) -> asyncio.Task | None:
        """Return the run owner for *key*.

        Child admissions are the parent tool task (``harness-tool-*``).
        Cancelling those would abort the parent, so only a dedicated run
        task or a non-tool admission (root startup) is owned here.
        """
        task = self._tasks.get(key)
        if task is not None:
            return task
        admission = self._admissions.get(key)
        if admission is None or admission.done():
            return None
        if admission.get_name().startswith("harness-tool-"):
            return None
        return admission

    def _startup_admission(self, key: str) -> asyncio.Task | None:
        """Return an in-flight ``start_run`` when no background owner exists yet."""
        task = self._tasks.get(key)
        if task is not None and not task.done():
            return None
        admission = self._admissions.get(key)
        if admission is None or admission.done() or admission is asyncio.current_task():
            return None
        return admission

    def _cancel_owned_task(self, key: str) -> None:
        """Cancel *key*'s run unless it is already stopping or finalizing."""
        task = self._owned_run_task(key)
        if task is None or task.done():
            task = None
        elif not task.cancelling() and not self._runs.get(key, {}).get("finalizing"):
            task.cancel()
        # A start_run blocked before its background task exists is the
        # admission (often the parent tool task). Cancel that waiter too
        # when this session has no live owner yet.
        admission = self._startup_admission(key)
        if (
            admission is None
            or admission is task
            or admission.cancelling()
            or self._runs.get(key, {}).get("finalizing")
        ):
            return
        admission.cancel()

    def _arm_abort_keys(self, keys: set[str]) -> None:
        """Mark user-stop intent and cancel every owned run in *keys*."""
        for key in keys:
            self._abort_requested.add(key)
            self._cancel_owned_task(key)

    async def _join_owned_task(self, key: str, task: asyncio.Task) -> None:
        """Wait for *task* without abandoning it if this stop is cancelled."""
        if task.done() or task is asyncio.current_task():
            return
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                raise
        except Exception:  # pragma: no cover - abort must not raise
            log.exception("harness_abort_run_error", session_id=key)
        # The done callback may have replaced a pre-entry task. Join that
        # owner too, without cancelling it: it is already settling.
        finalizer = self._tasks.get(key)
        if finalizer is not None and finalizer is not task and not finalizer.done():
            await self._join_owned_task(key, finalizer)

    async def _join_abort_tree(self, root: str, closure: set[str]) -> None:
        """Cancel and join every live owner in *closure* until it is stable."""
        while True:
            closure.update(self._descendant_keys(root))
            self._arm_abort_keys(closure)
            pending: list[tuple[str, asyncio.Task]] = []
            seen: set[int] = set()
            for key in closure:
                candidates = (
                    self._owned_run_task(key),
                    self._startup_admission(key),
                )
                for task in candidates:
                    if task is None or task.done() or task is asyncio.current_task():
                        continue
                    if id(task) in seen:
                        continue
                    seen.add(id(task))
                    pending.append((key, task))
            if not pending:
                return
            key, task = pending[0]
            await self._join_owned_task(key, task)

    async def _settle_abort_tree(
        self, session: HarnessSession, closure: set[str]
    ) -> HarnessSession:
        """Mark leftover busy rows idle and drop stop intent for *closure*."""
        ids: list[uuid.UUID] = []
        for key in closure:
            try:
                ids.append(uuid.UUID(key))
            except ValueError:
                continue
        rows = await sync_to_async(self.sessions.list_by_ids)(ids)
        fresh = session
        unresolved = (
            await sync_to_async(
                self._run_service_for_engine().repository.list_open_for_sessions,
                thread_sensitive=True,
            )(ids)
            if ids
            else []
        )
        unresolved_ids = {str(item.session_id) for item in unresolved}
        for row in rows:
            key = str(row.id)
            task = self._owned_run_task(key)
            if key in unresolved_ids or (task is not None and not task.done()):
                continue
            if row.status != HarnessSessionStatus.IDLE:
                await sync_to_async(self.sessions.mark_status)(
                    row, HarnessSessionStatus.IDLE
                )
                row.status = HarnessSessionStatus.IDLE
            self._abort_requested.discard(key)
            if row.id == session.id:
                fresh = row
        return fresh

    async def _abort_run_tree(self, session: HarnessSession) -> HarnessSession:
        """Cancel and join an authorized stop tree, including descendants.

        Intent is published and every registered run is cancelled before
        any join. ``asyncio.shield`` around a child therefore cannot keep
        that child alive until the root happens to unwind.
        """
        root = str(session.id)
        # Read descendants before publishing intent. A leaf admitted during
        # this query is then in the memory index and is armed with the rest,
        # instead of being cancelled before it can be registered.
        descendant_ids = await sync_to_async(self.sessions.list_descendant_ids)(
            session.id
        )
        closure = {str(child_id) for child_id in descendant_ids}
        closure.update(self._descendant_keys(root))
        durable_attempts = await sync_to_async(
            self._run_service_for_engine().repository.list_open_for_sessions,
            thread_sensitive=True,
        )(list(uuid.UUID(key) for key in closure))
        for attempt in durable_attempts:
            attempt_key = str(attempt.session_id)
            if (
                self._tasks.get(attempt_key) is None
                and self._admissions.get(attempt_key) is None
            ):
                # Retry detached cleanup; recovery still fences fresh owners
                # and requires the runner to confirm process termination.
                detached_session = await sync_to_async(self.get_session)(
                    attempt.session_id
                )
                await self._ensure_external_attempt_reconciled(
                    detached_session, force=True
                )
        # Cancel and join before any further database work. Cleanup writes
        # the terminal message; overlapping reads deadlock SQLite.
        self._arm_abort_keys(closure)
        await self._join_abort_tree(root, closure)
        descendant_ids = await sync_to_async(self.sessions.list_descendant_ids)(
            session.id
        )
        closure.update(str(child_id) for child_id in descendant_ids)
        closure.update(self._descendant_keys(root))
        self._arm_abort_keys(closure)
        await self._join_abort_tree(root, closure)
        await self.interactions.reject_sessions(closure)
        settled = await self._settle_abort_tree(session, closure)
        remaining = await sync_to_async(
            self._run_service_for_engine().repository.list_open_for_sessions,
            thread_sensitive=True,
        )(list(uuid.UUID(key) for key in closure))
        if remaining:
            raise ConflictError(
                "External engine cleanup is pending; the session remains busy"
            )
        return settled

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
        return task

    def _on_run_task_done(self, task: asyncio.Task[None], key: str) -> None:
        """Drop the run task and retrieve any leftover exception."""
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "harness_background_task_failed",
                session_id=key,
                error=str(exc),
            )

    @staticmethod
    async def _await_in_thread_sensitive_context(coro: Awaitable[None]) -> None:
        """Await *coro* with a dedicated thread-sensitive ORM executor."""
        async with ThreadSensitiveContext():
            await coro

    def _tools_for_session(self, session_id: str, agent_name: str = "build"):  # type: ignore[no-untyped-def]
        """Build the tool registry for a session agent."""
        from .tools import default_tool_registry

        registry = default_tool_registry()
        try:
            registry._tools["todowrite"] = TodoWriteTool(
                service=TodoService.for_session(session_id)
            )
        except KeyError:  # pragma: no cover - registry always has todowrite
            pass
        return registry

    async def _execute_run(
        self,
        *,
        session: HarnessSession,
        prompt: str,
        history: list[LLMMessage],
        assistant: HarnessMessage,
        provider: ProviderAdapter | None,
        organization_id: uuid.UUID,
        depth: int,
        max_depth: int,
    ) -> None:
        """Run the loop, persist events, and finalize the assistant message."""
        from .providers.resolver import StaticModelResolver
        from .services import ProviderConfigService

        key = str(session.id)
        run_context = self._runs.get(key, {})
        run_context["execution_started"] = True
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        mcp_runtime = None
        phase = "startup"
        if engine_id != "native":
            await self._execute_external_run(
                engine_id=engine_id,
                session=session,
                prompt=prompt,
                history=history,
                assistant=assistant,
                organization_id=organization_id,
                depth=depth,
                max_depth=max_depth,
            )
            return
        try:
            # Fail closed on missing plugin credentials before constructing provider
            # configuration or registering/calling any harness tools.
            mcp_snapshot = None
            agent_key = (session.agent_name or "").strip().lower()
            if agent_key not in {"title", "summary", "compaction"}:
                mcp_snapshot = await sync_to_async(self._prepare_mcp_snapshot_for_run)(
                    session, organization_id
                )
            model_resolver = None
            small_model = ""
            agent_configs: dict[str, dict[str, Any]] = {}
            if provider is not None:
                model_resolver = StaticModelResolver(provider).resolve
            elif self._provider_factory is not None:
                model_resolver = StaticModelResolver(
                    self._provider_factory(organization_id)
                ).resolve
            else:
                config_service = ProviderConfigService()
                config = None
                try:
                    config = await sync_to_async(config_service.get_config)(
                        organization_id
                    )
                except NotFoundError:
                    config = None
                run_context = self._runs.get(key, {})
                turn_id = run_context.get("provider_turn_id")

                def register_used(account_id: str) -> None:
                    if turn_id:
                        self._provider_turn_service.register_use(turn_id, account_id)

                resolver = await sync_to_async(config_service.build_resolver)(
                    organization_id,
                    user_id=run_context.get("initiated_by_id"),
                    account_ids=dict(run_context.get("account_ids") or {}),
                    on_account_used=register_used if turn_id else None,
                )
                await sync_to_async(resolver.prepare_adapters)()
                model_resolver = resolver.resolve
                small_model = ((config.small_model if config else "") or "").strip()
                agent_configs = await sync_to_async(HarnessService._agent_configs_map)(
                    organization_id
                )
                agent_name = (
                    (session.agent_name or session.mode or "build").strip().lower()
                )
                row = agent_configs.get(agent_name, {})
                agent_model = (
                    ""
                    if row.get("inherit_model")
                    else str(row.get("model") or "").strip()
                )
                agent_effort = (
                    ""
                    if row.get("inherit_model")
                    else normalize_reasoning_effort(str(row.get("effort") or ""))
                )
                model_default = session.model or agent_model
                if not model_default:
                    raise ValueError("No model configured for harness run")
                session.model = model_default
                run_effort = normalize_reasoning_effort(agent_effort)
                if not (session.reasoning_effort or "").strip() and run_effort:
                    session.reasoning_effort = run_effort
            model = session.model or "default"
            context_length = 0
            model_max_output_tokens = 0
            last_step_prompt_tokens = 0
            last_step_completion_tokens = 0
            last_step_total_tokens = 0
            context_length, model_max_output_tokens = await sync_to_async(
                self._resolve_run_model_limits
            )(
                organization_id,
                model,
                run_context.get("initiated_by_id"),
                dict(run_context.get("account_ids") or {}),
            )
            (
                last_step_prompt_tokens,
                last_step_completion_tokens,
                last_step_total_tokens,
            ) = await sync_to_async(self._last_assistant_step_tokens)(
                session.id, assistant.id
            )
            accessor = None
            if self._accessor_factory is not None:
                accessor = await self._accessor_factory(str(session.workspace_id))
            if accessor is not None:
                history = await hydrate_user_messages(history, accessor)
            tools = self._tools_for_session(key, session.agent_name or "build")
            from .services import ComputerUseConfigService

            computer_options = await sync_to_async(
                ComputerUseConfigService().get_or_default
            )(organization_id)
            # MCP plugin runtime: snapshot + connections are prepared BEFORE
            # the runner is constructed so any injected runner_factory sees
            # the same fully-registered ToolRegistry (an injected factory may
            # snapshot/copy instead of holding the live reference). Prepare
            # runs in the sync ORM context exactly once (snapshot +
            # credential resolution, no second ORM/decrypt round in setup).
            mcp_enabled = accessor is not None and (
                session.agent_name or ""
            ).strip().lower() not in (
                "title",
                "summary",
                "compaction",
            )
            if mcp_enabled:
                import apps.harness.mcp_client.runtime as mcp_runtime_module

                mcp_runtime = mcp_runtime_module.McpRuntime()
                # Reuse the already validated snapshot; setup does not repeat
                # credential lookup/decryption.
                await mcp_runtime.setup(
                    accessor=accessor,
                    core_tool_names=tools.names(),
                    prepared=mcp_snapshot,
                    owner_id=str(assistant.id),
                    session_id=key,
                    turn_id=str(assistant.id),
                    runtime_options=computer_options,
                )
                mcp_runtime.register_tools(tools)
                if agent_key == "computeruse":
                    mcp_runtime.require_computer_use()
                if mcp_runtime.skipped:
                    log.warning(
                        "mcp_discovery_health",
                        session_id=key,
                        skipped=mcp_runtime.skipped,
                    )
            if self._runner_factory is not None:
                loop_runner = self._runner_factory(
                    provider=provider,
                    model_resolver=model_resolver,
                    tools=tools,
                    accessor=accessor,
                    emit=lambda event: self._on_runner_event(session, assistant, event),
                )
            else:
                effort = (session.reasoning_effort or "").strip() or None
                loop_runner = self._engine_registry_for_run().create(
                    "native",
                    provider=provider,
                    model_resolver=model_resolver,
                    tools=tools,
                    accessor=accessor,
                    emit=lambda event: self._on_runner_event(session, assistant, event),
                    chat_options=ChatOptions(reasoning_effort=effort),
                )
            opts = RunOptions(
                max_steps=computer_options["max_steps"]
                if agent_key == "computeruse"
                else None,
                depth=depth,
                max_depth=max_depth,
                history=history,
                session_id=key,
                workspace_id=str(session.workspace_id),
                organization_id=str(organization_id),
                small_model=small_model,
                current_user_message_id=str(
                    self._runs.get(key, {}).get("user_message_id", "")
                ),
                skills=SkillService.merge_bodies(
                    self._runs.get(key, {}).get("skill_bodies", []),
                    mcp_snapshot.snapshot.plugin_skills
                    if mcp_snapshot is not None
                    else [],
                ),
                steering=self._runs.get(key, {}).get("steering"),
                context_length=context_length,
                max_output_tokens=model_max_output_tokens,
                last_step_prompt_tokens=last_step_prompt_tokens,
                last_step_completion_tokens=last_step_completion_tokens,
                last_step_total_tokens=last_step_total_tokens,
                on_permission=lambda **kw: self.interactions.request_permission(
                    session, assistant, **kw
                ),
                on_question=lambda **kw: self.interactions.ask_question(
                    session, assistant, **kw
                ),
                run_subagent=lambda args, ctx, subtask_id: self._run_subagent_tool(
                    parent=session,
                    args=args,
                    ctx=ctx,
                    subtask_id=subtask_id,
                    organization_id=organization_id,
                    agent_configs=dict(agent_configs),
                ),
            )
            phase = "run"
            result = await loop_runner.run(
                prompt, session.agent_name or "build", model, session.mode, opts
            )
            if mcp_runtime is not None:
                from .computer_use.recordings import append_video_to_output

                for path in await mcp_runtime.finish():
                    result.output = append_video_to_output(result.output or "", path)
            if self._user_abort_requested(session):
                raise asyncio.CancelledError()
            # Text deltas were already appended to the assistant message
            # incrementally (see RunTimeline.record); only genuinely new
            # tail content still needs persisting. The runner's final
            # ``output`` is the full assembled answer, so diff it against
            # what is already stored instead of appending blindly (content
            # drifts when a rerun/continuation re-emits a prefix). When a
            # remainder exists it also gets its own text part so message
            # content and parts never drift apart.
            await sync_to_async(self.messages.refresh, thread_sensitive=True)(assistant)
            existing = assistant.content or ""
            tail = result.output or ""
            remainder = tail_remainder(existing, tail)
            if remainder:
                await sync_to_async(self.messages.append_content)(assistant, remainder)
                await self.timeline.append_tail_text(session, assistant, remainder)
            # Message usage is accumulated per step_finish so abort still
            # keeps completed steps. Session usage is applied once here.
            await sync_to_async(self.sessions.add_usage)(
                session,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.total_tokens,
                cost=result.cost,
            )
            await sync_to_async(self.messages.complete)(
                assistant, finish=result.finish_reason
            )
            await self.timeline.settle_open_stream_parts(assistant)
        except asyncio.CancelledError:
            revocation = await self._provider_turn_revocation_message(session)
            if revocation:
                run_context["provider_revocation_message"] = revocation
                with anyio.CancelScope(shield=True):
                    await sync_to_async(self.messages.complete)(
                        assistant, finish="aborted", error=revocation
                    )
                    await self.timeline.fail_open_parts(
                        assistant, state="error", output=revocation, session=session
                    )
            else:
                await self._record_run_interruption(session, assistant, phase=phase)
            raise
        except Exception as exc:
            # SDK cancellation is level-triggered until its scopes are closed.
            revocation = await self._provider_turn_revocation_message(session)
            public_error = revocation or str(exc)
            with anyio.CancelScope(shield=True):
                await sync_to_async(self.messages.complete)(
                    assistant,
                    finish="aborted" if revocation else "error",
                    error=public_error,
                )
                await self.timeline.fail_open_parts(
                    assistant, state="error", output=public_error, session=session
                )
            if not revocation:
                log.exception("harness_run_failed", session_id=key)
        finally:
            self._runs.get(key, {})["finalizing"] = True
            # MCP scopes belong to this task and must close here, in LIFO order.
            # Even a cancellation during teardown must reach status finalization.
            try:
                try:
                    if mcp_runtime is not None:
                        try:
                            try:
                                await self._finish_mcp_recordings(
                                    mcp_runtime, session, assistant
                                )
                            finally:
                                await mcp_runtime.aclose()
                        except asyncio.CancelledError:
                            current = asyncio.current_task()
                            if current is not None and current.cancelling():
                                raise
                            log.warning("mcp_runtime_close_interrupted", session_id=key)
                        except Exception:  # pragma: no cover - best effort
                            log.exception("mcp_runtime_close_failed", session_id=key)
                finally:
                    await self._cleanup_session_processes(
                        workspace_id=str(session.workspace_id),
                        session_id=key,
                        reason="run_finished",
                    )
            finally:
                await self._finalize_run(session, assistant)

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
        from .engines.claude.security import redact_text

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
        from .engines.execution import execute_external_run

        await execute_external_run(
            self,
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
            context = self._runs.get(key, {})
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
                content=str(event.get("description") or ""),
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
            self._runs[child_key] = {
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
            }
            await sync_to_async(self.sessions.mark_status)(
                child, HarnessSessionStatus.BUSY
            )
            root_context = self._runs.get(str(session.id), {})
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
        children = self._runs.get(str(session.id), {}).get("external_children", {})
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
        root_context = self._runs.get(root_key, {})
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
        children = self._runs.get(str(session.id), {}).get("external_children", {})
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
        """Inherit explicit stop intent from any ancestor still in the tree."""
        key = str(session.id)
        cursor = str(session.parent_id) if session.parent_id else None
        chain = [key]
        seen = {key}
        while cursor and cursor not in seen:
            chain.append(cursor)
            seen.add(cursor)
            if cursor in self._abort_requested:
                self._abort_requested.update(chain)
                return True
            cursor = self._run_parents.get(cursor)
        return key in self._abort_requested

    def _parent_stop_in_progress(self, session: HarnessSession) -> bool:
        """True when user stop must end *session* instead of returning a tool error."""
        if self._user_abort_requested(session):
            return True
        parent_task = self._tasks.get(str(session.id))
        return parent_task is not None and bool(parent_task.cancelling())

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
        context = self._runs.get(str(session.id), {})
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
        self._runs.get(key, {})["finalizing"] = True
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
                context = self._runs.get(key, {})
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
            self._runs.pop(key, None)
            self._event_locks.pop(key, None)
            self._abort_requested.discard(key)
            # Keep task ownership until all persistence/emission awaits have finished.
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)
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

    @staticmethod
    def _run_workspace(workspace_id):  # type: ignore[no-untyped-def]
        """Return the Workspace row for MCP snapshot resolution.

        Uses the repository (loaded ``runner`` relation + prefetched
        credentials) so the runtime layer never issues ORM queries.
        """
        from apps.runners.repositories import WorkspaceRepository

        return WorkspaceRepository.get_by_id(workspace_id)

    @staticmethod
    def _prepare_mcp_snapshot_for_run(session, organization_id):  # type: ignore[no-untyped-def]
        """Build the effective plugin snapshot + resolve credentials once.

        Runs in the sync ORM context (called via ``sync_to_async``):
        credential gaps raise here before any provider call. Always
        returns a :class:`PreparedPluginRuntime` (possibly empty) so the
        run path never issues a second workspace query. ``setup`` reuses
        the snapshot + decrypted requirement map without another
        ORM/decrypt round.
        """
        from apps.plugins import runtime as plugin_runtime
        from apps.plugins.runtime_snapshot import PreparedPluginRuntime

        workspace = HarnessService._run_workspace(session.workspace_id)
        snapshot = plugin_runtime.build_workspace_plugin_snapshot(
            workspace=workspace, org_id=organization_id
        )
        if not snapshot.plugins:
            return PreparedPluginRuntime(snapshot=snapshot)
        resolved = plugin_runtime.resolve_runtime_credentials(
            snapshot, workspace=workspace
        )
        oauth_credentials = plugin_runtime.resolve_runtime_oauth_credentials(
            snapshot, workspace=workspace
        )
        return PreparedPluginRuntime(
            snapshot=snapshot,
            plaintexts=dict(resolved or {}),
            oauth_credentials=oauth_credentials,
        )

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
        lock = self._event_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._event_locks[session_id] = lock
        return lock

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
                self._runs.get(str(session.id), {}).get("message_id", "") or ""
            )
        if not user_message_id:
            user_message_id = str(
                self._runs.get(str(session.id), {}).get("user_message_id", "") or ""
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
            run_ctx = self._runs.get(str(data.get("session_id", "")), {})
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
        context = self._runs.get(str(session.id), {})
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
        context = self._runs.get(str(session.id), {})
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
        """Create a child session, run it to completion, return tool output.

        Model/effort routing comes from AgentConfig: custom mode uses the
        fixed config model/effort; inherit mode reuses the parent run model
        (``args.model_override`` always wins). Effort strategies
        lowest/medium/highest resolve against the catalog efforts of the
        inherited model; ``inherit`` reuses the parent reasoning effort.

        ``args.task_id`` resumes a previous child of the same parent
        (OpenCode parity): the follow-up prompt runs on the same child
        session with its persisted history instead of creating a fresh
        one. Unknown/missing ``task_id`` values fall back to a fresh
        child; foreign or busy children are rejected with ``ToolError``.
        A ``subagent_type`` mismatch on resume is ignored — the stored
        child agent keeps running.
        """
        from .tools.base import ToolError
        from .tools.subagents import TASK_OUTPUT_MAX_CHARS, build_task_result

        if ctx.depth >= ctx.max_depth:
            raise ToolError(
                f"Subagent depth limit reached (depth={ctx.depth}, "
                f"max_depth={ctx.max_depth}); nested task calls are not allowed.",
                tool="task",
            )
        agent = (args.agent or args.subagent_type or "general").strip().lower()
        configs = dict(agent_configs or {}) or self._agent_configs_map(organization_id)
        row = configs.get(agent, {})
        inherit = bool(row.get("inherit_model", True))
        fixed_model = str(row.get("model") or "").strip()
        codex_parent = getattr(parent, "harness_id", "native") == "codex"
        effort_normalizer = (
            (lambda value: (value or "").strip().lower())
            if codex_parent
            else normalize_reasoning_effort
        )
        fixed_effort = effort_normalizer(str(row.get("effort") or ""))
        strategy = str(row.get("effort_strategy") or "inherit").strip() or "inherit"
        override = (args.model_override or "").strip()
        if inherit:
            model = (
                override or (getattr(ctx, "model", "") or "") or parent.model or ""
            ).strip()
        else:
            model = (
                override
                or fixed_model
                or (getattr(ctx, "model", "") or "")
                or parent.model
                or ""
            ).strip()
        if not model:
            raise ToolError(
                "No model available for subagent run",
                tool="task",
            )
        if codex_parent:
            try:
                model, _ = await sync_to_async(
                    self.session_settings.validate_codex_config
                )(
                    parent,
                    model,
                    "",
                    account_ids=self._provider_turn_context(parent).get("account_ids"),
                )
            except (ValueError, AuthenticationError, NotFoundError) as exc:
                raise ToolError(str(exc), tool="task") from exc
        parent_ctx = self._runs.get(str(parent.id), {})
        message_id = parent_ctx.get("message_id")
        if not message_id:
            raise ToolError(
                "Parent assistant message missing for subagent run",
                tool="task",
            )
        parent_assistant = await sync_to_async(self.messages.get_required)(message_id)
        child = await self._resolve_resume_child(parent, args.task_id)
        resumed = child is not None
        if resumed:
            agent = (
                child.agent_name or agent or "general"
            ).strip().lower() or "general"
        validate_subagent_delegation(parent.agent_name, agent)
        if inherit:
            if strategy in ("lowest", "medium", "highest"):
                if codex_parent:
                    from .engines.service import EngineCatalogService

                    models = await sync_to_async(EngineCatalogService().list_models)(
                        "codex",
                        organization_id,
                        parent.created_by_id,
                        account_ids=self._provider_turn_context(parent).get(
                            "account_ids"
                        ),
                    )
                    catalog_efforts = next(
                        (list(m.reasoning_efforts) for m in models if m.id == model), []
                    )
                else:
                    catalog_efforts = self._catalog_efforts_for_model(
                        organization_id,
                        model,
                        self._provider_turn_context(parent).get("initiated_by_id"),
                        dict(
                            self._provider_turn_context(parent).get("account_ids") or {}
                        ),
                    )
                from .agents.effort_strategy import resolve_strategy_effort

                child_effort = resolve_strategy_effort(catalog_efforts, strategy)
            else:
                child_effort = effort_normalizer(parent.reasoning_effort or "")
        else:
            child_effort = fixed_effort
        if resumed:
            # Resume keeps the stored child routing: model_override still
            # wins, otherwise the existing child model/effort continue.
            assert child is not None
            if override:
                if codex_parent:
                    await sync_to_async(
                        self.session_settings.validate_codex_config
                    )(
                        child,
                        override,
                        child.reasoning_effort,
                        account_ids=self._provider_turn_context(parent).get(
                            "account_ids"
                        ),
                    )
                child = await sync_to_async(self.sessions.set_model)(child, override)
                model = override
            else:
                model = (child.model or model or "").strip()
                if not model:
                    raise ToolError(
                        "No model available for subagent run",
                        tool="task",
                    )
            child_effort = effort_normalizer(child.reasoning_effort or "")
        else:
            child = await sync_to_async(self.create_session)(
                workspace_id=parent.workspace_id,
                organization_id=parent.organization_id,
                prompt=args.prompt,
                agent_name=agent,
                mode=parent.mode,
                model=model,
                reasoning_effort=child_effort,
                title=args.description[:255],
                parent_id=parent.id,
                user_id=getattr(parent, "created_by_id", None),
                skill_ids=list(parent.skill_ids or []),
            )
        await self._on_runner_event(
            parent,
            parent_assistant,
            {
                "type": "subtask_started",
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "description": args.description,
                "model": child.model or "",
                "reasoning_effort": child.reasoning_effort or "",
            },
        )
        status = "completed"
        assistant: HarnessMessage | None = None
        child_task: asyncio.Task[None] | None = None
        try:
            assistant = await self.start_run(
                child,
                args.prompt,
                organization_id=organization_id,
                workspace_id=str(parent.workspace_id),
                max_depth=ctx.max_depth,
                user_id=getattr(parent, "created_by_id", None),
            )
            child_task = self._tasks.get(str(child.id))
            if child_task is not None:
                # Cancel execution explicitly below, not an already-settling owner.
                await asyncio.shield(child_task)
            # A stopped parent must not treat the child outcome as a tool
            # error the model can recover from by launching another subagent.
            if self._parent_stop_in_progress(parent):
                raise asyncio.CancelledError()
            assistant = await sync_to_async(self.messages.get_required)(assistant.id)
            output = assistant.content or ""
            if assistant.finish in ("error", "aborted"):
                error = assistant.error or "Subagent run interrupted"
                if output:
                    error += "\n\nPartial output:\n" + output[:TASK_OUTPUT_MAX_CHARS]
                raise ToolError(error, tool="task")
        except asyncio.CancelledError:
            tracked = child_task or self._tasks.get(str(child.id))
            if tracked is not None and not tracked.done():
                if not tracked.cancelling() and not self._runs.get(
                    str(child.id), {}
                ).get("finalizing"):
                    tracked.cancel()
                try:
                    await asyncio.shield(tracked)
                except (asyncio.CancelledError, Exception):
                    pass
            # A task cancelled before entry installs a replacement finalizer.
            # Await that owner without cancelling it before reading its outcome.
            finalizer = self._tasks.get(str(child.id))
            if finalizer is not None and finalizer is not tracked:
                with anyio.CancelScope(shield=True):
                    while not finalizer.done():
                        try:
                            await asyncio.shield(finalizer)
                        except asyncio.CancelledError:
                            # A parent stop can arrive while this child is already
                            # settling. Finish the join before propagating it below.
                            continue
            # Awaiting a cancelled child does not cancel this tool task. Only
            # cancellation of the waiting task/parent should stop the whole run.
            current = asyncio.current_task()
            parent_task = self._tasks.get(str(parent.id))
            if (
                (current is not None and current.cancelling())
                or str(parent.id) in self._abort_requested
                or (parent_task is not None and parent_task.cancelling())
            ):
                raise
            error = "Subagent run aborted"
            if assistant is not None:
                await sync_to_async(self.messages.refresh, thread_sensitive=True)(
                    assistant
                )
                error = assistant.error or error
                if assistant.content:
                    error += (
                        "\n\nPartial output:\n"
                        + assistant.content[:TASK_OUTPUT_MAX_CHARS]
                    )
            log.warning(
                "harness_subagent_interrupted",
                session_id=str(parent.id),
                child_session_id=str(child.id),
                subtask_id=subtask_id,
                agent=agent,
                phase="startup" if assistant is None else "run",
            )
            await self._on_runner_event(
                parent,
                parent_assistant,
                {
                    "type": "subtask_finished",
                    "subtask_id": subtask_id,
                    "child_session_id": str(child.id),
                    "agent": agent,
                    "status": "aborted",
                    "summary": error[:500],
                },
            )
            raise ToolError(
                f"Subagent '{agent}' failed (task_id: {child.id}): {error}",
                tool="task",
            )
        except Exception as exc:
            await self._on_runner_event(
                parent,
                parent_assistant,
                {
                    "type": "subtask_finished",
                    "subtask_id": subtask_id,
                    "child_session_id": str(child.id),
                    "agent": agent,
                    "status": "error",
                    "summary": str(exc)[:500],
                },
            )
            raise ToolError(
                f"Subagent '{agent}' failed (task_id: {child.id}): {exc}",
                tool="task",
            ) from exc
        await self._on_runner_event(
            parent,
            parent_assistant,
            {
                "type": "subtask_finished",
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "agent": agent,
                "status": status,
                "summary": output[:500],
            },
        )
        return await build_task_result(
            str(child.id),
            output,
            ctx,
            agent=agent,
            metadata={
                "subtask_id": subtask_id,
                "child_session_id": str(child.id),
                "task_id": str(child.id),
                "agent": agent,
                "status": status,
                "resumed": resumed,
            },
        )

    async def _resolve_resume_child(
        self, parent: HarnessSession, task_id: str | None
    ) -> HarnessSession | None:
        """Return the resumable child for *task_id* or ``None`` for fresh.

        A child resumes only when it exists and belongs to the same
        parent, workspace, and organization. Foreign or busy children
        are rejected so one parent can neither hijack nor queue onto
        another parent's subagent (sequential resume only, no
        update-while-running).
        """
        from .tools.base import ToolError

        raw = (task_id or "").strip()
        if not raw:
            return None
        try:
            child_id = uuid.UUID(raw)
        except (ValueError, TypeError, AttributeError):
            return None
        child = await sync_to_async(self.sessions.get_by_id)(child_id)
        if child is None:
            # OpenCode parity: an unknown task_id starts a fresh child.
            return None
        if (
            child.parent_id != parent.id
            or child.workspace_id != parent.workspace_id
            or child.organization_id != parent.organization_id
        ):
            raise ToolError(
                f"task_id '{raw}' does not belong to this session",
                tool="task",
            )
        if self.is_running(child.id):
            raise ToolError(
                f"Subagent '{raw}' is still running; wait for it to finish "
                "before resuming it",
                tool="task",
            )
        return child


def _forked_title(title: str) -> str:
    """Return the fork title (inspired by OpenCode ``getForkedTitle``)."""
    import re

    match = re.match(r"^(.+) \(fork #(\d+)\)$", (title or "").strip())
    if match:
        base = match.group(1)
        return f"{base} (fork #{int(match.group(2)) + 1})"
    return f"{(title or '').strip()} (fork #1)"


_default_harness_service: HarnessService | None = None


def create_default_harness_service() -> HarnessService:
    """Build the production service with runner-backed workspace access.

    REST uses :func:`get_harness_service` so abort, permissions,
    and in-flight runs share one task registry.
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


def get_harness_service() -> HarnessService:
    """Return the process-wide HarnessService singleton."""
    global _default_harness_service
    if _default_harness_service is None:
        _default_harness_service = create_default_harness_service()
    return _default_harness_service


def reset_default_harness_service() -> None:
    """Drop the process singleton (tests)."""
    global _default_harness_service
    _default_harness_service = None


#: Backwards-friendly alias used by tests for the double-run conflict.
HarnessBusyError = ConflictError
