"""Durable gateway/worker protocol for versioned harness execution."""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

from django.db import IntegrityError, models, transaction
from django.db.models import F
from django.utils import timezone

from common.exceptions import ConflictError

from .models import (
    HarnessMessage,
    HarnessRun,
    HarnessRunCommand,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
    HarnessWorkerRegistration,
)

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class HarnessDeployment:
    """Validated role, immutable application version, and process identity."""

    mode: str
    version: str
    worker_id: str
    gateway_worker_versions: tuple[str, ...] = ()
    gateway_role: str = "gateway"

    @classmethod
    def from_environment(cls) -> HarnessDeployment:
        """Validate role/version/worker configuration against the release manifest."""
        mode = os.environ.get("OPENCURIA_HARNESS_MODE", "local").strip().lower()
        if mode not in {"local", "gateway", "worker"}:
            raise ValueError("OPENCURIA_HARNESS_MODE must be local|gateway|worker")
        version = os.environ.get("OPENCURIA_DEPLOYMENT_VERSION", "local").strip()
        worker_id = os.environ.get("OPENCURIA_WORKER_ID", "").strip()
        if mode != "local":
            if not _TOKEN.fullmatch(version):
                raise ValueError("OPENCURIA_DEPLOYMENT_VERSION is required and invalid")
            from django.conf import settings

            configured = str(
                getattr(settings, "DEPLOYMENT_RELEASE", {}).get("version", "")
            )
            if not _TOKEN.fullmatch(configured) or configured != version:
                raise ValueError(
                    "OPENCURIA_DEPLOYMENT_VERSION must match the "
                    "container release manifest"
                )
        if mode == "worker" and (
            not _TOKEN.fullmatch(worker_id) or len(worker_id) > 96
        ):
            raise ValueError("OPENCURIA_WORKER_ID is required and invalid")
        raw_versions = os.environ.get("OPENCURIA_GATEWAY_WORKER_VERSIONS", version)
        gateway_role = (
            os.environ.get(
                "OPENCURIA_GATEWAY_ROLE", "worker" if mode == "worker" else "gateway"
            )
            .strip()
            .lower()
        )
        if mode == "gateway" and gateway_role not in {"api", "gateway", "worker"}:
            raise ValueError("OPENCURIA_GATEWAY_ROLE must be api|gateway|worker")
        if mode == "gateway" and gateway_role == "api":
            # The deployment manifest calls this role `api`; the Socket.IO
            # compatibility contract has historically called it `gateway`.
            gateway_role = "gateway"
        if mode == "worker":
            gateway_role = "worker"
        versions = tuple(
            dict.fromkeys(
                item.strip() for item in raw_versions.split(",") if item.strip()
            )
        )
        if mode == "gateway":
            if not versions or any(not _TOKEN.fullmatch(item) for item in versions):
                raise ValueError("OPENCURIA_GATEWAY_WORKER_VERSIONS is invalid")
        return cls(mode, version, worker_id, versions, gateway_role)


class HarnessWorkerRepository:
    """Database boundary for worker readiness, admission, outbox, and drain."""

    @staticmethod
    def register(
        worker_id: str, version: str, *, pid: int
    ) -> HarnessWorkerRegistration:
        """Register a never-before-used process ID without adopting old runs."""
        if not _TOKEN.fullmatch(worker_id) or len(worker_id) > 96:
            raise ValueError("Worker identity is invalid")
        if not _TOKEN.fullmatch(version):
            raise ValueError("Deployment version is invalid")
        now = timezone.now()
        try:
            return HarnessWorkerRegistration.objects.create(
                worker_id=worker_id,
                deployment_version=version,
                pid=pid,
                state=HarnessWorkerRegistration.State.READY,
                heartbeat_at=now,
                started_at=now,
            )
        except IntegrityError as exc:
            raise ConflictError(
                "Worker id was already used; each worker process needs a new id"
            ) from exc

    @staticmethod
    def get_registration(worker_id: str) -> HarnessWorkerRegistration | None:
        """Read a registration without changing liveness."""
        return HarnessWorkerRegistration.objects.filter(worker_id=worker_id).first()

    @staticmethod
    def heartbeat(worker_id: str) -> bool:
        """Refresh ready/draining liveness without clearing drain state."""
        return bool(
            HarnessWorkerRegistration.objects.filter(
                worker_id=worker_id,
                state__in=(
                    HarnessWorkerRegistration.State.READY,
                    HarnessWorkerRegistration.State.DRAINING,
                ),
            ).update(heartbeat_at=timezone.now())
        )

    @staticmethod
    def set_draining(worker_id: str) -> bool:
        """Serialize admission and drain against the same registration lock."""
        with transaction.atomic():
            row = (
                HarnessWorkerRegistration.objects.select_for_update()
                .filter(
                    worker_id=worker_id,
                    state=HarnessWorkerRegistration.State.READY,
                )
                .first()
            )
            if row is None:
                return False
            row.state = HarnessWorkerRegistration.State.DRAINING
            row.draining_at = timezone.now()
            row.save(update_fields=["state", "draining_at"])
            return True

    @staticmethod
    def mark_stopped(worker_id: str) -> None:
        """Mark graceful exit only; never finalize/release an outstanding run."""
        HarnessWorkerRegistration.objects.filter(worker_id=worker_id).update(
            state=HarnessWorkerRegistration.State.STOPPED,
            heartbeat_at=timezone.now(),
        )

    @staticmethod
    def ready_for_version(version: str) -> bool:
        """Report a compatible, fresh, non-draining worker registration."""
        return HarnessWorkerRegistration.objects.filter(
            deployment_version=version,
            state=HarnessWorkerRegistration.State.READY,
            heartbeat_at__gte=timezone.now()
            - HarnessWorkerRegistration.HEARTBEAT_TIMEOUT,
        ).exists()

    @staticmethod
    def select_ready_worker(
        versions: tuple[str, ...], *, preferred_version: str
    ) -> HarnessWorkerRegistration | None:
        """Select and row-lock a compatible, heartbeat-fresh worker."""
        if not versions:
            return None
        # Drain/rollback state is a deployment-wide admission fence even if a
        # previously disconnected worker process has just re-registered.
        from apps.deployments.models import Release

        draining = Release.objects.filter(
            state__in=("draining", "retired")
        ).values_list("version", flat=True)
        query = (
            HarnessWorkerRegistration.objects.exclude(deployment_version__in=draining)
            .filter(
                deployment_version__in=versions,
                state=HarnessWorkerRegistration.State.READY,
                heartbeat_at__gte=timezone.now()
                - HarnessWorkerRegistration.HEARTBEAT_TIMEOUT,
            )
            .order_by("worker_id")
        )
        if transaction.get_connection().in_atomic_block:
            query = query.select_for_update()
        workers = list(query)
        return next(
            (
                worker
                for worker in workers
                if worker.deployment_version == preferred_version
            ),
            workers[0] if workers else None,
        )

    @staticmethod
    def drained(worker_id: str) -> bool:
        """Check owned runs and starts committed before the drain cut."""
        worker = HarnessWorkerRegistration.objects.filter(worker_id=worker_id).first()
        if worker is None or worker.state != HarnessWorkerRegistration.State.DRAINING:
            return False
        if HarnessRun.objects.filter(
            owner_worker_id=worker_id,
            status__in=(
                HarnessRunStatus.STARTING,
                HarnessRunStatus.RUNNING,
                HarnessRunStatus.CLOSING,
            ),
        ).exists():
            return False
        return not HarnessRunCommand.objects.filter(
            command_type=HarnessRunCommand.Type.START,
            state__in=(
                HarnessRunCommand.State.PENDING,
                HarnessRunCommand.State.CLAIMED,
            ),
            target_worker_id=worker_id,
            run__root_run_id__isnull=True,
            created_at__lte=worker.draining_at,
        ).exists()

    @staticmethod
    def admit_run(
        *,
        session_id: uuid.UUID,
        prompt: str,
        model: str,
        reasoning_effort: str,
        provider: str,
        skill_ids: list[str],
        engine_id: str,
        initiated_by_id: int | None,
        deployment_version: str,
        gateway: bool,
        admission_versions: tuple[str, ...] = (),
        owner_worker_id: str = "",
        scheduled: bool = False,
        inherited_worker_id: str = "",
        inherited_version: str = "",
        root_run_id: uuid.UUID | None = None,
    ) -> tuple[HarnessMessage, HarnessMessage, HarnessRun, int]:
        """Atomically reserve the workspace/session and persist turn + outbox."""
        from apps.runners.locking import lock_runner
        from apps.runners.models import Workspace

        with transaction.atomic():
            session = HarnessSession.objects.get(id=session_id)
            runner_id = Workspace.objects.values_list("runner_id", flat=True).get(
                id=session.workspace_id
            )
            lock_runner(runner_id)
            workspace_query = Workspace.objects.filter(id=session.workspace_id)
            if scheduled:
                workspace_query = workspace_query.filter(
                    status="running", active_operation__isnull=True
                )
            if not workspace_query.update(updated_at=F("updated_at")):
                raise ConflictError("Workspace is not available for harness runs")
            from .repositories import HarnessSessionRepository

            HarnessSessionRepository.ensure_interactions_available(session.workspace_id)
            session = HarnessSession.objects.get(id=session_id)
            if session.status != HarnessSessionStatus.IDLE:
                raise ConflictError(
                    f"Harness session '{session.id}' already has an active run"
                )
            if (
                scheduled
                and HarnessSession.objects.filter(
                    workspace_id=session.workspace_id,
                    parent__isnull=True,
                    status=HarnessSessionStatus.BUSY,
                )
                .exclude(id=session.id)
                .exists()
            ):
                raise ConflictError("Another chat is already active in this workspace")

            selected_worker = None
            effective_worker = inherited_worker_id or owner_worker_id
            if gateway and not inherited_worker_id:
                selected_worker = HarnessWorkerRepository.select_ready_worker(
                    admission_versions or (deployment_version,),
                    preferred_version=deployment_version,
                )
                if selected_worker is None:
                    raise ConflictError("No ready harness worker for this release")
                effective_worker = selected_worker.worker_id
                deployment_version = selected_worker.deployment_version

            if session.parent_id is not None:
                parent_run = (
                    HarnessRun.objects.select_for_update()
                    .filter(
                        session_id=session.parent_id,
                        status__in=(
                            HarnessRunStatus.STARTING,
                            HarnessRunStatus.RUNNING,
                            HarnessRunStatus.CLOSING,
                        ),
                    )
                    .first()
                )
                if (
                    parent_run is None
                    or parent_run.status != HarnessRunStatus.RUNNING
                    or parent_run.abort_requested_at is not None
                ):
                    raise ConflictError("Subagent parent run is no longer active")
                if (
                    inherited_worker_id
                    and parent_run.owner_worker_id != inherited_worker_id
                ):
                    raise ConflictError(
                        "Subagent worker does not match its parent owner"
                    )
                if (
                    inherited_version
                    and parent_run.deployment_version != inherited_version
                ):
                    raise ConflictError(
                        "Subagent release does not match its parent owner"
                    )
                expected_root = parent_run.root_run_id or parent_run.id
                if root_run_id and root_run_id != expected_root:
                    raise ConflictError("Subagent root owner does not match its parent")
                effective_worker = parent_run.owner_worker_id
                deployment_version = parent_run.deployment_version
                root_run_id = expected_root

            HarnessSession.objects.filter(id=session.id).update(
                updated_at=F("updated_at")
            )
            positions = list(
                HarnessMessage.objects.filter(session_id=session.id).values_list(
                    "position", flat=True
                )
            )
            position = max(positions, default=-1) + 1
            user_message = HarnessMessage.objects.create(
                session=session,
                role="user",
                content=prompt,
                skill_ids=list(skill_ids),
                harness_id=engine_id,
                position=position,
            )
            assistant = HarnessMessage.objects.create(
                session=session,
                role="assistant",
                model=model,
                reasoning_effort=reasoning_effort,
                provider=provider,
                harness_id=engine_id,
                position=position + 1,
            )
            now = timezone.now()
            run = HarnessRun.objects.create(
                session=session,
                assistant_message=assistant,
                organization_id=session.organization_id,
                initiated_by_id=initiated_by_id,
                harness_id=engine_id[:16],
                owner_worker_id="" if gateway else effective_worker,
                deployment_version=deployment_version,
                root_run_id=root_run_id,
                status=(
                    HarnessRunStatus.RUNNING
                    if effective_worker and not gateway
                    else HarnessRunStatus.STARTING
                ),
                heartbeat_at=now,
            )
            HarnessSession.objects.filter(id=session.id).update(
                status=HarnessSessionStatus.BUSY,
                last_message_at=now,
                updated_at=now,
            )
            HarnessMessage.objects.filter(
                session_id=session.id,
                role="assistant",
                notice_dismissed_at__isnull=True,
            ).exclude(id=assistant.id).filter(
                models.Q(error__gt="") | models.Q(finish__in=("aborted", "error"))
            ).update(notice_dismissed_at=now)
            if gateway:
                HarnessRunCommand.objects.create(
                    run=run,
                    command_type=HarnessRunCommand.Type.START,
                    deployment_version=deployment_version,
                    target_worker_id=effective_worker,
                )
            prior_users = HarnessMessage.objects.filter(
                session_id=session.id, role="user", position__lt=position
            ).count()
            return user_message, assistant, run, prior_users

    @staticmethod
    def pending_pre_drain_roots(worker_id: str) -> list[HarnessRunCommand]:
        """List root starts addressed to this worker before its drain fence."""
        worker = HarnessWorkerRegistration.objects.filter(worker_id=worker_id).first()
        if worker is None or worker.state != HarnessWorkerRegistration.State.DRAINING:
            return []
        return list(
            HarnessRunCommand.objects.filter(
                state=HarnessRunCommand.State.PENDING,
                command_type=HarnessRunCommand.Type.START,
                target_worker_id=worker_id,
                deployment_version=worker.deployment_version,
                created_at__lte=worker.draining_at,
                run__root_run_id__isnull=True,
                run__status=HarnessRunStatus.STARTING,
            ).order_by("created_at", "id")[:64]
        )

    @staticmethod
    def claim_pre_drain_root(
        command_id: uuid.UUID, worker_id: str
    ) -> HarnessRunCommand | None:
        """Claim one pre-drain root admission without any stale-claim replay."""
        with transaction.atomic():
            worker = (
                HarnessWorkerRegistration.objects.select_for_update()
                .filter(
                    worker_id=worker_id,
                    state=HarnessWorkerRegistration.State.DRAINING,
                )
                .first()
            )
            if worker is None:
                return None
            command = (
                HarnessRunCommand.objects.select_for_update()
                .filter(
                    id=command_id,
                    state=HarnessRunCommand.State.PENDING,
                    command_type=HarnessRunCommand.Type.START,
                    target_worker_id=worker_id,
                    deployment_version=worker.deployment_version,
                    created_at__lte=worker.draining_at,
                    run__root_run_id__isnull=True,
                    run__status=HarnessRunStatus.STARTING,
                )
                .first()
            )
            if command is None:
                return None
            changed = HarnessRunCommand.objects.filter(
                id=command.id,
                state=HarnessRunCommand.State.PENDING,
                claimed_by="",
                target_worker_id=worker_id,
                created_at__lte=worker.draining_at,
            ).update(
                state=HarnessRunCommand.State.CLAIMED,
                claimed_by=worker_id,
                claimed_at=timezone.now(),
            )
            if not changed:
                return None
            HarnessRun.objects.filter(
                id=command.run_id,
                status=HarnessRunStatus.STARTING,
                owner_worker_id="",
            ).update(owner_worker_id=worker_id)
            command.refresh_from_db()
            return command

    @staticmethod
    def claim_next(worker_id: str, version: str) -> HarnessRunCommand | None:
        """Claim a targeted root/owned child start once; never replay claims."""
        with transaction.atomic():
            worker = (
                HarnessWorkerRegistration.objects.select_for_update()
                .filter(
                    worker_id=worker_id,
                    deployment_version=version,
                    state__in=(
                        HarnessWorkerRegistration.State.READY,
                        HarnessWorkerRegistration.State.DRAINING,
                    ),
                )
                .first()
            )
            if worker is None:
                return None
            candidates = (
                HarnessRunCommand.objects.filter(
                    state=HarnessRunCommand.State.PENDING,
                    deployment_version=version,
                    target_worker_id=worker_id,
                    claimed_by="",
                    command_type=HarnessRunCommand.Type.START,
                    run__status=HarnessRunStatus.STARTING,
                    run__deployment_version=version,
                    run__owner_worker_id="",
                )
                .select_related("run")
                .order_by("created_at", "id")
            )
            if worker.state == HarnessWorkerRegistration.State.DRAINING:
                candidates = candidates.filter(created_at__lte=worker.draining_at)
            for command in candidates[:64]:
                run = command.run
                if run.root_run_id:
                    root = HarnessRun.objects.filter(
                        id=run.root_run_id,
                        owner_worker_id=worker_id,
                        deployment_version=version,
                        status=HarnessRunStatus.RUNNING,
                        abort_requested_at__isnull=True,
                    ).first()
                    if root is None:
                        continue
                    if run.session.parent_id is not None:
                        parent_is_running = HarnessRun.objects.filter(
                            session_id=run.session.parent_id,
                            status=HarnessRunStatus.RUNNING,
                            owner_worker_id=worker_id,
                            deployment_version=version,
                            abort_requested_at__isnull=True,
                        ).exists()
                        if not parent_is_running:
                            continue
                changed = HarnessRunCommand.objects.filter(
                    id=command.id,
                    state=HarnessRunCommand.State.PENDING,
                    claimed_by="",
                    target_worker_id=worker_id,
                    run__status=HarnessRunStatus.STARTING,
                    run__deployment_version=version,
                    run__owner_worker_id="",
                ).update(
                    state=HarnessRunCommand.State.CLAIMED,
                    claimed_by=worker_id,
                    claimed_at=timezone.now(),
                )
                if changed:
                    HarnessRun.objects.filter(
                        id=command.run_id,
                        status=HarnessRunStatus.STARTING,
                        owner_worker_id="",
                    ).update(owner_worker_id=worker_id)
                    command.refresh_from_db()
                    return command
            return None

    @staticmethod
    def start_claimed(command_id: uuid.UUID, worker_id: str) -> HarnessRun | None:
        """Bind run ownership and transition to RUNNING exactly once."""
        with transaction.atomic():
            command = (
                HarnessRunCommand.objects.select_for_update()
                .filter(
                    id=command_id,
                    state=HarnessRunCommand.State.CLAIMED,
                    claimed_by=worker_id,
                    target_worker_id=worker_id,
                    command_type=HarnessRunCommand.Type.START,
                )
                .select_related("run")
                .first()
            )
            if command is None:
                return None
            changed = HarnessRun.objects.filter(
                id=command.run_id,
                status=HarnessRunStatus.STARTING,
                owner_worker_id=worker_id,
                deployment_version=command.deployment_version,
            ).update(
                owner_worker_id=worker_id,
                status=HarnessRunStatus.RUNNING,
                heartbeat_at=timezone.now(),
            )
            if not changed:
                return None
            return HarnessRun.objects.select_related(
                "session", "assistant_message"
            ).get(id=command.run_id)

    @staticmethod
    def finish_command(command_id: uuid.UUID, worker_id: str) -> bool:
        """Acknowledge only a start/control command claimed by this worker."""
        return bool(
            HarnessRunCommand.objects.filter(
                id=command_id,
                state=HarnessRunCommand.State.CLAIMED,
                claimed_by=worker_id,
            ).update(state=HarnessRunCommand.State.DONE, completed_at=timezone.now())
        )

    @staticmethod
    def request_abort(session_ids: list[uuid.UUID]) -> int:
        """Persist user stop intent and owner-targeted abort commands."""
        count = 0
        with transaction.atomic():
            runs = HarnessRun.objects.select_for_update().filter(
                session_id__in=session_ids,
                status__in=(
                    HarnessRunStatus.STARTING,
                    HarnessRunStatus.RUNNING,
                    HarnessRunStatus.CLOSING,
                ),
            )
            for run in runs:
                HarnessRun.objects.filter(
                    id=run.id, abort_requested_at__isnull=True
                ).update(abort_requested_at=timezone.now())
                if run.owner_worker_id:
                    HarnessRunCommand.objects.update_or_create(
                        run=run,
                        command_type=HarnessRunCommand.Type.ABORT,
                        dedupe_key="",
                        defaults={
                            "deployment_version": run.deployment_version,
                            "target_worker_id": run.owner_worker_id,
                            "state": HarnessRunCommand.State.PENDING,
                            "claimed_by": "",
                            "claimed_at": None,
                            "completed_at": None,
                        },
                    )
                count += 1
        return count

    @staticmethod
    def owned_runs(worker_id: str) -> list[HarnessRun]:
        """Read all unresolved attempts pinned to this process."""
        return list(
            HarnessRun.objects.filter(
                owner_worker_id=worker_id,
                status__in=(
                    HarnessRunStatus.STARTING,
                    HarnessRunStatus.RUNNING,
                    HarnessRunStatus.CLOSING,
                ),
            ).only("id", "session_id", "abort_requested_at")
        )

    @staticmethod
    def claim_control_commands(worker_id: str) -> list[HarnessRunCommand]:
        """Claim durable abort notifications for active owned runs."""
        claimed: list[HarnessRunCommand] = []
        with transaction.atomic():
            pending = (
                HarnessRunCommand.objects.filter(
                    state=HarnessRunCommand.State.PENDING,
                    command_type=HarnessRunCommand.Type.ABORT,
                    target_worker_id=worker_id,
                    run__owner_worker_id=worker_id,
                )
                .select_related("run")
                .order_by("created_at", "id")[:128]
            )
            for command in pending:
                changed = HarnessRunCommand.objects.filter(
                    id=command.id,
                    state=HarnessRunCommand.State.PENDING,
                    claimed_by="",
                    target_worker_id=worker_id,
                    run__owner_worker_id=worker_id,
                ).update(
                    state=HarnessRunCommand.State.CLAIMED,
                    claimed_by=worker_id,
                    claimed_at=timezone.now(),
                )
                if changed:
                    command.state = HarnessRunCommand.State.CLAIMED
                    claimed.append(command)
        return claimed

    @staticmethod
    def resolved_gate_ids(
        worker_id: str,
    ) -> tuple[list[tuple[str, str]], list[tuple[str, list]]]:
        """Read persisted gate resolutions for process-local waiter recovery."""
        from .models import QuestionRequest
        from .permissions.models import PermissionRequest

        run_ids = HarnessRun.objects.filter(
            owner_worker_id=worker_id,
            status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
        ).values_list("session_id", flat=True)
        permissions = list(
            PermissionRequest.objects.filter(
                session_id__in=run_ids,
                status__in=("approved", "rejected"),
            ).values_list("id", "status")
        )
        questions = list(
            QuestionRequest.objects.filter(
                session_id__in=run_ids,
                status__in=("answered", "rejected", "timed_out"),
            ).values_list("id", "answers", "status")
        )
        return (
            [
                (str(request_id), "allow" if status == "approved" else "deny")
                for request_id, status in permissions
            ],
            [
                (str(request_id), list(answers or []) if status == "answered" else [])
                for request_id, answers, status in questions
            ],
        )

    @staticmethod
    def recover_dead_worker(worker_id: str, *, confirmed_dead: bool) -> int:
        """Count unresolved attempts only after explicit worker-death proof."""
        if not confirmed_dead:
            raise ValueError("confirmed_dead must be explicitly true")
        worker = HarnessWorkerRegistration.objects.filter(worker_id=worker_id).first()
        if worker is None or worker.state not in {
            HarnessWorkerRegistration.State.STOPPED,
            HarnessWorkerRegistration.State.FAILED,
        }:
            raise ConflictError("Worker must be marked stopped before recovery")
        return HarnessRun.objects.filter(
            owner_worker_id=worker_id,
            status__in=(
                HarnessRunStatus.STARTING,
                HarnessRunStatus.RUNNING,
                HarnessRunStatus.CLOSING,
            ),
        ).count()
