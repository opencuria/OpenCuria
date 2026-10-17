"""Gateway admission facade: durable state and local execution both use DB rows."""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from .harness_service import (
    FRONTEND_EVENT_STATUS,
    HarnessService,
    _normalize_skill_ids,
)
from .models import HarnessMessage, HarnessRun, HarnessSession, HarnessSessionStatus
from .providers.base import ProviderAdapter
from .providers.model_ref import parse_model_ref
from .worker_protocol import HarnessDeployment, HarnessWorkerRepository

log = structlog.get_logger(__name__)


class DurableHarnessService(HarnessService):
    """Keep the existing query/service surface; replace run admission with DB.

    Local mode deliberately remains the legacy injectable HarnessService.
    Gateway and worker mode use one admission transaction and a separate durable
    run row for every engine (native and Claude).
    """

    def __init__(self, *, deployment: HarnessDeployment, gateway: bool, **kwargs: Any):
        super().__init__(**kwargs)
        self.deployment = deployment
        self.gateway = gateway
        self._durable_admission = True

    async def _start_durable_run(
        self,
        session: HarnessSession,
        prompt: str,
        *,
        organization_id: uuid.UUID | None,
        provider: ProviderAdapter | None,
        workspace_id: str,
        user_id: int | None,
        skill_ids: list[str] | None,
        scheduled: bool,
        max_depth: int | None,
    ) -> HarnessMessage:
        """Validate, atomically admit, then enqueue or execute a durable run."""
        if self.gateway:
            from apps.runners.sio_server import get_sio_server

            from .runner_rpc import get_gateway_broker

            await get_gateway_broker().start(get_sio_server())
        key = str(session.id)
        if await sync_to_async(self.is_running)(session.id):
            raise ConflictError(
                f"Harness session '{session.id}' already has an active run"
            )
        await sync_to_async(self.sessions.ensure_interactions_available)(
            session.workspace_id
        )
        engine_id = (getattr(session, "harness_id", "native") or "native").strip()
        org_id = organization_id or session.organization_id
        run_user_id = user_id
        if engine_id == "claude":
            owner_id = getattr(session, "created_by_id", None)
            if (
                run_user_id is None
                or owner_id is None
                or str(run_user_id) != str(owner_id)
            ):
                raise ConflictError(
                    "Only the session owner may use its Claude connection"
                )
            await sync_to_async(self._resolve_engine_auth)(
                session.organization_id,
                owner_id,
                getattr(session, "connection_id", None),
            )
        depth = await sync_to_async(self.sessions.get_depth)(session.id)
        if max_depth is None:
            from .services import SubagentConfigService

            max_depth = (
                await sync_to_async(SubagentConfigService().get_or_default)(org_id)
            )["max_depth"]
        if depth > max_depth:
            raise ValueError(f"depth {depth} exceeds max_depth {max_depth}")
        effective_skill_ids = (
            _normalize_skill_ids(skill_ids)
            if skill_ids is not None
            else _normalize_skill_ids(list(session.skill_ids or []))
        )
        skill_bodies: list[str] = []
        if engine_id == "claude" or skill_ids is not None:
            if effective_skill_ids and run_user_id is None:
                raise ConflictError("A user is required to validate selected skills")
            if run_user_id is not None:
                from .harness_service import resolve_skill_bodies

                skill_bodies = await sync_to_async(resolve_skill_bodies)(
                    effective_skill_ids,
                    user_id=run_user_id,
                    organization_id=org_id,
                )
            if engine_id == "claude":
                from .engines.claude.runtime_state import validate_claude_skills

                skill_bodies = validate_claude_skills(skill_bodies)
        if skill_ids is not None:
            if engine_id == "claude":
                session = await sync_to_async(self.sessions.set_skill_ids)(
                    session, effective_skill_ids
                )
            else:
                if user_id is None:
                    raise ConflictError(
                        "A user is required to validate selected skills"
                    )
                session = await sync_to_async(self.update_skill_ids)(
                    session.id,
                    effective_skill_ids,
                    user_id=user_id,
                    organization_id=org_id,
                )
        resolved_model = await sync_to_async(self.validate_provider_for_run)(
            org_id, session, provider=provider
        )
        if provider is not None:
            resolved_provider = provider.name
        elif engine_id == "claude":
            resolved_provider = "claude"
        else:
            resolved_provider, _ = parse_model_ref(resolved_model)
        parent_run = None
        inherited_worker = ""
        inherited_version = ""
        root_run_id = None
        if session.parent_id is not None:
            parent_run = await sync_to_async(
                lambda: HarnessRun.objects.filter(
                    session_id=session.parent_id,
                    status__in=("starting", "running", "closing"),
                ).first()
            )()
            if parent_run is not None:
                inherited_worker = parent_run.owner_worker_id
                inherited_version = parent_run.deployment_version
                root_run_id = parent_run.root_run_id or parent_run.id
        local_execution = not self.gateway
        if self.gateway and session.parent_id is not None:
            raise ConflictError("Subagent parent has no durable worker owner")
        owner_worker_id = self.deployment.worker_id if local_execution else ""
        deployment_version = inherited_version or self.deployment.version

        user_message, assistant, run, prior_users = await sync_to_async(
            HarnessWorkerRepository.admit_run, thread_sensitive=True
        )(
            session_id=session.id,
            prompt=prompt.strip(),
            model=resolved_model,
            reasoning_effort=session.reasoning_effort or "",
            provider=resolved_provider,
            skill_ids=list(session.skill_ids or []),
            engine_id=engine_id,
            initiated_by_id=run_user_id,
            deployment_version=deployment_version,
            gateway=self.gateway,
            owner_worker_id=owner_worker_id,
            admission_versions=self.deployment.gateway_worker_versions,
            inherited_worker_id=inherited_worker,
            inherited_version=inherited_version,
            scheduled=scheduled,
            root_run_id=root_run_id,
        )
        session.status = HarnessSessionStatus.BUSY
        context = self._worker_context(run, user_message)
        context["skill_bodies"] = skill_bodies
        context["initiated_by_id"] = run_user_id
        context["engine_id"] = engine_id
        if local_execution:
            self._runs[key] = context
            self._remember_run(session)
        if local_execution:
            context["engine_id"] = engine_id
            from .engines.runs import RunOwnership

            owner = RunOwnership(run)
            context["ownership"] = owner
            owner.start_heartbeat(on_lost=lambda: self._cancel_owned_task(key))
            authority = self._worker_authorities.get(key)
            if authority is None:
                from .runtime_authority import RunAuthority

                root_id = run.root_run_id or run.id
                authority = RunAuthority(
                    owner.run_id,
                    owner.owner_token,
                    self.deployment.worker_id,
                    self.deployment.version,
                    root_id,
                )
                self._worker_authorities[key] = authority
            history = await self._build_history(
                session,
                exclude_message_id=assistant.id,
                exclude_user_message_id=user_message.id,
            )
            task = self._spawn_background(
                self._execute_run(
                    session=session,
                    prompt=prompt.strip(),
                    history=history,
                    assistant=assistant,
                    provider=provider,
                    organization_id=org_id,
                    depth=depth,
                    max_depth=max_depth,
                )
            )
            self._tasks[key] = task
            task.add_done_callback(lambda done: self._on_run_task_done(done, key))
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
        if (
            local_execution
            and engine_id == "native"
            and session.parent_id is None
            and prior_users == 0
        ):
            self._spawn_background(
                self._generate_title(
                    session_id=session.id, prompt=prompt.strip(), organization_id=org_id
                )
            )
        return assistant

    def _worker_context(
        self, run: HarnessRun, user_message: HarnessMessage
    ) -> dict[str, Any]:
        """Create task-local mutable projection state from durable identifiers."""
        return {
            "session_id": str(run.session_id),
            "workspace_id": str(run.session.workspace_id),
            "organization_id": str(run.organization_id),
            "message_id": str(run.assistant_message_id),
            "user_message_id": str(user_message.id),
            "text_part_id": None,
            "reasoning_part_id": None,
            "tool_parts": {},
            "step_parts": {},
            "subtask_parts": {},
            "skill_bodies": [],
            "durable_run_id": run.id,
        }

    async def abort_run(self, session_id: uuid.UUID) -> HarnessSession:
        """Persist stop intent for any worker; never clear busy without its owner."""
        session = await sync_to_async(self.get_session)(session_id)
        await sync_to_async(self.sessions.ensure_interactions_available)(
            session.workspace_id
        )
        descendants = await sync_to_async(self.sessions.list_descendant_ids)(session.id)
        await sync_to_async(
            HarnessWorkerRepository.request_abort, thread_sensitive=True
        )(descendants)
        if self.gateway:
            return session
        local_task = self._tasks.get(str(session.id))
        if local_task is not None and not local_task.done():
            self._abort_requested.add(str(session.id))
            local_task.cancel()
        return session


class HarnessGatewayService(DurableHarnessService):
    """Named public type for gateway deployments."""


class HarnessWorkerService(DurableHarnessService):
    """Named public type for worker deployments."""
