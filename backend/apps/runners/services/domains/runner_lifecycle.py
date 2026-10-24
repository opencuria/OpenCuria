"""Runner presence and workspace queries shared by RunnerService.

Lifecycle delivery belongs to the independent durable recovery worker,
not runner registration.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from common.exceptions import AuthenticationError
from common.utils import generate_api_token, hash_token

from ...exceptions import RunnerNotFoundError, WorkspaceNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids runtime cycles
    from ...models import Runner, RunnerSystemMetrics, Workspace

logger = logging.getLogger(__name__)


class RunnerLifecycleMixin:
    """Runner lifecycle and read queries shared by RunnerService."""

    def create_runner(self, *, name: str, organization) -> tuple[Runner, str]:
        """Create a runner and return it with its one-time plaintext API token."""
        token = generate_api_token()
        runner = self.runners.create(
            name=name,
            api_token_hash=hash_token(token),
            organization=organization,
        )
        return runner, token

    def authenticate_runner(self, token: str) -> "Runner":
        """
        Authenticate a runner by its API token.

        Returns the Runner instance if valid, raises AuthenticationError otherwise.
        """
        token_hash = hash_token(token)
        runner = self.runners.get_by_token_hash(token_hash)
        if runner is None:
            raise AuthenticationError("Invalid runner API token")
        return runner

    def register_runner(
        self,
        runner: "Runner",
        *,
        sid: str,
        desktop_epoch: str | None = None,
    ) -> "Runner":
        """
        Mark a runner as online after it connects and sends runner:register.

        Args:
            runner: The authenticated Runner instance.
            sid: Socket.IO session ID for targeted messaging.
        """
        runner = self.runners.set_online(
            runner,
            sid=sid,
        )
        self._set_runner_desktop_epoch(str(runner.id), desktop_epoch)
        logger.info(
            "Runner registered: %s",
            runner.id,
        )
        return runner

    def unregister_runner(self, sid: str) -> None:
        """
        Mark a runner as offline when it disconnects.

        Looks up the runner by its Socket.IO session ID.
        """
        runner = self.runners.get_online_by_sid(sid)
        if runner is None:
            logger.warning("Disconnect from unknown SID: %s", sid)
            return

        self.runners.set_offline(runner)
        logger.info("Runner unregistered: %s", runner.id)
        # Fail any open byte streams so harness waiters surface offline
        # instead of hanging until their timeout.
        try:
            self.fail_streams_for_runner(str(runner.id))
        except Exception:
            logger.exception("Failed failing streams for runner %s", runner.id)

        # Notify frontend about runner going offline so it can update display.
        self._forward_runner_status_to_frontend(runner, "offline")

    def is_current_runner_session(
        self, runner_id: str, sid: str, *, require_online: bool = True
    ) -> bool:
        """Return whether *sid* is still the runner's authenticated session.

        Events from superseded sessions are fenced. Liveness reports pass
        ``require_online=False`` so an offline runner can come back online.
        """
        return self.runners.has_session(runner_id, sid, online_only=require_online)

    def record_runner_heartbeat(self, runner: Runner) -> Runner:
        """Refresh transport liveness of the runner's current session."""
        return self.runners.update_heartbeat(runner)

    def record_runner_system_metrics(self, runner: Runner, metrics: dict) -> None:
        """Persist one host metrics sample and prune samples older than 24 h."""
        reported_at = metrics.get("timestamp")
        vm_metrics = metrics.get("vm_metrics")
        self.runner_metrics.create(
            runner=runner,
            timestamp=(
                datetime.fromisoformat(reported_at.replace("Z", "+00:00"))
                if reported_at
                else timezone.now()
            ),
            cpu_usage_percent=float(metrics.get("cpu_usage_percent", 0.0)),
            ram_used_bytes=int(metrics.get("ram_used_bytes", 0)),
            ram_total_bytes=int(metrics.get("ram_total_bytes", 0)),
            disk_used_bytes=int(metrics.get("disk_used_bytes", 0)),
            disk_total_bytes=int(metrics.get("disk_total_bytes", 0)),
            vm_metrics=vm_metrics if isinstance(vm_metrics, dict) else None,
        )
        self.runner_metrics.purge_old(runner_id=runner.id)

    def get_latest_runner_metrics(
        self, runner_id: uuid.UUID
    ) -> RunnerSystemMetrics | None:
        """Return the newest host metrics sample of a runner, if any."""
        return self.runner_metrics.get_latest(runner_id)

    def list_runner_metrics(
        self, runner_id: uuid.UUID, *, hours: int
    ) -> list[RunnerSystemMetrics]:
        """Return the host metrics samples of the last *hours*, oldest first."""
        since = timezone.now() - timedelta(hours=hours)
        return list(self.runner_metrics.get_history(runner_id, since))

    def update_runner_qemu_settings(
        self,
        runner_id: uuid.UUID,
        **fields,
    ) -> "Runner":
        """Update per-runner QEMU resource limits/defaults."""
        runner = self.get_runner(runner_id)
        updated_fields = dict(fields)
        if not updated_fields:
            return runner
        for key, value in updated_fields.items():
            setattr(runner, key, value)
        self._validate_runner_qemu_limits(runner)
        return self.runners.update_qemu_settings(runner, **updated_fields)

    def list_runners(self, organization_id: uuid.UUID | None = None) -> list["Runner"]:
        """Return all registered runners, optionally filtered by organization."""
        if organization_id:
            return list(self.runners.list_by_organization(organization_id))
        return list(self.runners.list_all())

    def get_runner(self, runner_id: uuid.UUID) -> "Runner":
        """Return a runner by ID or raise RunnerNotFoundError."""
        runner = self.runners.get_by_id(runner_id)
        if runner is None:
            raise RunnerNotFoundError(str(runner_id))
        return runner

    def get_runner_or_none(self, runner_id: uuid.UUID) -> Runner | None:
        """Return a runner by ID or None (no raise)."""
        return self.runners.get_by_id(runner_id)

    def list_workspaces(
        self,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        user=None,
    ) -> list["Workspace"]:
        """Return workspaces filtered by org and owner."""
        workspaces = self.workspaces.list_for_scope(
            runner_id=runner_id, organization_id=organization_id, user=user
        )
        self._populate_workspace_activity(workspaces)
        return workspaces

    def get_workspace(self, workspace_id: uuid.UUID) -> "Workspace":
        """Return a workspace by ID or raise WorkspaceNotFoundError."""
        workspace = self.get_workspace_or_none(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        return workspace

    def get_workspace_or_none(self, workspace_id: uuid.UUID) -> Workspace | None:
        """Return a workspace with current activity, or None when missing."""
        workspace = self.workspaces.get_by_id(workspace_id)
        if workspace is not None:
            self._populate_workspace_activity([workspace])
        return workspace

    def _populate_workspace_activity(self, workspaces: list[Workspace]) -> None:
        """Compose workspace reads with harness activity through its port."""
        if not workspaces:
            return
        busy_ids = self._harness().busy_workspace_ids(
            [workspace.id for workspace in workspaces]
        )
        for workspace in workspaces:
            workspace.has_active_harness_session = workspace.id in busy_ids
