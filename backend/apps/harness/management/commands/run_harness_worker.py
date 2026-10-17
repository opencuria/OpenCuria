"""Poll durable outbox commands and execute runs in this process generation."""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from contextlib import suppress
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError

from apps.harness.harness_service import get_harness_service
from apps.harness.models import HarnessRun, HarnessWorkerRegistration
from apps.harness.worker_protocol import HarnessDeployment, HarnessWorkerRepository

log = structlog.get_logger(__name__)


class Command(BaseCommand):
    """Run one uniquely registered immutable harness worker process."""

    help = "Run the versioned OpenCuria harness execution worker"

    def add_arguments(self, parser) -> None:  # type: ignore[no-untyped-def]
        parser.add_argument("--poll-interval", type=float, default=0.5)
        parser.add_argument("--drain", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        deployment = HarnessDeployment.from_environment()
        if deployment.mode != "worker":
            raise CommandError(
                "run_harness_worker requires OPENCURIA_HARNESS_MODE=worker"
            )
        interval = options["poll_interval"]
        if not 0.05 <= interval <= 30:
            raise CommandError("--poll-interval must be in 0.05..30 seconds")
        try:
            asyncio.run(
                self._run(deployment, interval, drain_only=bool(options["drain"]))
            )
        except KeyboardInterrupt:
            self.stdout.write("Worker stopped.")

    async def _run(
        self, deployment: HarnessDeployment, interval: float, *, drain_only: bool
    ) -> None:
        worker_id = deployment.worker_id
        if drain_only:
            row = await sync_to_async(
                HarnessWorkerRepository.get_registration, thread_sensitive=True
            )(worker_id)
            if row is None or row.deployment_version != deployment.version:
                raise CommandError("Cannot drain an unregistered or mismatched worker")
            await sync_to_async(
                HarnessWorkerRepository.set_draining, thread_sensitive=True
            )(worker_id)
            while True:
                row = await sync_to_async(HarnessWorkerRepository.get_registration)(
                    worker_id
                )
                if (
                    row is not None
                    and row.state == HarnessWorkerRegistration.State.STOPPED
                ):
                    break
                await asyncio.sleep(interval)
            return

        # A container name is not a process identity. A restarted container
        # must not adopt the prior process's pending or uncertain effects.
        from dataclasses import replace

        worker_id = f"{worker_id[:80]}-{uuid.uuid4().hex[:12]}"
        os.environ["OPENCURIA_WORKER_ID"] = worker_id
        deployment = replace(deployment, worker_id=worker_id)
        await sync_to_async(HarnessWorkerRepository.register, thread_sensitive=True)(
            worker_id, deployment.version, pid=os.getpid()
        )
        service = get_harness_service()
        from apps.runners.sio_server import get_runner_service

        runner_service = get_runner_service()
        service._runner_service = runner_service

        async def emit(event, data):
            from apps.harness.runtime_authority import current_run_authority

            authority = current_run_authority()
            if authority is None:
                # Admission/gate mutations already persist; live execution
                # updates carry the current task's durable run authority.
                return
            workspace_id = data.get("workspace_id")
            if workspace_id is None:
                from apps.harness.engines.run_repository import HarnessRunRepository

                run = await sync_to_async(HarnessRunRepository.get_by_id)(
                    authority.run_id
                )
                workspace_id = str(run.session.workspace_id) if run else ""
            await runner_service._frontend_bus(event, data, str(workspace_id))

        service._emit = emit
        service.deployment = deployment
        service._worker_deployment = deployment
        running: dict[str, asyncio.Task[None]] = {}
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, stop.set)
        log.info(
            "harness_worker_ready",
            worker_id=worker_id,
            deployment_version=deployment.version,
        )
        rpc_client = getattr(
            getattr(service, "_runner_service", None),
            "_harness_runner_rpc_client",
            None,
        )
        if rpc_client is None:
            rpc_client = getattr(service, "_harness_runner_rpc_client", None)
        if rpc_client is None:
            from apps.runners.sio_server import get_runner_service

            rpc_client = getattr(
                get_runner_service(), "_harness_runner_rpc_client", None
            )
        if rpc_client is not None:
            await rpc_client.start()
        try:
            while True:
                try:
                    if stop.is_set():
                        await sync_to_async(
                            HarnessWorkerRepository.set_draining, thread_sensitive=True
                        )(worker_id)
                    heartbeat_ok = await sync_to_async(
                        HarnessWorkerRepository.heartbeat, thread_sensitive=True
                    )(worker_id)
                    if not heartbeat_ok:
                        raise RuntimeError("Worker registration authority was lost")
                    row = await sync_to_async(
                        HarnessWorkerRepository.get_registration, thread_sensitive=True
                    )(worker_id)
                    draining = bool(
                        row and row.state == HarnessWorkerRegistration.State.DRAINING
                    )
                    await self._handle_abort_intents(worker_id, service, running)
                    if draining:
                        await self._handle_pre_drain_roots(
                            worker_id, service, running, deployment
                        )
                        await self._handle_gate_resolutions(service, running)
                        if not running and await sync_to_async(
                            HarnessWorkerRepository.drained, thread_sensitive=True
                        )(worker_id):
                            return
                    else:
                        for _ in range(16):
                            command = await sync_to_async(
                                HarnessWorkerRepository.claim_next,
                                thread_sensitive=True,
                            )(worker_id, deployment.version)
                            if command is None:
                                break
                            run = await sync_to_async(
                                HarnessWorkerRepository.start_claimed,
                                thread_sensitive=True,
                            )(command.id, worker_id)
                            if run is None:
                                log.error(
                                    "harness_worker_claim_fence_failed",
                                    command_id=str(command.id),
                                )
                                continue
                            await sync_to_async(
                                HarnessWorkerRepository.finish_command,
                                thread_sensitive=True,
                            )(command.id, worker_id)
                            await self._spawn_owned_run(
                                service, run, worker_id, deployment, running
                            )
                        await self._handle_gate_resolutions(service, running)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception(
                        "harness_worker_poll_failed",
                        worker_id=worker_id,
                        error=type(exc).__name__,
                    )
                await asyncio.sleep(interval)
        finally:
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)
            await sync_to_async(
                HarnessWorkerRepository.mark_stopped, thread_sensitive=True
            )(worker_id)
            if rpc_client is not None:
                await rpc_client.close()
            log.info("harness_worker_stopped", worker_id=worker_id)

    async def _spawn_owned_run(
        self, service, run: HarnessRun, worker_id: str, deployment, running: dict
    ) -> None:
        """Settle configuration failures instead of orphaning busy runs."""
        try:
            await self._prepare_owned_run(service, run, worker_id, deployment, running)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("harness_worker_preparation_failed", run_id=str(run.id))
            from apps.harness.engines.runs import RunOwnership

            owner = RunOwnership(run)
            if await owner.begin_close():
                await owner.finalize_turn(
                    status="error", finish="error", error="Run preparation failed."
                )
            service._drop_run_tracking(str(run.session_id))

    async def _prepare_owned_run(
        self, service, run: HarnessRun, worker_id: str, deployment, running: dict
    ) -> None:
        """Rebuild one attempt from durable messages and launch the harness loop."""
        run = await sync_to_async(
            HarnessRun.objects.select_related("session", "assistant_message").get,
            thread_sensitive=True,
        )(id=run.id)
        session = run.session
        session.model = run.assistant_message.model
        session.reasoning_effort = run.assistant_message.reasoning_effort
        user = await sync_to_async(
            lambda: (
                service.messages.model.objects.filter(
                    session_id=run.session_id,
                    role="user",
                    position__lt=run.assistant_message.position,
                )
                .order_by("-position")
                .first()
            ),
            thread_sensitive=True,
        )()
        if user is None:
            raise RuntimeError("Admitted user turn is missing")
        from apps.harness.engines.runs import RunOwnership
        from apps.harness.runtime_authority import RunAuthority

        owner = RunOwnership(run)
        context = service._worker_context(run, user)
        context["ownership"] = owner
        context["engine_id"] = run.harness_id
        context["initiated_by_id"] = run.initiated_by_id
        if session.skill_ids and run.initiated_by_id is not None:
            from apps.harness.harness_service import resolve_skill_bodies

            context["skill_bodies"] = await sync_to_async(resolve_skill_bodies)(
                list(session.skill_ids),
                user_id=run.initiated_by_id,
                organization_id=run.organization_id,
            )
        service._runs[str(session.id)] = context
        service._remember_run(session)
        service._worker_authorities[str(session.id)] = RunAuthority(
            run.id,
            run.owner_token,
            worker_id,
            run.deployment_version,
            run.root_run_id or run.id,
        )
        history = await service._build_history(
            session,
            exclude_message_id=run.assistant_message_id,
            exclude_user_message_id=user.id,
        )
        provider = None
        if session.harness_id == "native":
            from apps.harness.services import ProviderConfigService

            provider = await sync_to_async(
                ProviderConfigService().build_adapter, thread_sensitive=True
            )(
                session.organization_id,
                provider_name=run.assistant_message.provider or "openrouter",
            )
        depth = await sync_to_async(service.sessions.get_depth, thread_sensitive=True)(
            session.id
        )
        from apps.harness.services import SubagentConfigService

        max_depth = (
            await sync_to_async(
                SubagentConfigService().get_or_default, thread_sensitive=True
            )(session.organization_id)
        )["max_depth"]
        if run.abort_requested_at is not None:
            service._abort_requested.add(str(session.id))
        task = asyncio.create_task(
            service._execute_run(
                session=session,
                prompt=user.content,
                history=history,
                assistant=run.assistant_message,
                provider=provider,
                organization_id=session.organization_id,
                depth=depth,
                max_depth=max_depth,
            ),
            name=f"harness-worker-run-{run.id}",
        )
        running[str(run.id)] = task
        service._tasks[str(session.id)] = task
        task.add_done_callback(
            lambda done: self._run_done(
                done,
                str(run.id),
                str(session.id),
                running,
                service,
                session=session,
                assistant=run.assistant_message,
            )
        )
        if run.abort_requested_at is not None:
            # This command was explicitly stopped before execution entry. The
            # task must take the pre-execution finalizer path before any tool or
            # runner RPC can start.
            task.cancel()

    async def _handle_pre_drain_roots(
        self, worker_id: str, service, running: dict, deployment
    ) -> None:
        """Complete only commands durably admitted before draining_at."""
        commands = await sync_to_async(
            HarnessWorkerRepository.pending_pre_drain_roots, thread_sensitive=True
        )(worker_id)
        for command in commands:
            claimed = await sync_to_async(
                HarnessWorkerRepository.claim_pre_drain_root, thread_sensitive=True
            )(command.id, worker_id)
            if claimed is None:
                continue
            run = await sync_to_async(
                HarnessWorkerRepository.start_claimed, thread_sensitive=True
            )(claimed.id, worker_id)
            if run is None:
                continue
            await sync_to_async(
                HarnessWorkerRepository.finish_command, thread_sensitive=True
            )(claimed.id, worker_id)
            await self._spawn_owned_run(service, run, worker_id, deployment, running)

    @staticmethod
    async def _handle_abort_intents(worker_id: str, service, running: dict) -> None:
        rows = await sync_to_async(
            HarnessWorkerRepository.owned_runs, thread_sensitive=True
        )(worker_id)
        for run in rows:
            task = running.get(str(run.id)) or service._tasks.get(str(run.session_id))
            if (
                run.abort_requested_at is not None
                and task is not None
                and not task.done()
            ):
                service._abort_requested.add(str(run.session_id))
                task.cancel()

    @staticmethod
    async def _handle_gate_resolutions(service, running: dict) -> None:
        from apps.harness.models import HarnessRun, QuestionRequest
        from apps.harness.permissions.models import PermissionRequest
        from apps.harness.worker_protocol import HarnessWorkerRepository

        owned = await sync_to_async(HarnessWorkerRepository.owned_runs)(
            service.deployment.worker_id
        )
        ids = [row.id for row in owned]
        if not ids:
            return
        sessions = await sync_to_async(
            lambda: list(
                HarnessRun.objects.filter(id__in=ids).values_list(
                    "session_id", flat=True
                )
            ),
            thread_sensitive=True,
        )()
        perms = await sync_to_async(
            lambda: list(
                PermissionRequest.objects.filter(
                    session_id__in=sessions, status__in=("approved", "rejected")
                ).only("id", "status")
            ),
            thread_sensitive=True,
        )()
        for row in perms:
            service._wake_local_gate(
                row.id,
                permission=True,
                value="allow" if row.status == "approved" else "deny",
            )
        questions = await sync_to_async(
            lambda: list(
                QuestionRequest.objects.filter(
                    session_id__in=sessions,
                    status__in=("answered", "rejected", "timed_out"),
                ).only("id", "answers", "status")
            ),
            thread_sensitive=True,
        )()
        for row in questions:
            service._wake_local_gate(
                row.id,
                permission=False,
                value=list(row.answers or []),
                reject=row.status != "answered",
            )

    @staticmethod
    def _run_done(
        task: asyncio.Task[None],
        run_key: str,
        session_key: str,
        running: dict,
        service,
        *,
        session=None,
        assistant=None,
    ) -> None:
        running.pop(run_key, None)
        if service._tasks.get(session_key) is task:
            service._tasks.pop(session_key, None)
        if task.cancelled():
            if (
                session is not None
                and assistant is not None
                and not service._runs.get(session_key, {}).get("execution_started")
            ):
                finalizer = asyncio.create_task(
                    service._finalize_unstarted_run(session, assistant),
                    name=f"harness-worker-finalize-{run_key}",
                )
                service._tasks[session_key] = finalizer
                finalizer.add_done_callback(
                    lambda done: service._on_run_task_done(done, session_key)
                )
            return
        error = task.exception()
        if error is not None:
            log.error(
                "harness_worker_run_task_error",
                run_id=run_key,
                error=type(error).__name__,
            )
