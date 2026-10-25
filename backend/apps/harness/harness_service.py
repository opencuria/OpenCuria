"""
Harness session lifecycle service (M6, additive).

``HarnessService`` owns the persistent side of agent runs while
``HarnessRunner`` (M4/M5) owns the streaming loop. The service:

- creates/gets/lists ``HarnessSession`` rows per workspace,
- persists a user message + assistant message shell on run start,
- builds message history from the DB (not from in-memory parameters),
- starts ``HarnessRunner.run`` as an ``asyncio.Task`` tracked per
  session (one active run per session; a second start is rejected
  with :class:`ConflictError`, surfaced as HTTP 409),
- writes runner events back to ``HarnessMessage``/``HarnessPart`` rows
  (text deltas appended, tool states transitioned, step-finish with
  cost/tokens) and forwards them to subscribed frontend clients via
  ``emit_to_frontend``,
- aborts runs by cancelling the task and marking message/parts aborted,
- binds the M3 permission flow: ask gates persist a
  ``PermissionRequest`` and resolve through ``PermissionService``.

No ORM outside repositories. No real LLM calls (provider injected).
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Awaitable, Callable
from functools import partial
from itertools import islice
from typing import Any

import anyio
import structlog
from asgiref.sync import ThreadSensitiveContext, async_to_sync, sync_to_async

from apps.runners.services import get_runner_service
from apps.runners.services.infra import realtime
from apps.skills.services import SkillService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from .agents.definitions import get_agent
from .compaction import CHECKPOINT_PREFIX
from .images import (
    build_tool_message_content,
    hydrate_user_messages,
    select_persisted_tool_attachments,
)
from .models import (
    HarnessMessage,
    HarnessPart,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
)
from .permissions.service import PermissionService
from .providers.base import ChatOptions, LLMMessage, ProviderAdapter, Usage
from .providers.model_ref import namespaced_model_id, parse_model_ref
from .providers.models_catalog import normalize_reasoning_effort
from .repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
    QuestionRequestRepository,
)
from .run_events import publish_assistant_run_finished
from .runner import HarnessRunner, RunOptions
from .small_model_service import build_small_model_runner
from .steering import NO_ACTIVE_RUN_CODE, SteerEntry, SteeringChannel
from .todos.repositories import TodoRepository
from .todos.service import TodoService
from .tools.subagents import TaskArgs, validate_subagent_delegation
from .tools.todos import TodoWriteTool
from .tools.truncate import truncate_tool_output

log = structlog.get_logger(__name__)

EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]

# Bound raw tool-output retention while amortizing queries and executor hops.
HISTORY_PART_BATCH_SIZE = 8


def _event_tool_attachments(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return sanitizable attachment dicts from a tool_completed event."""
    attachments = event.get("attachments", [])
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


def _tool_part_attachments(part: HarnessPart) -> list[dict[str, Any]]:
    """Return persisted attachment dicts from a tool ``HarnessPart``."""
    meta = getattr(part, "meta", None) or {}
    if not isinstance(meta, dict):
        return []
    attachments = meta.get("attachments", [])
    if not isinstance(attachments, list):
        return []
    return [item for item in attachments if isinstance(item, dict)]


def _socket_text(value: object, *, limit: int) -> str:
    """Bound runner-controlled text included in a live socket projection."""
    return str(value or "")[:limit]


def _tool_socket_identity(
    event: dict[str, Any], part: HarnessPart, *, state: str
) -> dict[str, str]:
    """Project a tool event to identifiers and a small display title only."""
    return {
        "tool": _socket_text(
            event.get("tool") or (part.input or {}).get("tool", ""), limit=100
        ),
        "title": _socket_text(event.get("title") or part.title, limit=240),
        "call_id": _socket_text(event.get("call_id") or part.call_id, limit=255),
        "state": state,
    }


FRONTEND_EVENT_PART = "harness.part_updated"
FRONTEND_EVENT_PERMISSION = "harness.permission_required"
FRONTEND_EVENT_QUESTION = "harness.question_required"
FRONTEND_EVENT_STATUS = "harness.session_status"
FRONTEND_EVENT_TODO = "harness.todo_updated"
FRONTEND_EVENT_SUMMARY = "harness.session_summary"
FRONTEND_EVENT_SUBTASK_STARTED = "harness.subtask_started"
FRONTEND_EVENT_SUBTASK_FINISHED = "harness.subtask_finished"
FRONTEND_EVENT_CONVERSATIONS_CHANGED = "harness.conversations_changed"
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
        self.todos = todos or TodoRepository
        self.permissions = permissions or PermissionService()
        self.skills = skills or SkillService()
        self._emit = emit
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
        self._pending_permissions: dict[str, asyncio.Future[str]] = {}
        self._pending_questions: dict[str, asyncio.Future[list[Any]]] = {}
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
                from .provider_turns import unregister_revocation_listener

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
        from .provider_turns import unregister_revocation_listener

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
                    await self._reject_tree_gates(
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
        self.sessions.ensure_interactions_available(workspace_id)
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
        if engine_id in {"claude", "codex"} and requested == "computeruse":
            raise ValueError(f"{engine_id} engine does not support Agent-S computeruse")
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
            "title": title or _title_from_prompt(prompt),
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
            self._emit_conversations_changed_sync(session.workspace_id)
        return session

    def get_session(self, session_id: uuid.UUID) -> HarnessSession:
        """Return a session or raise NotFoundError."""
        session = self.sessions.get_by_id(session_id)
        if session is None:
            raise NotFoundError("HarnessSession", str(session_id))
        return session

    def ensure_user_promptable(self, session: HarnessSession) -> None:
        """Raise when users must not send follow-up prompts to *session*.

        Subagent child sessions (``parent_id`` set) are launched internally
        and are not a user-addressable chat. ``start_run`` itself stays
        allowed so the task tool can start the child run.
        """
        if session.parent_id is not None:
            raise ValueError("Cannot send messages to a subagent session")

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

    def set_mode(self, session_id: uuid.UUID, mode: str) -> HarnessSession:
        """Persist a mode change (plan|build) and align primary agent_name."""
        normalized = (mode or "").strip().lower()
        if normalized not in ("plan", "build"):
            raise ValueError(f"Invalid mode '{mode}'; expected plan|build")
        session = self.get_session(session_id)
        self.sessions.ensure_interactions_available(session.workspace_id)
        updated = self.sessions.set_mode(session, normalized)
        if updated.parent_id is None:
            self._emit_conversations_changed_sync(updated.workspace_id)
        return updated

    @staticmethod
    def _codex_provider_for_session(session) -> str:
        """Read the immutable non-secret provider binding."""
        from .engines.codex.catalog import codex_provider

        return codex_provider(session.model, session.engine_state)

    def _validate_codex_session_config(
        self, session, model, effort, *, user_id=None, account_ids=None
    ) -> tuple[str, str]:
        """Validate the candidate without mutating model/provider binding."""
        from .engines.codex.catalog import codex_provider
        from .engines.service import EngineCatalogService

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
        session = self.get_session(session_id)
        self.sessions.ensure_interactions_available(session.workspace_id)
        value = (model or "").strip()
        if getattr(session, "harness_id", "native") == "claude":
            from .engines.catalog import normalize_claude_model

            value = normalize_claude_model(value)
        if getattr(session, "harness_id", "native") == "codex":
            value, default_effort = self._validate_codex_session_config(
                session, value, ""
            )
            # Model changes within a provider remain possible when the old
            # model supported an effort (e.g. ultra) that the new one does not.
            try:
                self._validate_codex_session_config(
                    session, value, session.reasoning_effort
                )
            except ValueError:
                session = self.sessions.set_reasoning_effort(session, default_effort)
        return self.sessions.set_model(session, value)

    def set_reasoning_effort(
        self, session_id: uuid.UUID, reasoning_effort: str
    ) -> HarnessSession:
        """Persist a reasoning-effort override for subsequent runs."""
        session = self.get_session(session_id)
        self.sessions.ensure_interactions_available(session.workspace_id)
        if getattr(session, "harness_id", "native") == "claude":
            from .engines.catalog import normalize_claude_effort

            effort = normalize_claude_effort(reasoning_effort)
        elif getattr(session, "harness_id", "native") == "codex":
            _, effort = self._validate_codex_session_config(
                session, session.model, reasoning_effort
            )
        else:
            effort = normalize_reasoning_effort(reasoning_effort)
        return self.sessions.set_reasoning_effort(session, effort)

    def update_title(self, session_id: uuid.UUID, title: str) -> HarnessSession:
        """Rename a session (title only)."""
        session = self.get_session(session_id)
        self.sessions.ensure_interactions_available(session.workspace_id)
        normalized = (title or "").strip()
        if not normalized:
            raise ValueError("title must not be empty")
        updated = self.sessions.set_title(session, normalized)
        if updated.parent_id is None:
            self._emit_conversations_changed_sync(updated.workspace_id)
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
        session = self.get_session(session_id)
        self.sessions.ensure_interactions_available(session.workspace_id)
        normalized = SkillService.normalize_ids(skill_ids)
        if normalized:
            self.skills.resolve_bodies(
                normalized,
                user_id=user_id,
                organization_id=organization_id,
            )
        return self.sessions.set_skill_ids(session, normalized)

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
        await sync_to_async(self.sessions.ensure_interactions_available)(
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
            await self._emit_conversations_changed(session.workspace_id)

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
        await sync_to_async(self.sessions.ensure_interactions_available)(
            session.workspace_id
        )
        self.ensure_user_promptable(session)
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
                    if session.harness_id == "codex" else {}
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
        await self._emit_conversations_changed(forked.workspace_id)
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
        await sync_to_async(self.sessions.ensure_interactions_available)(
            session.workspace_id
        )
        self.ensure_user_promptable(session)
        if self.is_running(session.id):
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        if getattr(session, "harness_id", "native") != "native":
            owner_id = getattr(session, "created_by_id", None)
            if user_id is None or owner_id is None or str(user_id) != str(owner_id):
                raise AuthenticationError(
                    "Only the session owner may edit an external conversation"
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
                self._validate_codex_session_config
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
        for pending, request_ids in (
            (self._pending_permissions, permission_ids),
            (self._pending_questions, question_ids),
        ):
            for request_id in request_ids:
                future = pending.pop(str(request_id), None)
                if future is not None and not future.done():
                    future.cancel()
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

    def list_conversations(
        self,
        *,
        organization_id: uuid.UUID,
        workspace_ids: list[uuid.UUID],
    ) -> list[dict[str, Any]]:
        """List root sessions across owned workspaces (conversations feed)."""
        sessions = self.sessions.list_for_workspaces(workspace_ids)
        unread_map = self.unread_for_sessions(sessions)
        attention_map = self.attention_for_sessions(sessions)
        return [
            {
                "session_id": str(session.id),
                "workspace_id": str(session.workspace_id),
                "workspace_name": session.workspace_name or "",
                "title": session.title or "",
                "status": session.status,
                "mode": session.mode,
                "agent_name": session.agent_name,
                "harness_id": session.harness_id or "native",
                "model": session.model or "",
                "reasoning_effort": session.reasoning_effort or "",
                "unread": unread_map.get(session.id, False),
                "manual_unread": session.manual_unread_at is not None,
                "needs_attention": attention_map.get(session.id, {}).get(
                    "needs_attention", False
                ),
                "attention_kind": attention_map.get(session.id, {}).get(
                    "attention_kind", ""
                ),
                "updated_at": session.updated_at.isoformat(),
                "last_message_at": (
                    session.last_message_at or session.created_at
                ).isoformat(),
            }
            for session in sessions
        ]

    def mark_session_read(self, session_id: uuid.UUID) -> HarnessSession:
        """Persist that the user opened a harness session."""
        session = self.get_session(session_id)
        updated = self.sessions.mark_read(session)
        if updated.parent_id is None:
            self._emit_conversations_changed_sync(updated.workspace_id)
        return updated

    def mark_session_unread(self, session_id: uuid.UUID) -> HarnessSession:
        """Persist that the user explicitly marked a session unread."""
        session = self.get_session(session_id)
        updated = self.sessions.mark_unread(session)
        if updated.parent_id is None:
            self._emit_conversations_changed_sync(updated.workspace_id)
        return updated

    def dismiss_notice(
        self, session_id: uuid.UUID, message_id: uuid.UUID
    ) -> HarnessMessage:
        """Dismiss the stopped/failed notice of one session message."""
        session = self.get_session(session_id)
        message = self.messages.get_by_id(message_id)
        if message is None or message.session_id != session.id:
            raise NotFoundError("HarnessMessage", str(message_id))
        return self.messages.dismiss_notice(message)

    def unread_for_sessions(
        self, sessions: list[HarnessSession]
    ) -> dict[uuid.UUID, bool]:
        """Return unread flags keyed by session id."""
        if not sessions:
            return {}
        latest_assistant_at = self.messages.latest_assistant_completed_at_by_session(
            [session.id for session in sessions]
        )
        return {
            session.id: self._is_conversation_unread(
                session,
                latest_assistant_at.get(session.id),
            )
            for session in sessions
        }

    def is_session_unread(self, session: HarnessSession) -> bool:
        """Return whether a single session has unread assistant work."""
        return self.unread_for_sessions([session]).get(session.id, False)

    @staticmethod
    def _is_conversation_unread(
        session: HarnessSession,
        latest_assistant_completed_at,
    ) -> bool:
        """Return True when the session is manually unread or has new work."""
        if session.manual_unread_at is not None:
            return True
        if session.status != HarnessSessionStatus.IDLE:
            return False
        if latest_assistant_completed_at is None:
            return False
        if session.last_read_at is None:
            return True
        return latest_assistant_completed_at > session.last_read_at

    def attention_for_sessions(
        self, sessions: list[HarnessSession]
    ) -> dict[uuid.UUID, dict[str, Any]]:
        """Return pending-gate attention flags keyed by session id.

        Child (subagent) gates count toward every ancestor in *sessions*,
        so a root conversation is marked when a descendant is waiting.
        """
        if not sessions:
            return {}
        workspace_ids = list({session.workspace_id for session in sessions})
        id_parents = self.sessions.list_id_parent_for_workspaces(workspace_ids)
        children_map: dict[uuid.UUID, list[uuid.UUID]] = {}
        all_ids: list[uuid.UUID] = []
        for session_id, parent_id in id_parents:
            all_ids.append(session_id)
            if parent_id is None:
                continue
            children_map.setdefault(parent_id, []).append(session_id)
        pending_perm_ids = {
            row.session_id
            for row in self.permissions.requests.list_pending_for_sessions(all_ids)
        }
        pending_question_ids = {
            row.session_id
            for row in QuestionRequestRepository.list_pending_for_sessions(all_ids)
        }
        result: dict[uuid.UUID, dict[str, Any]] = {}
        for session in sessions:
            tree = _descendant_ids(session.id, children_map)
            has_permission = any(sid in pending_perm_ids for sid in tree)
            has_question = any(sid in pending_question_ids for sid in tree)
            if has_permission and has_question:
                kind = "both"
            elif has_permission:
                kind = "permission"
            elif has_question:
                kind = "question"
            else:
                kind = ""
            result[session.id] = {
                "needs_attention": bool(kind),
                "attention_kind": kind,
            }
        return result

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
                self._validate_codex_session_config(
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
        if (session.agent_name or "").strip().lower() == "computeruse":
            # Explicit grounding models must resolve to a connected
            # provider; empty grounding falls back to the main model
            # (validated above). No network check — connection only.
            from .services import AgentSConfigService

            grounding = str(
                AgentSConfigService()
                .get_or_default(organization_id)
                .get("grounding_model")
                or ""
            ).strip()
            if grounding and grounding.strip() != model.strip():
                config_service.provider_connected_for_model(
                    organization_id,
                    grounding,
                    user_id=account_user_id,
                    account_ids=account_ids,
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

    def list_pending_permissions(
        self, session_id: uuid.UUID, *, include_descendants: bool = False
    ) -> list[dict[str, Any]]:
        """Return pending permission gates for *session_id* as dicts.

        When *include_descendants* is true, also include pending gates of
        child (and nested) sessions so a parent parts fetch can surface
        subagent asks.
        """
        self.get_session(session_id)
        session_ids = (
            self.sessions.list_descendant_ids(session_id)
            if include_descendants
            else [session_id]
        )
        rows = self.permissions.requests.list_pending_for_sessions(session_ids)
        names = {
            row.id: row.agent_name for row in self.sessions.list_by_ids(session_ids)
        }
        from .permissions.scopes import permission_display_action

        return [
            {
                "request_id": str(row.id),
                "session_id": str(row.session_id),
                "workspace_id": str(row.workspace_id) if row.workspace_id else "",
                "tool": row.tool,
                "pattern": permission_display_action(row.pattern),
                "title": row.title or "",
                "call_id": row.call_id or "",
                "status": "pending",
                "agent_name": names.get(row.session_id, ""),
            }
            for row in rows
        ]

    def list_pending_questions(
        self, session_id: uuid.UUID, *, include_descendants: bool = False
    ) -> list[dict[str, Any]]:
        """Return pending question gates for *session_id* as dicts.

        When *include_descendants* is true, also include pending gates of
        child (and nested) sessions.
        """
        self.get_session(session_id)
        session_ids = (
            self.sessions.list_descendant_ids(session_id)
            if include_descendants
            else [session_id]
        )
        rows = QuestionRequestRepository.list_pending_for_sessions(session_ids)
        names = {
            row.id: row.agent_name for row in self.sessions.list_by_ids(session_ids)
        }
        return [
            {
                "request_id": str(row.id),
                "session_id": str(row.session_id),
                "workspace_id": str(row.workspace_id) if row.workspace_id else "",
                "questions": list(row.questions or []),
                "call_id": row.call_id or "",
                "status": "pending",
                "agent_name": names.get(row.session_id, ""),
            }
            for row in rows
        ]

    # -- run orchestration --------------------------------------------------

    def is_running(self, session_id: uuid.UUID) -> bool:
        """Return True when a run task is active for *session_id*."""
        key = str(session_id)
        task = self._tasks.get(key) or self._admissions.get(key)
        return task is not None and not task.done()

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
        if engine_id != "native":
            owner_id = getattr(session, "created_by_id", None)
            if (
                run_user_id is None
                or owner_id is None
                or str(run_user_id) != str(owner_id)
            ):
                raise AuthenticationError(
                    "Only the session owner may use its external engine connection"
                )
        if engine_id == "codex":
            user_id = run_user_id
        self._remember_run(session)
        # Parent stop already covers this session: do not spawn another run.
        if self._user_abort_requested(session):
            self._forget_run(str(session.id))
            raise asyncio.CancelledError()
        await sync_to_async(self.sessions.ensure_interactions_available)(
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
                session = await sync_to_async(self.update_skill_ids)(
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
        admitted = await sync_to_async(self.sessions.reserve_workspace_run)(
            session.id, scheduled=scheduled
        )
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
            prior_user_messages = await sync_to_async(
                self.messages.count_for_session
            )(session.id, role="user")
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
            history = await self._build_history(
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
                from .provider_turns import register_revocation_listener

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
            if (
                engine_id == "native"
                and _edit_message_id is None
                and session.parent_id is None
                and prior_user_messages == 0
                and provider_turn_id
            ):
                title_holder = f"title:{uuid.uuid4()}"
                if await sync_to_async(self._provider_turn_service.add_hold)(
                    provider_turn_id, title_holder
                ):
                    title_task = self._spawn_background(
                        self._generate_title(
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
            monitor_task = run_context.get("provider_monitor_task")
            if monitor_task is not None and not monitor_task.done():
                monitor_task.cancel()
                watched = run_context.get("provider_revocation_accounts")
                event = run_context.get("provider_revocation_event")
                if watched and event is not None:
                    from .provider_turns import unregister_revocation_listener

                    unregister_revocation_listener(watched, event=event)
            durable_owner = run_context.get("ownership")
            if durable_owner is not None:
                try:
                    await durable_owner.stop_heartbeat()
                    await durable_owner.begin_close()
                    await durable_owner.finish(
                        status=(
                            HarnessRunStatus.INTERRUPTED
                            if user_abort
                            else HarnessRunStatus.ERROR
                        ),
                        error="Run admission failed",
                        cleanup_confirmed=True,
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
        await sync_to_async(self.sessions.ensure_interactions_available)(
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
        self.ensure_user_promptable(session)
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
            await self._close_running_stream_parts(session)
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

    async def _reject_tree_gates(self, keys: set[str]) -> None:
        """Reject permission and question gates for every session in *keys*."""
        ids: list[uuid.UUID] = []
        for key in keys:
            try:
                ids.append(uuid.UUID(key))
            except ValueError:
                continue
        if not ids:
            return
        rows = await sync_to_async(self.sessions.list_by_ids)(ids)
        for row in rows:
            await self._reject_pending_user_gates(row)

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
        await self._reject_tree_gates(closure)
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

    async def resolve_permission(
        self, *, session: HarnessSession, request_id: uuid.UUID, response: str
    ) -> dict[str, Any]:
        """Resolve a permission request via the M3 service + wake the loop."""
        record = await sync_to_async(self.permissions.requests.get_by_id)(request_id)
        if record is None or record.status != "pending":
            raise NotFoundError("PermissionRequest", str(request_id))
        if str(record.session_id) != str(session.id):
            raise NotFoundError("PermissionRequest", str(request_id))
        await sync_to_async(self.sessions.ensure_interactions_available)(
            session.workspace_id
        )
        result = await sync_to_async(self.permissions.resolve)(request_id, response)
        future = self._pending_permissions.pop(str(request_id), None)
        if future is not None and not future.done():
            future.set_result(result.decision)
        await self._emit_frontend(
            FRONTEND_EVENT_PERMISSION,
            {
                **(await self._gate_ids(session)),
                "request_id": str(request_id),
                "decision": result.decision,
                "remember": result.remember,
            },
            str(session.workspace_id),
        )
        if str(response).strip().lower() == "always":
            await self._resolve_sibling_permissions(
                session=session,
                tool=record.tool,
                skip_id=request_id,
                decision=result.decision,
                remember=result.remember,
            )
        return {"decision": result.decision, "remember": result.remember}

    async def resolve_question(
        self,
        *,
        session: HarnessSession,
        question_id: uuid.UUID,
        answers: list[Any],
        reject: bool = False,
    ) -> dict[str, Any]:
        """Resolve a question request and resume the waiting tool."""
        record = await sync_to_async(QuestionRequestRepository.get_by_id)(question_id)
        if record is None or record.status != "pending":
            raise NotFoundError("QuestionRequest", str(question_id))
        if str(record.session_id) != str(session.id):
            raise NotFoundError("QuestionRequest", str(question_id))
        await sync_to_async(self.sessions.ensure_interactions_available)(
            session.workspace_id
        )
        status = "rejected" if reject else "answered"
        await sync_to_async(QuestionRequestRepository.resolve)(
            record,
            answers=list(answers or []),
            status=status,
        )
        future = self._pending_questions.pop(str(question_id), None)
        resumed = False
        if future is not None and not future.done():
            if reject:
                future.set_exception(ValueError("Question rejected by user"))
            else:
                future.set_result(list(answers or []))
            resumed = True
        if resumed:
            log.info(
                "harness_question_resumed",
                session_id=str(session.id),
                request_id=str(question_id),
                status=status,
            )
        else:
            # No live waiter: the run task is gone (abort/cancel race or
            # process restart dropped the in-memory future). The DB row is
            # resolved, but nothing will wake up — log loudly instead of
            # silently returning 200 while the session stays busy.
            log.warning(
                "harness_question_no_waiter",
                session_id=str(session.id),
                request_id=str(question_id),
                status=status,
                running=self.is_running(session.id),
            )
        await self._emit_frontend(
            FRONTEND_EVENT_QUESTION,
            {
                **(await self._gate_ids(session)),
                "request_id": str(question_id),
                "status": status,
            },
            str(session.workspace_id),
        )
        return {
            "request_id": str(question_id),
            "status": status,
            "resumed": resumed,
        }

    # -- internals ----------------------------------------------------------

    async def _build_history(
        self,
        session: HarnessSession,
        *,
        exclude_message_id: uuid.UUID | None = None,
        exclude_user_message_id: uuid.UUID | None = None,
        exclude_message_ids: list[uuid.UUID] | None = None,
    ) -> list[LLMMessage]:
        """Build provider history from persisted messages and parts.

        When a completed compaction part exists, only the checkpoint summary
        plus messages from ``tail_start_id`` onward are sent to the provider
        (OpenCode ``filterCompacted`` equivalent). Full history stays in the DB.
        """
        import json

        stored = await sync_to_async(self.messages.list_for_session)(session.id)
        excluded_ids = {
            exclude_message_id,
            exclude_user_message_id,
            *(exclude_message_ids or []),
        }
        stored = [message for message in stored if message.id not in excluded_ids]
        assistant_by_id = {
            message.id: message for message in stored if message.role == "assistant"
        }
        # Find the checkpoint without reading every assistant's full tool output.
        checkpoint = await sync_to_async(self.parts.get_latest_completed_compaction)(
            list(assistant_by_id)
        )
        compaction = (
            (assistant_by_id[checkpoint.message_id], checkpoint)
            if checkpoint is not None
            else None
        )
        del assistant_by_id
        checkpoint_summary = ""
        compaction_msg_id: uuid.UUID | None = None
        if compaction is not None:
            compaction_msg, compaction_part = compaction
            compaction_msg_id = compaction_msg.id
            checkpoint_summary = str(compaction_part.output or "").strip()
            stored = self._slice_messages_for_compaction(stored, compaction)
        history: list[LLMMessage] = []
        if checkpoint_summary:
            history.append(
                LLMMessage(
                    role="user",
                    content=f"{CHECKPOINT_PREFIX}\n{checkpoint_summary}",
                )
            )
        assistant_ids = iter(
            message.id for message in stored if message.role == "assistant"
        )
        parts_by_message: dict[uuid.UUID, list[HarnessPart]] = {}
        for message in stored:
            if message.role == "user":
                history.append(
                    LLMMessage(
                        role="user",
                        content=message.content or "",
                        message_id=str(message.id),
                    )
                )
                continue
            if message.role != "assistant":
                continue
            if not parts_by_message:
                parts_by_message = await sync_to_async(
                    self.parts.history_parts_by_message
                )(list(islice(assistant_ids, HISTORY_PART_BATCH_SIZE)))
            else:
                # Keep cancellation and scheduling points between assistant replays.
                await asyncio.sleep(0)
            parts = parts_by_message.pop(message.id)
            tool_parts = [
                part
                for part in parts
                if part.type == "tool"
                and part.state in ("completed", "error")
                and part.call_id
            ]
            assistant_content = self._assistant_history_content(
                message,
                parts,
                summary=checkpoint_summary if message.id == compaction_msg_id else "",
            )
            has_steers = any(part.type == "steer" and part.output for part in parts)
            if not tool_parts and not has_steers:
                history.append(
                    LLMMessage(
                        role="assistant",
                        content=assistant_content,
                        message_id=str(message.id),
                    )
                )
                continue
            # Rebuild conversational turns from ordered parts, not from the
            # flattened message.content. A response can contain text, tools,
            # more text, and more tools; provider replay must keep these
            # segments in the same order as the persisted timeline.
            tool_ids = {part.id for part in tool_parts}
            has_text_parts = any(part.type == "text" and part.output for part in parts)
            text = ""
            pending_calls: list[HarnessPart] = []
            if not has_text_parts and assistant_content:
                # Legacy messages without text parts only have a flattened
                # content field; place it before tools as the best effort.
                text = assistant_content

            def flush_segment() -> None:
                nonlocal text, pending_calls
                if not text and not pending_calls:
                    return
                calls: list[dict[str, Any]] = []
                for tool_part in pending_calls:
                    tool_input = dict(tool_part.input or {})
                    tool_name = str(
                        tool_input.get("tool", "") or tool_part.title or "tool"
                    )
                    raw_args = tool_input.get("arguments", "")
                    if isinstance(raw_args, dict):
                        raw_args = json.dumps(raw_args)
                    calls.append(
                        {
                            "id": tool_part.call_id,
                            "name": tool_name,
                            "arguments": str(raw_args or "{}"),
                        }
                    )
                history.append(
                    LLMMessage(
                        role="assistant",
                        content=text or None,
                        tool_calls=calls,
                        message_id=str(message.id),
                    )
                )
                for tool_part in pending_calls:
                    clipped = truncate_tool_output(tool_part.output or "")
                    attachments = select_persisted_tool_attachments(
                        _tool_part_attachments(tool_part)
                    )
                    history.append(
                        LLMMessage(
                            role="tool",
                            content=build_tool_message_content(
                                clipped.content, attachments
                            ),
                            tool_call_id=tool_part.call_id,
                        )
                    )
                text = ""
                pending_calls = []

            for part in parts:
                if part.type == "text" and part.output:
                    if pending_calls:
                        flush_segment()
                    text += part.output
                elif part.type == "steer" and part.output:
                    # A message the user sent while the turn was running sits
                    # between the answer segments that surround it.
                    flush_segment()
                    history.append(
                        LLMMessage(
                            role="user",
                            content=part.output,
                            message_id=str(message.id),
                        )
                    )
                elif part.id in tool_ids:
                    pending_calls.append(part)
            flush_segment()
        return history

    @staticmethod
    def _slice_messages_for_compaction(
        stored: list[HarnessMessage],
        compaction: tuple[HarnessMessage, HarnessPart],
    ) -> list[HarnessMessage]:
        """Keep tail messages from the latest compaction checkpoint onward."""
        compaction_msg, part = compaction
        tail_start_id = str((part.meta or {}).get("tail_start_id", "") or "").strip()
        if tail_start_id:
            try:
                tail_uuid = uuid.UUID(tail_start_id)
            except ValueError:
                tail_uuid = None
            if tail_uuid is not None:
                for index, message in enumerate(stored):
                    if message.id == tail_uuid:
                        return stored[index:]
        for index, message in enumerate(stored):
            if message.id == compaction_msg.id:
                return stored[index:]
        return stored

    @staticmethod
    def _assistant_history_content(
        message: HarnessMessage,
        parts: list[HarnessPart],
        *,
        summary: str = "",
    ) -> str | None:
        """Resolve assistant text for history without the compaction summary."""
        text_output = "".join(
            part.output for part in parts if part.type == "text" and part.output
        )
        if text_output:
            return text_output
        content = (message.content or "").strip()
        if not content:
            return None
        if summary and content == summary.strip():
            return None
        return message.content

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
        part = HarnessPartRepository.get_latest_for_message(
            prev.id, type="step-finish"
        )
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
        from .tools import agent_s_tool_registry, default_tool_registry

        if (agent_name or "").strip().lower() == "computeruse":
            # Agent-S children never see OpenCuria tool schemas.
            return agent_s_tool_registry()
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
            if agent_key not in {"computeruse", "title", "summary", "compaction"}:
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
            agent_s_run_config = None
            if (session.agent_name or "").strip().lower() == "computeruse":
                agent_s_run_config = await sync_to_async(
                    self._resolve_agent_s_run_config
                )(organization_id, model)
            accessor = None
            if self._accessor_factory is not None:
                accessor = await self._accessor_factory(str(session.workspace_id))
            if accessor is not None:
                history = await hydrate_user_messages(history, accessor)
            tools = self._tools_for_session(key, session.agent_name or "build")
            # MCP plugin runtime: snapshot + connections are prepared BEFORE
            # the runner is constructed so any injected runner_factory sees
            # the same fully-registered ToolRegistry (an injected factory may
            # snapshot/copy instead of holding the live reference). Prepare
            # runs in the sync ORM context exactly once (snapshot +
            # credential resolution, no second ORM/decrypt round in setup).
            mcp_enabled = accessor is not None and (
                session.agent_name or ""
            ).strip().lower() not in (
                "computeruse",
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
                    owner_id=str(uuid.uuid4()),
                )
                mcp_runtime.register_tools(tools)
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
                depth=depth,
                max_depth=max_depth,
                history=history,
                session_id=key,
                workspace_id=str(session.workspace_id),
                organization_id=str(organization_id),
                small_model=small_model,
                agent_s_config=agent_s_run_config,
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
                on_permission=lambda **kw: self._on_permission(
                    session, assistant, **kw
                ),
                on_question=lambda **kw: self._on_question(session, assistant, **kw),
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
            if self._user_abort_requested(session):
                raise asyncio.CancelledError()
            # Text deltas were already appended to the assistant message
            # incrementally (see _persist_part_updated); only genuinely new
            # tail content still needs persisting. The runner's final
            # ``output`` is the full assembled answer, so diff it against
            # what is already stored instead of appending blindly (content
            # drifts when a rerun/continuation re-emits a prefix). When a
            # remainder exists it also gets its own text part so message
            # content and parts never drift apart.
            await sync_to_async(self.messages.refresh, thread_sensitive=True)(assistant)
            existing = assistant.content or ""
            tail = result.output or ""
            remainder = _tail_remainder(existing, tail)
            if remainder:
                await sync_to_async(self.messages.append_content)(assistant, remainder)
                await self._append_tail_text_part(session, assistant, remainder)
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
            await self._settle_open_stream_parts(assistant)
        except asyncio.CancelledError:
            revocation = await self._provider_turn_revocation_message(session)
            if revocation:
                run_context["provider_revocation_message"] = revocation
                with anyio.CancelScope(shield=True):
                    await sync_to_async(self.messages.complete)(
                        assistant, finish="aborted", error=revocation
                    )
                    await self._fail_open_parts(
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
                await self._fail_open_parts(
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
                    await self._emit_conversations_changed(session.workspace_id)
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
            await self._append_tail_text_part(session, assistant, text)
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
            await self._fail_open_parts(
                assistant,
                state="error",
                output=str(event.get("summary") or "Subagent ended"),
                session=session,
            )
        else:
            await self._settle_open_stream_parts(assistant)
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
            await self._fail_open_parts(
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
                    await self._emit_conversations_changed(session.workspace_id)
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

    async def _settle_open_stream_parts(self, assistant: HarnessMessage) -> None:
        """Mark leftover running text/reasoning parts completed.

        The runner closes these in-memory on ``step_finish``, but the DB
        rows stay ``running`` unless we persist the completed state.
        """
        open_parts = await sync_to_async(
            lambda: self.parts.list_open_for_message(
                assistant.id, types=("text", "reasoning")
            )
        )()
        for part in open_parts:
            await sync_to_async(self.parts.mark_state)(part, "completed")

    async def _fail_open_parts(
        self,
        assistant: HarnessMessage,
        *,
        state: str,
        output: str,
        session: HarnessSession | None = None,
    ) -> None:
        """Mark pending/running parts of *assistant* as failed/aborted.

        Text and reasoning keep their streamed content and are marked
        completed — aborting a run does not abort a thought that already
        happened. Tool/subtask parts become *state* and keep any partial
        output already written. When *session* is set, open tool and
        subtask parts are also published so the timeline leaves ``running``
        without waiting for a later refetch.
        """
        open_parts = await sync_to_async(self.parts.list_open_for_message)(
            assistant.id
        )
        for part in open_parts:
            if part.type in ("text", "reasoning"):
                await sync_to_async(self.parts.mark_state)(part, "completed")
                continue
            stored = part.output or output
            await sync_to_async(self.parts.mark_state)(part, state, output=stored)
            if session is not None and part.type in ("tool", "subtask"):
                await self._emit_failed_open_part(
                    session, assistant, part, output=stored
                )

    async def _emit_failed_open_part(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        part: HarnessPart,
        *,
        output: str,
    ) -> None:
        """Publish a terminal tool or subtask part after a user abort."""
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        message_id = str(assistant.id)
        if part.type == "subtask":
            meta = part.meta or {}
            await self._emit_frontend(
                FRONTEND_EVENT_SUBTASK_FINISHED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "subtask_id": str(meta.get("subtask_id", "")),
                    "child_session_id": str(meta.get("child_session_id", "")),
                    "agent": str(meta.get("agent", "")),
                    "status": "aborted",
                    "summary": _socket_text(output, limit=240),
                },
                workspace_id,
            )
            return
        await self._emit_frontend(
            FRONTEND_EVENT_PART,
            {
                "workspace_id": workspace_id,
                "session_id": session_id,
                "message_id": message_id,
                "delta": {
                    "tool_error": _socket_text(output, limit=240) or "Tool failed",
                    "title": _socket_text(part.title or "", limit=240),
                    "call_id": _socket_text(part.call_id or "", limit=255),
                    "state": "error",
                },
                "part_id": str(part.id),
            },
            workspace_id,
        )

    async def _append_tail_text_part(
        self, session: HarnessSession, assistant: HarnessMessage, remainder: str
    ) -> None:
        """Persist the non-streamed tail remainder as its own text part."""
        run_ctx = self._runs.get(str(session.id), {})
        part_id = run_ctx.get("text_part_id")
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="text",
                state="running",
                meta={"step": None, "tail": True},
            )
            run_ctx["text_part_id"] = str(part.id)
        await sync_to_async(self.parts.append_output)(part, remainder)

    async def _authorize_file_read(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        path: str,
    ) -> bool:
        """Authorize a Claude user-media read through existing permission gates."""
        from .permissions.evaluator import ASK, DENY, PermissionEvaluator

        action = str(path)
        agent = get_agent(session.agent_name or session.mode or "build")
        agent_decision = PermissionEvaluator(
            agent_rules=dict(agent.permissions or {})
        ).evaluate("read", action, mode=session.mode or "")
        decision = self.permissions.evaluator.evaluate(
            "read", action, mode=session.mode or ""
        )
        # Explicit policy denials remain authoritative, including agent hard bans.
        if DENY in (agent_decision, decision):
            return False
        if await sync_to_async(self.permissions.is_allowlisted)(
            session.organization_id, "read", action, session.workspace_id
        ):
            return True
        if ASK in (agent_decision, decision):
            approved = await self._on_permission(
                session,
                assistant,
                tool="read",
                action=action,
                title=f"Read {action}"[:240],
                key="permission_read",
            )
            return approved in {"once", "always", "allow", "approved", "yes"}
        return True

    async def _on_permission(
        self, session: HarnessSession, assistant: HarnessMessage, **kwargs: Any
    ) -> str:
        """Persist an effective ask unless its exact action is allowlisted."""
        from .engines.claude.security import redact_action, redact_title
        from .permissions.scopes import is_permission_scope, permission_scope

        tool = str(kwargs.get("tool", ""))
        display_action = redact_action(str(kwargs.get("action", "")))
        provided_action = str(kwargs.get("permission_action", ""))
        if is_permission_scope(provided_action):
            scope = provided_action
        else:
            raw_action = provided_action or str(kwargs.get("action", ""))
            scope = permission_scope(raw_action, display_action)
        title = redact_title(str(kwargs.get("title", "")))
        call_id = str(kwargs.get("call_id", ""))
        allowlisted = await sync_to_async(self.permissions.is_allowlisted)(
            session.organization_id,
            tool,
            scope,
            session.workspace_id,
        )
        if allowlisted:
            return "once"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        request = await sync_to_async(self.permissions.requests.create)(
            organization_id=session.organization_id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            tool=tool,
            pattern=scope,
            title=title,
            message_id=assistant.id,
            call_id=call_id,
        )
        self._pending_permissions[str(request.id)] = future
        await self._emit_frontend(
            FRONTEND_EVENT_PERMISSION,
            {
                **(await self._gate_ids(session)),
                "request_id": str(request.id),
                "tool": tool,
                "pattern": display_action,
                "title": title,
                "call_id": call_id,
                "agent_name": session.agent_name or "",
            },
            str(session.workspace_id),
        )
        try:
            decision = await future
        finally:
            self._pending_permissions.pop(str(request.id), None)
        return "once" if decision == "allow" else "reject"

    async def _on_question(
        self, session: HarnessSession, assistant: HarnessMessage, **kwargs: Any
    ) -> list[Any]:
        """Persist a question request and wait for user answers."""
        questions = list(kwargs.get("questions") or [])
        call_id = str(kwargs.get("call_id", ""))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[list[Any]] = loop.create_future()
        request = await sync_to_async(QuestionRequestRepository.create)(
            organization_id=session.organization_id,
            session_id=session.id,
            workspace_id=session.workspace_id,
            message_id=assistant.id,
            call_id=call_id,
            questions=questions,
        )
        self._pending_questions[str(request.id)] = future
        await self._emit_frontend(
            FRONTEND_EVENT_QUESTION,
            {
                **(await self._gate_ids(session)),
                "request_id": str(request.id),
                "questions": questions,
                "call_id": call_id,
                "status": "pending",
                "agent_name": session.agent_name or "",
            },
            str(session.workspace_id),
        )
        try:
            return await future
        except asyncio.CancelledError:
            # Abort while waiting: mark the gate so it never stays
            # pending after the run is gone. resolve_question() would
            # 404 on it otherwise, and the question sheet would linger.
            await sync_to_async(QuestionRequestRepository.resolve)(
                request,
                answers=[],
                status="rejected",
            )
            await self._emit_frontend(
                FRONTEND_EVENT_QUESTION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": str(request.id),
                    "status": "rejected",
                },
                str(session.workspace_id),
            )
            raise
        finally:
            self._pending_questions.pop(str(request.id), None)

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
            await self._persist_runner_event(session, assistant, event)
        # A dependency can consume Task.cancel() while persisting or emitting.
        # Keep explicit stop intent authoritative before control returns to the loop.
        if advances_run and self._user_abort_requested(session):
            raise asyncio.CancelledError()

    async def _persist_runner_event(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Persist a runner streaming event and forward it to frontend.

        Contract shared by the runner, persisted parts, and frontend:

        - every ``harness.part_updated`` payload carries ``message_id``
          (the current assistant shell) so live deltas route to the right
          turn; text/reasoning deltas additionally carry the persisted
          ``part_id`` of the running stream part.
        - ``harness.subtask_started/finished`` carry ``message_id`` too.
        - ``harness.session_status`` busy/idle carries ``message_id`` of the
          *current* assistant (see ``_session_status_payload``).
        - provider-position handling: the runner emits
          ``tool_queued`` ``{type, step, call_id, tool, title, arguments}``
          in observed order. We persist all tool details, but live
          ``delta.tool_started`` contains only bounded tool/title/call_id/state
          fields plus the ``part_id``. The later ``tool_started`` for the
          same call flips the same part to running and re-emits that small
          projection. ``tool_queued`` ``call_id`` may be provisional: when
          the real ``tool_started`` carries a *different* id for the same
          step/tool, we rebind the queued part instead of creating a second row.
        """
        etype = str(event.get("type", ""))
        workspace_id = str(session.workspace_id)
        session_id = str(session.id)
        message_id = str(assistant.id)
        if etype == "part_updated":
            part_id = await self._persist_part_updated(session, assistant, event)
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": event.get("delta", {}),
                    "step": event.get("step"),
                    **({"part_id": part_id} if part_id is not None else {}),
                },
                workspace_id,
            )
        elif etype == "tool_queued":
            part = await self._persist_tool_queued(session, assistant, event)
            identity = _tool_socket_identity(event, part, state="pending")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_started": identity["tool"],
                        "title": identity["title"],
                        "call_id": identity["call_id"],
                        "state": identity["state"],
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "step_start":
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="step-start",
                state="running",
                meta={"step": event.get("step")},
            )
            self._runs.get(session_id, {}).get("step_parts", {})[
                str(event.get("step"))
            ] = str(part.id)
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {"step_start": event.get("step")},
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "step_finish":
            tokens = event.get("tokens", {}) or {}
            usage = Usage(
                prompt_tokens=int(tokens.get("prompt_tokens", 0)),
                completion_tokens=int(tokens.get("completion_tokens", 0)),
                total_tokens=int(tokens.get("total_tokens", 0)),
                cost=float(event.get("cost", 0.0) or 0.0),
            )
            await sync_to_async(self.messages.add_usage)(
                assistant,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                cost=float(event.get("cost", 0.0) or 0.0),
            )
            # Close the matching step-start part (DB-side; it stayed
            # ``running`` because steps used to be frontend-only). A
            # missing step part (e.g. legacy rows) is not an error.
            step_part_id = (
                self._runs.get(session_id, {})
                .get("step_parts", {})
                .pop(str(event.get("step")), None)
            )
            if step_part_id is not None:
                step_part = await sync_to_async(self.parts.get_by_id)(step_part_id)
                if step_part is not None:
                    await sync_to_async(self.parts.mark_state)(step_part, "completed")
            # Close the per-step reasoning and text parts (DB-side; the
            # frontend already closes them live on step_finish). Resetting
            # the ids makes the next step start fresh parts so the final
            # answer stays after tools in created_at order ("Worked for"
            # above the answer after idle fetchParts).
            run_ctx = self._runs.get(session_id, {})
            reasoning_part_id = run_ctx.pop("reasoning_part_id", None)
            if reasoning_part_id is not None:
                reasoning_part = await sync_to_async(self.parts.get_by_id)(
                    reasoning_part_id
                )
                if reasoning_part is not None:
                    await sync_to_async(self.parts.mark_state)(
                        reasoning_part, "completed"
                    )
            text_part_id = run_ctx.pop("text_part_id", None)
            if text_part_id is not None:
                text_part = await sync_to_async(self.parts.get_by_id)(text_part_id)
                if text_part is not None:
                    await sync_to_async(self.parts.mark_state)(text_part, "completed")
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="step-finish",
                state="completed",
                meta={
                    "step": event.get("step"),
                    "tokens": tokens,
                    "cost": event.get("cost", 0.0),
                },
            )
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "step_finish": event.get("step"),
                        "cost": event.get("cost", 0.0),
                        "tokens": tokens,
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_started":
            part = await self._persist_tool_started(session, assistant, event)
            identity = _tool_socket_identity(event, part, state="running")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_started": identity["tool"],
                        "title": identity["title"],
                        "call_id": identity["call_id"],
                        "state": identity["state"],
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "tool_completed":
            part = await self._finish_tool_part(assistant, event, state="completed")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_completed": _socket_text(
                            event.get("tool")
                            or ((part.input or {}).get("tool", "") if part else ""),
                            limit=100,
                        ),
                        "title": _socket_text(
                            event.get("title") or (part.title if part else ""),
                            limit=240,
                        ),
                        "call_id": _socket_text(
                            event.get("call_id") or (part.call_id if part else ""),
                            limit=255,
                        ),
                        "state": "completed",
                        **(
                            {"display": dict(part.display or {})}
                            if part is not None
                            else {}
                        ),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id) if part is not None else None,
                },
                workspace_id,
            )
        elif etype == "tool_error":
            part = await self._finish_tool_part(assistant, event, state="error")
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {
                        "tool_error": "Tool failed",
                        "call_id": _socket_text(event.get("call_id", ""), limit=255),
                        "state": "error",
                        **(
                            {"display": dict(part.display or {})}
                            if part is not None
                            else {}
                        ),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id) if part is not None else None,
                },
                workspace_id,
            )
        elif etype in ("retry_scheduled", "retry_attempt"):
            # Lightweight forward without a DB part, mirroring
            # step_start/step_finish (no persisted part, frontend-only).
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {etype: event.get("attempt")},
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "todo_updated":
            await self._emit_frontend(
                FRONTEND_EVENT_TODO,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "todos": event.get("todos", []),
                    "step": event.get("step"),
                },
                workspace_id,
            )
        elif etype == "agent":
            # Agent-S plan observability (child-visible chain-of-thought
            # plan, never the final assistant answer). Persisted as an
            # ``agent`` card part so the frontend renders it; the child
            # run's assistant message keeps only the final status text.
            # The full plan stays in ``output`` for explicit part-detail
            # fetches; only bounded safe summaries in ``display.agent_meta``
            # and ``display.summary`` reach the live socket. Raw ``exec_code``
            # and materialized coordinates are never persisted.
            delta = event.get("delta", {}) or {}
            plan = str(delta.get("plan", "") or "")
            if plan:
                from .agent_s.plan_meta import parse_plan_meta

                # Always re-parse server-side from the plan: an
                # event-supplied ``agent_meta`` is only a transport
                # optimization and never trusted (it could carry
                # ``exec_code`` or other unexpected keys).
                agent_meta = parse_plan_meta(plan)
                await self._close_running_stream_parts(session)
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="agent",
                    state="completed",
                    title="Agent plan",
                    output=plan,
                    meta={
                        "step": event.get("step"),
                        "agent_meta": agent_meta,
                    },
                )
                safe_agent_meta = dict(part.display.get("agent_meta", {}))
                safe_summary = _socket_text(part.display.get("summary", ""), limit=240)
                await self._emit_frontend(
                    FRONTEND_EVENT_PART,
                    {
                        "workspace_id": workspace_id,
                        "session_id": session_id,
                        "message_id": message_id,
                        "delta": {
                            "agent": safe_summary,
                            "agent_meta": safe_agent_meta,
                            "display": dict(part.display or {}),
                        },
                        "step": event.get("step"),
                        "part_id": str(part.id),
                    },
                    workspace_id,
                )
        elif etype == "subtask_started":
            # A non-text part opens: close any running text/reasoning stream
            # first so nothing merges across the boundary (text/tool/text
            # stays three rows in observed order).
            await self._close_running_stream_parts(session)
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="subtask",
                state="running",
                title=str(event.get("description", "")),
                meta={
                    "subtask_id": event.get("subtask_id", ""),
                    "agent": event.get("agent", ""),
                    "child_session_id": event.get("child_session_id", ""),
                    "model": event.get("model", ""),
                    "reasoning_effort": event.get("reasoning_effort", ""),
                },
            )
            self._runs.get(session_id, {}).get("subtask_parts", {})[
                str(event.get("subtask_id", ""))
            ] = str(part.id)
            await self._emit_frontend(
                FRONTEND_EVENT_SUBTASK_STARTED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "subtask_id": event.get("subtask_id", ""),
                    "child_session_id": event.get("child_session_id", ""),
                    "agent": event.get("agent", ""),
                    "description": event.get("description", ""),
                    "part_id": str(part.id),
                    "model": event.get("model", ""),
                    "reasoning_effort": event.get("reasoning_effort", ""),
                },
                workspace_id,
            )
        elif etype == "subtask_finished":
            await self._finish_subtask_part(session, assistant, event)
            await self._emit_frontend(
                FRONTEND_EVENT_SUBTASK_FINISHED,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "subtask_id": event.get("subtask_id", ""),
                    "child_session_id": event.get("child_session_id", ""),
                    "status": event.get("status", ""),
                    "summary": event.get("summary", ""),
                },
                workspace_id,
            )
        elif etype == "patch":
            await self._close_running_stream_parts(session)
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="patch",
                state="completed",
                call_id=str(event.get("call_id", "")),
                title=str(event.get("title", "")),
                output=str(event.get("unified_diff", "") or ""),
                meta={
                    "path": event.get("path", ""),
                    "step": event.get("step"),
                    "tool": event.get("tool", ""),
                },
            )
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    # The bounded preview lets clients render the card without
                    # refetching the whole timeline for every file edit.
                    "delta": {
                        "patch": event.get("path", ""),
                        "display": dict(part.display or {}),
                    },
                    "step": event.get("step"),
                    "part_id": str(part.id),
                },
                workspace_id,
            )
        elif etype == "compaction":
            await self._close_running_stream_parts(session)
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="compaction",
                state="completed",
                title="Session compacted",
                output=str(event.get("summary", "") or ""),
                meta={
                    "auto": bool(event.get("auto", True)),
                    "overflow": bool(event.get("overflow", False)),
                    "tail_start_id": str(event.get("tail_start_id", "") or ""),
                },
            )
            await self._emit_frontend(
                FRONTEND_EVENT_PART,
                {
                    "workspace_id": workspace_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "delta": {"compaction": True},
                    "part_id": str(part.id),
                },
                workspace_id,
            )

    async def _persist_part_updated(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> str | None:
        """Append text/reasoning deltas to the running text/reasoning parts.

        Reasoning parts stay step-attributed via ``meta["step"]`` (normal
        LLM reasoning without a step keeps working — ``step`` is simply
        ``None``). ``step_finish`` closes the running text and reasoning
        parts DB-side and resets ``text_part_id`` / ``reasoning_part_id``
        so the next step starts fresh ones (see ``_persist_runner_event``).

        A text/reasoning *kind transition* closes the other running stream
        first so text and reasoning never merge across boundaries; a new
        tool/subtask/patch/agent/compaction part also closes both via
        ``_close_running_stream_parts``. Completion alone never closes.
        Returns the persisted ``part_id`` when a text/reasoning delta was
        stored (so live events carry it), else ``None``.
        """
        delta = event.get("delta", {}) or {}
        text = str(delta.get("text", "") or "")
        reasoning = str(delta.get("reasoning", "") or "")
        step = event.get("step")
        key = str(session.id)
        run_ctx = self._runs.get(key, {})
        touched_id: str | None = None
        if text:
            await self._complete_stream_part(run_ctx, "reasoning_part_id")
            part_id = run_ctx.get("text_part_id")
            if part_id is None:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="text",
                    state="running",
                    meta={"step": step},
                )
                run_ctx["text_part_id"] = str(part.id)
                part_id = str(part.id)
            await sync_to_async(self.messages.append_content)(
                assistant, text, text_part_id=part_id
            )
            touched_id = str(part_id)
        if reasoning:
            await self._complete_stream_part(run_ctx, "text_part_id")
            part_id = run_ctx.get("reasoning_part_id")
            if part_id is None:
                part = await sync_to_async(self.parts.create)(
                    message_id=assistant.id,
                    type="reasoning",
                    state="running",
                    meta={"step": step},
                )
                run_ctx["reasoning_part_id"] = str(part.id)
                part_id = str(part.id)
            await sync_to_async(self.parts.append_output_by_id)(part_id, reasoning)
            touched_id = str(part_id)
        return touched_id

    async def _complete_stream_part(self, run_ctx: dict[str, Any], slot: str) -> None:
        """Complete+clear one running text/reasoning stream slot (best-effort)."""
        part_id = run_ctx.pop(slot, None)
        if part_id is None:
            return
        part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is not None:
            await sync_to_async(self.parts.mark_state)(part, "completed")

    async def _close_running_stream_parts(self, session: HarnessSession) -> None:
        """Close open text/reasoning streams before a non-text part boundary."""
        run_ctx = self._runs.get(str(session.id), {})
        await self._complete_stream_part(run_ctx, "text_part_id")
        await self._complete_stream_part(run_ctx, "reasoning_part_id")

    async def _persist_tool_queued(
        self, session: HarnessSession, assistant: HarnessMessage, event: dict[str, Any]
    ) -> HarnessPart:
        """Persist one tool card per non-empty call ID without regressing state.

        Some engines announce a call once with a provisional empty payload and
        again after arguments are available. The later event enriches the same
        pending card; it never creates an orphan card or reopens a terminal one.
        Calls with no ID retain the legacy create-per-event behavior.
        """
        await self._close_running_stream_parts(session)
        call_id = str(event.get("call_id", "") or "")
        run_ctx = self._runs.get(str(session.id), {})
        tool_parts = run_ctx.setdefault("tool_parts", {})
        part = None
        if call_id:
            part_id = tool_parts.get(call_id)
            if part_id:
                part = await sync_to_async(
                    lambda: self.parts.find_by_call_id(
                        assistant.id, call_id, type="tool", part_id=part_id
                    )
                )()
            if part is None:
                part = await sync_to_async(
                    lambda: self.parts.find_by_call_id(
                        assistant.id, call_id, type="tool"
                    )
                )()

        incoming_tool = str(event.get("tool", "") or "")
        incoming_arguments = event.get("arguments", "")
        if part is not None:
            if call_id:
                tool_parts[call_id] = str(part.id)
            if part.state not in ("pending", "running"):
                return part
            current_input = dict(part.input or {})
            merged_input = dict(current_input)
            if incoming_tool:
                merged_input["tool"] = incoming_tool
            if incoming_arguments not in (None, "", {}, []):
                merged_input["arguments"] = incoming_arguments
            meta = dict(part.meta or {})
            if event.get("step") is not None:
                meta["step"] = event.get("step")
            await sync_to_async(self.parts.set_input)(part, merged_input)
            title = str(event.get("title", "") or part.title or "")
            if title != part.title or meta != dict(part.meta or {}):
                part = await sync_to_async(self.parts.mark_state)(
                    part,
                    part.state,
                    title=title,
                    meta=meta,
                )
            if call_id:
                tool_parts[call_id] = str(part.id)
            return part

        part = await sync_to_async(self.parts.create)(
            message_id=assistant.id,
            type="tool",
            state="pending",
            call_id=call_id,
            title=str(event.get("title", "") or ""),
            input={
                "tool": incoming_tool,
                "arguments": incoming_arguments or "",
            },
            meta={"step": event.get("step")},
        )
        tool_parts[call_id] = str(part.id)
        return part

    async def _persist_tool_started(
        self, session: HarnessSession, assistant: HarnessMessage, event: dict[str, Any]
    ) -> HarnessPart:
        """Update-or-create the tool part for ``tool_started`` (running).

        The normal path updates the ``tool_queued`` row for the same
        ``call_id`` to running and re-emits ``delta.tool_started`` with the
        *same* ``part_id`` (frontend dedupes). When the queued ``call_id``
        was provisional, a pending tool part for
        the same step+tool is rebound by updating its ``call_id`` instead of
        creating a second row. Without any queued row this falls back to
        creating the part here (legacy runner path).
        """
        await self._close_running_stream_parts(session)
        call_id = str(event.get("call_id", ""))
        step = event.get("step")
        tool = str(event.get("tool", ""))
        run_ctx = self._runs.get(str(session.id), {})
        part = await self._lookup_tool_part(run_ctx, assistant, call_id)
        queued_id = str(event.get("queued_id", "") or "")
        if part is None and queued_id:
            part = await self._lookup_tool_part(run_ctx, assistant, queued_id)
        if part is None and not queued_id:
            part = await self._rebind_provisional_tool_part(
                run_ctx, assistant, step=step, tool=tool, call_id=call_id
            )
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="tool",
                state="running",
                call_id=call_id,
                title=str(event.get("title", "")),
                input={
                    "tool": event.get("tool", ""),
                    "arguments": event.get("arguments", ""),
                },
                meta={"step": step},
            )
            run_ctx.get("tool_parts", {})[call_id] = str(part.id)
            return part
        if call_id and part.call_id != call_id:
            rebased_meta = dict(part.meta or {})
            rebased_meta["step"] = step
            await sync_to_async(self.parts.rebind_call_id)(
                part, call_id, meta=rebased_meta
            )
            # Rebind the run-context index to the real call id.
            tool_parts = run_ctx.get("tool_parts", {})
            for old_key, old_part_id in list(tool_parts.items()):
                if old_part_id == str(part.id) and old_key != call_id:
                    del tool_parts[old_key]
            tool_parts[call_id] = str(part.id)
        await sync_to_async(self.parts.mark_state)(
            part,
            "running",
            title=str(event.get("title", "") or part.title),
            meta={"step": step},
        )
        run_ctx.get("tool_parts", {})[call_id] = str(part.id)
        # Refresh title/arguments observed at start (queued row may only
        # have had provisional values).
        part.title = str(event.get("title", "") or part.title)
        await sync_to_async(self.parts.set_input)(
            part,
            {
                "tool": event.get("tool", "") or (part.input or {}).get("tool", ""),
                "arguments": event.get("arguments", "")
                or (part.input or {}).get("arguments", ""),
            },
        )
        return part

    async def _lookup_tool_part(
        self, run_ctx: dict[str, Any], assistant: HarnessMessage, call_id: str
    ) -> HarnessPart | None:
        """Return the tool part for *call_id* (run ctx first, then DB)."""
        part = None
        part_id = (run_ctx.get("tool_parts", {}) or {}).get(call_id)
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None and call_id:
            part = await sync_to_async(self.parts.find_by_call_id)(
                assistant.id, call_id
            )
        return part

    async def _rebind_provisional_tool_part(
        self,
        run_ctx: dict[str, Any],
        assistant: HarnessMessage,
        *,
        step: Any,
        tool: str,
        call_id: str,
    ) -> HarnessPart | None:
        """Rebind a pending queued part when ``tool_queued`` used a temp id.

        Matches the oldest still-pending tool part for the same step+tool so
        the real ``tool_started`` id adopts that row (same ``part_id``
        across both live events) instead of appending a duplicate.
        """
        if not tool or not call_id:
            return None
        candidate = await sync_to_async(self.parts.list_pending_tools)(assistant.id)
        for row in candidate:
            meta = dict(row.meta or {})
            row_tool = str((row.input or {}).get("tool", "") or row.title or "")
            if meta.get("step") == step and (
                row_tool == tool or (row.title or "") == tool or not row_tool
            ):
                return row
        return None

    async def _finish_tool_part(
        self, assistant: HarnessMessage, event: dict[str, Any], *, state: str
    ) -> HarnessPart:
        """Transition the matching tool part to completed/error.

        ``tool_completed`` events persist sanitized attachments into
        ``HarnessPart.meta["attachments"]`` and full output in ``output``;
        image/PDF bytes survive restarts and are rehydrated in
        :meth:`_build_history`. These full details never enter socket deltas.
        ``mark_state`` merges ``meta`` (existing keys like ``step`` from
        the ``tool_started`` part are preserved). Tool *results* never
        create a new part: completion only updates the queued/started row;
        the no-queued fallback creates the row here for legacy runners.
        """
        call_id = str(event.get("call_id", ""))
        output = str(event.get("output", "") or event.get("error", "") or "")
        part_id = (
            self._runs.get(str(assistant.session_id), {})
            .get("tool_parts", {})
            .get(call_id)
        )
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None and call_id:
            part = await sync_to_async(self.parts.find_by_call_id)(
                assistant.id, call_id
            )
        queued_id = str(event.get("queued_id", "") or "")
        if part is None and queued_id:
            part = await self._lookup_tool_part(
                self._runs.get(str(assistant.session_id), {}), assistant, queued_id
            )
            if part is not None and call_id != part.call_id:
                await sync_to_async(self.parts.rebind_call_id)(part, call_id)
                self._runs.get(str(assistant.session_id), {}).get("tool_parts", {})[
                    call_id
                ] = str(part.id)
        if part is None:
            part = await sync_to_async(self.parts.create)(
                message_id=assistant.id,
                type="tool",
                state="pending",
                call_id=call_id,
                title=str(event.get("tool", "")),
            )
        meta: dict[str, Any] | None = None
        if state == "completed":
            attachments = select_persisted_tool_attachments(
                _event_tool_attachments(event)
            )
            if attachments:
                meta = {"attachments": attachments}
        await sync_to_async(self.parts.mark_state)(
            part, state, output=output or part.output, meta=meta
        )
        return part

    async def _finish_subtask_part(
        self,
        session: HarnessSession,
        assistant: HarnessMessage,
        event: dict[str, Any],
    ) -> None:
        """Transition the matching subtask part to completed/error."""
        subtask_id = str(event.get("subtask_id", ""))
        status = str(event.get("status", ""))
        part_id = (
            self._runs.get(str(session.id), {}).get("subtask_parts", {}).get(subtask_id)
        )
        part = None
        if part_id is not None:
            part = await sync_to_async(self.parts.get_by_id)(part_id)
        if part is None:
            return
        state = "completed" if status == "completed" else "error"
        await sync_to_async(self.parts.mark_state)(
            part,
            state,
            meta={
                "subtask_id": subtask_id,
                "agent": event.get("agent", ""),
                "status": status,
                "child_session_id": event.get("child_session_id", ""),
            },
        )

    def _event_lock(self, session_id: str) -> asyncio.Lock:
        """Return the per-session lock for runner-event persistence."""
        lock = self._event_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._event_locks[session_id] = lock
        return lock

    async def _reject_pending_user_gates(self, session: HarnessSession) -> None:
        """Reject leftover permission and question gates after abort."""
        pending_perms = await sync_to_async(
            self.permissions.requests.list_pending_for_session
        )(session.id)
        pending_questions = await sync_to_async(
            QuestionRequestRepository.list_pending_for_session
        )(session.id)
        workspace_id = str(session.workspace_id)
        for request in pending_perms:
            await sync_to_async(self.permissions.requests.mark_resolved)(
                request, approved=False, remember="once"
            )
            future = self._pending_permissions.pop(str(request.id), None)
            if future is not None and not future.done():
                future.cancel()
            await self._emit_frontend(
                FRONTEND_EVENT_PERMISSION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": str(request.id),
                    "decision": "reject",
                    "remember": "once",
                },
                workspace_id,
            )
        for request in pending_questions:
            await sync_to_async(QuestionRequestRepository.resolve)(
                request,
                answers=[],
                status="rejected",
            )
            future = self._pending_questions.pop(str(request.id), None)
            if future is not None and not future.done():
                future.cancel()
            await self._emit_frontend(
                FRONTEND_EVENT_QUESTION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": str(request.id),
                    "status": "rejected",
                },
                workspace_id,
            )

    async def _resolve_sibling_permissions(
        self,
        *,
        session: HarnessSession,
        tool: str,
        skip_id: uuid.UUID,
        decision: str,
        remember: str,
    ) -> None:
        """Approve remaining pending asks for the same tool in this session."""
        pending = await sync_to_async(
            self.permissions.requests.list_pending_for_session
        )(session.id, tool=tool)
        for sibling in pending:
            if sibling.id == skip_id:
                continue
            allowlisted = await sync_to_async(self.permissions.is_allowlisted)(
                session.organization_id,
                sibling.tool,
                sibling.pattern,
                session.workspace_id,
            )
            if not allowlisted:
                continue
            await sync_to_async(self.permissions.requests.mark_resolved)(
                sibling, approved=True, remember="always"
            )
            sid = str(sibling.id)
            future = self._pending_permissions.pop(sid, None)
            if future is not None and not future.done():
                future.set_result(decision)
            await self._emit_frontend(
                FRONTEND_EVENT_PERMISSION,
                {
                    **(await self._gate_ids(session)),
                    "request_id": sid,
                    "decision": decision,
                    "remember": remember,
                },
                str(session.workspace_id),
            )

    async def _gate_ids(self, session: HarnessSession) -> dict[str, str]:
        """Workspace, session, and root session ids for gate events."""
        root_id = await sync_to_async(self.sessions.get_root_id)(session)
        return {
            "workspace_id": str(session.workspace_id),
            "session_id": str(session.id),
            "root_session_id": str(root_id),
        }

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

    async def _emit_conversations_changed(self, workspace_id: uuid.UUID | str) -> None:
        """Notify workspace subscribers that the conversation feed may have changed."""
        workspace = str(workspace_id)
        await self._emit_frontend(
            FRONTEND_EVENT_CONVERSATIONS_CHANGED,
            {"workspace_id": workspace},
            workspace,
        )

    def _emit_conversations_changed_sync(self, workspace_id: uuid.UUID | str) -> None:
        """Dispatch a feed change from synchronous service/API entry points.

        ``async_to_sync`` bridges sync Django views and sync-to-async worker
        threads back to the request loop. If called directly from an async
        context, schedule on that loop instead of blocking it.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            async_to_sync(self._emit_conversations_changed)(workspace_id)
        else:
            loop.create_task(self._emit_conversations_changed(workspace_id))

    async def _emit_frontend(
        self, event: str, data: dict[str, Any], workspace_id: str
    ) -> None:
        """Forward an event to subscribed frontend clients (never breaks)."""
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
                    position = await sync_to_async(self.parts.get_position)(
                        part_id
                    )
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
        from .services import ProviderConfigService

        try:
            config = await sync_to_async(ProviderConfigService().get_config)(
                session.organization_id
            )
        except NotFoundError:
            return
        if not (config.small_model or "").strip():
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
            await self._generate_summary(
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

    def _summary_material(self, session_id: uuid.UUID, assistant_id: uuid.UUID) -> str:
        """Collect user requests and closing replies through the settled turn."""
        message = self.messages.get_by_id(assistant_id)
        if (
            message is None
            or message.session_id != session_id
            or message.role != "assistant"
        ):
            return ""
        sections: list[str] = []
        has_request = False
        for row in self.messages.list_for_summary(session_id, message.position):
            if row.role == "user":
                if row.content.strip():
                    sections.append(f"User:\n{row.content.strip()}")
                    has_request = True
                continue
            parts = row.summary_parts
            for part in parts:
                if part.type == "steer" and part.summary_text.strip():
                    sections.append(f"User:\n{part.summary_text.strip()}")
                    has_request = True
            answer = _assistant_summary_content(row, parts)
            if answer:
                sections.append(f"Agent's final reply:\n{answer}")
        return "\n\n".join(sections) if has_request else ""

    async def _generate_summary(
        self,
        *,
        session_id: uuid.UUID,
        assistant_id: uuid.UUID,
        workspace_id: uuid.UUID,
        organization_id: uuid.UUID,
        provider_turn_id: uuid.UUID | str | None = None,
        account_ids: dict[str, str] | None = None,
        initiated_by_id: int | None = None,
    ) -> None:
        """Store a brief recap of the user's goal after a run settles.

        Runs for every engine (the small model comes from the org's provider
        config, not from the session's engine) and never raises.
        """
        try:
            if provider_turn_id and await sync_to_async(
                self._provider_turn_service.cancellation_reason
            )(provider_turn_id):
                return
            small = await build_small_model_runner(
                organization_id,
                user_id=initiated_by_id,
                account_ids=account_ids,
                provider_factory=self._provider_factory,
                on_account_used=(
                    partial(self._provider_turn_service.register_use, provider_turn_id)
                    if provider_turn_id
                    else None
                ),
            )
            if small is None:
                return
            runner, small_model = small
            material = await sync_to_async(self._summary_material)(
                session_id, assistant_id
            )
            if not material:
                return
            result = await runner.run(
                material,
                "summary",
                small_model,
                "build",
                RunOptions(auto_approve=True),
            )
            if provider_turn_id and await sync_to_async(
                self._provider_turn_service.cancellation_reason
            )(provider_turn_id):
                return
            summary = _normalize_generated_summary(result.output or "")
            if not summary:
                return
            stored = await sync_to_async(self.sessions.set_summary_for_turn)(
                session_id, assistant_id, summary
            )
            if stored:
                await self._emit_frontend(
                    FRONTEND_EVENT_SUMMARY,
                    {
                        "workspace_id": str(workspace_id),
                        "session_id": str(session_id),
                        "summary": summary,
                    },
                    str(workspace_id),
                )
        except Exception:  # pragma: no cover - summary must never break runs
            log.warning("harness_summary_generation_failed", session_id=str(session_id))

    async def _generate_title(
        self,
        *,
        session_id: uuid.UUID,
        prompt: str,
        organization_id: uuid.UUID,
        provider_turn_id: uuid.UUID | str | None = None,
        account_ids: dict[str, str] | None = None,
        initiated_by_id: int | None = None,
    ) -> None:
        """Run the hidden title agent on the initiating answer's snapshot."""
        try:
            session = await sync_to_async(self.sessions.get_by_id)(session_id)
            if session is None or getattr(session, "harness_id", "native") != "native":
                return
            if provider_turn_id and await sync_to_async(
                self._provider_turn_service.cancellation_reason
            )(provider_turn_id):
                return
            small = await build_small_model_runner(
                organization_id,
                user_id=initiated_by_id,
                account_ids=account_ids,
                provider_factory=self._provider_factory,
                on_account_used=(
                    partial(self._provider_turn_service.register_use, provider_turn_id)
                    if provider_turn_id
                    else None
                ),
            )
            if small is None:
                return
            runner, small_model = small
            result = await runner.run(
                prompt,
                "title",
                small_model,
                "build",
                RunOptions(auto_approve=True),
            )
            if provider_turn_id and await sync_to_async(
                self._provider_turn_service.cancellation_reason
            )(provider_turn_id):
                return
            title = _normalize_generated_title(result.output or "")
            if not title:
                return
            session = await sync_to_async(self.sessions.get_by_id)(session_id)
            if session is None:
                return
            if provider_turn_id and await sync_to_async(
                self._provider_turn_service.cancellation_reason
            )(provider_turn_id):
                return
            auto_title = _title_from_prompt(prompt)
            if (session.title or "").strip() != auto_title:
                return
            await sync_to_async(self.sessions.set_title)(session, title)
            if session.parent_id is None:
                await self._emit_conversations_changed(session.workspace_id)
        except Exception:  # pragma: no cover - title must never break runs
            log.warning("harness_title_generation_failed", session_id=str(session_id))

    @staticmethod
    def _resolve_agent_s_run_config(organization_id: uuid.UUID, model: str):  # type: ignore[no-untyped-def]
        """Build the persisted Agent-S run config for a computeruse run.

        Only computeruse children receive a config; other agents resolve
        ``None`` downstream. Grounding falls back to the effective session
        model when unconfigured (see ``AgentSConfigService.to_run_config``).
        """
        from .services import AgentSConfigService

        return AgentSConfigService().to_run_config(organization_id, main_model=model)

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
            if agent == "computeruse":
                raise ToolError(
                    "Codex task computeruse is not supported yet; "
                    "use a native Agent-S session",
                    tool="task",
                )
            try:
                model, _ = await sync_to_async(self._validate_codex_session_config)(
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
        parent_assistant = await sync_to_async(self.messages.get_required)(
            message_id
        )
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
                    await sync_to_async(self._validate_codex_session_config)(
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
            assistant = await sync_to_async(self.messages.get_required)(
                assistant.id
            )
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


def computeruse_recording_path(output: str, child_id: str) -> str | None:
    """Return the real recording path embedded in a computer-use child output.

    The child assistant content already embeds the final ``record_stop``
    path when recording was enabled — reuse the first
    ``![Computer use](...)`` target so the parent card links the real
    file. Returns ``None`` when no recording marker is present (recording
    disabled): callers must not invent a video via a default path.
    ``child_id`` is kept for backward compatibility and ignored.
    """
    from .agent_s.harness import extract_recording_path

    return extract_recording_path(output)


def _normalize_generated_title(text: str) -> str:
    """Clamp a generated title to at most eight words and 255 chars."""
    title = " ".join((text or "").strip().split())
    words = title.split()
    if len(words) > 8:
        title = " ".join(words[:8])
    return title[:255]


def _title_from_prompt(prompt: str) -> str:
    """Derive a short session title from the first prompt line."""
    first_line = (prompt or "").strip().splitlines()[0] if prompt.strip() else ""
    title = first_line.strip()[:80]
    return title or "Harness session"


def _normalize_generated_summary(text: str) -> str:
    """Reduce model output to one plain line of at most 100 chars."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    summary = " ".join(lines[0].split()) if lines else ""
    summary = summary.replace("*", "").replace("`", "").strip("\"'“”„ ")
    if len(summary) > 100:
        summary = summary[:100].rsplit(" ", 1)[0].rstrip(",;:- ") + "…"
    return summary


def _assistant_summary_content(
    message: HarnessMessage, parts: list[HarnessPart]
) -> str:
    """Select closing prose outside Worked, like chat-core's message-copy rule.

    Summary rows project only text/steer output into ``summary_text``. Walk
    backwards through visible text; work, plans, compaction, and steers end the
    final answer. Never fall back to aggregate content when parts exist.
    """
    if not parts:
        return (message.content or "").strip()
    has_subtask = any(part.type == "subtask" for part in parts)
    patched_calls = {
        part.call_id for part in parts if part.type == "patch" and part.call_id
    }
    answer: list[str] = []
    for part in reversed(parts):
        if part.type in {"step-start", "step-finish"}:
            continue
        text = part.summary_text
        if part.type == "text":
            if text:
                answer.append(text)
            continue
        if part.type == "steer" and not text:
            continue
        if part.type == "tool":
            tool = str((part.display or {}).get("tool") or "").strip().lower()
            if tool in {"question", "ask_user"} and part.state in {
                "pending",
                "running",
            }:
                continue
            if has_subtask and (tool == "task" or part.title.startswith("Subagent:")):
                continue
            if (
                tool in {"edit", "write"}
                and part.state != "error"
                and part.call_id in patched_calls
            ):
                continue
        break
    return "".join(reversed(answer)).strip()


def _tail_remainder(existing: str, tail: str) -> str:
    """Return the genuinely-new suffix of a runner final ``output``.

    Streaming already appended every text delta to the message, so the
    final ``output`` is the full assembled answer: appending it blindly
    duplicates content (content-only drift). Exact suffix/prefix overlaps
    return ``""``; only a true remainder is returned for persistence.
    """
    existing = existing or ""
    tail = tail or ""
    if not tail:
        return ""
    if not existing:
        return tail
    if existing.endswith(tail):
        return ""
    if tail.startswith(existing):
        return tail[len(existing) :]
    if existing.startswith(tail):
        return ""
    # A divergent final string is not a suffix of the streamed response.
    # Appending it would duplicate or scramble the answer; the persisted
    # deltas remain authoritative instead.
    return ""


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


def _descendant_ids(
    session_id: uuid.UUID,
    children_map: dict[uuid.UUID, list[uuid.UUID]],
) -> list[uuid.UUID]:
    """Return *session_id* followed by descendant ids from *children_map*."""
    ids: list[uuid.UUID] = [session_id]
    queue: list[uuid.UUID] = [session_id]
    seen: set[uuid.UUID] = {session_id}
    while queue:
        current = queue.pop(0)
        for child_id in children_map.get(current, []):
            if child_id in seen:
                continue
            seen.add(child_id)
            ids.append(child_id)
            queue.append(child_id)
    return ids


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
