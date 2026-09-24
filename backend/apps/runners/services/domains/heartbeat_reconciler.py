"""Heartbeat reconciliation and auto-stop (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.runners``,
``self.workspaces``, ``self.processes``,
``self._pending_unknown_workspace_cleanup`` /
``self._pending_process_verify`` plus sibling helpers (``stop_workspace``
via :class:`WorkspaceLifecycleMixin`, ``_cleanup_desktop_state`` /
``_sync_desktop_state_from_heartbeat`` via ``SessionStoreMixin``,
``_forward_workspace_status`` via ``FrontendBusMixin``,
``reconcile_workspace_processes`` / ``sweep_unconfirmed_processes`` /
``mark_processes_killed`` / ``reverify_vanished_processes`` via
:class:`ProcessManagerMixin`). The top-level ``django.utils.timezone``
import serves the ``auto_stop`` helpers; ``handle_heartbeat`` needs no
clock import (it never calls ``now()``).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from asgiref.sync import async_to_sync, sync_to_async
from django.utils import timezone

from common.exceptions import ConflictError

from ...enums import WorkspaceStatus
from ...exceptions import RunnerOfflineError, WorkspaceStateError

logger = logging.getLogger(__name__)


class HeartbeatReconcilerMixin:
    """Heartbeat handling and auto-stop shared by RunnerService."""

    def _get_workspace_auto_stop_deadline(self, workspace) -> datetime | None:
        """Return the inactivity deadline for a workspace, or None when disabled."""
        organization = getattr(getattr(workspace, "runner", None), "organization", None)
        timeout_minutes = getattr(
            organization,
            "workspace_auto_stop_timeout_minutes",
            None,
        )
        if (
            timeout_minutes is None
            or workspace.status != WorkspaceStatus.RUNNING
            or workspace.last_activity_at is None
        ):
            return None
        return workspace.last_activity_at + timedelta(minutes=timeout_minutes)

    def _should_auto_stop_workspace(
        self,
        workspace,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Return True if inactivity policy requires stopping the workspace."""
        if workspace.status != WorkspaceStatus.RUNNING:
            return False
        if workspace.active_operation:
            return False
        runner = getattr(workspace, "runner", None)
        if runner is None or not runner.is_online:
            return False
        deadline = self._get_workspace_auto_stop_deadline(workspace)
        if deadline is None:
            return False
        return deadline <= (now or timezone.now())

    async def auto_stop_inactive_workspaces(
        self,
        *,
        runner_id: uuid.UUID | None = None,
        organization_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> list["Task"]:
        """Stop running workspaces whose inactivity deadline has elapsed."""
        evaluation_time = now or timezone.now()
        if runner_id is not None:
            workspaces = await sync_to_async(
                lambda: list(self.workspaces.list_by_runner(runner_id))
            )()
        elif organization_id is not None:
            workspaces = await sync_to_async(
                lambda: list(self.workspaces.list_by_organization(organization_id))
            )()
        else:
            workspaces = await sync_to_async(lambda: list(self.workspaces.list_all()))()

        dispatched: list["Task"] = []
        for workspace in workspaces:
            if not self._should_auto_stop_workspace(workspace, now=evaluation_time):
                continue
            try:
                task = await self.stop_workspace(workspace.id)
            except (ConflictError, RunnerOfflineError, WorkspaceStateError) as exc:
                logger.info(
                    "Skipped auto-stop for workspace %s: %s",
                    workspace.id,
                    exc,
                )
                continue
            logger.info(
                "Dispatched inactivity auto-stop for workspace %s",
                workspace.id,
            )
            dispatched.append(task)

        return dispatched

    def handle_heartbeat(
        self,
        runner: "Runner",
        workspaces: list[dict],
    ) -> list[uuid.UUID]:
        """Handle runner:heartbeat event — reconcile workspace state.

        Compares the runner's reported container states with the backend's
        records and updates any stale entries.

        Args:
            runner: The Runner that sent the heartbeat.
            workspaces: List of dicts with workspace_id and status.

        Returns:
            Workspace IDs whose on-disk credentials need live reconciliation.
        """
        # Update heartbeat timestamp
        self.runners.update_heartbeat(runner)

        # Build lookup of runner-reported workspace states
        runner_ws_payloads: dict[str, dict] = {}
        runner_ws_states: dict[str, str] = {}
        for ws_data in workspaces:
            ws_id = ws_data.get("workspace_id", "")
            status = ws_data.get("status", "unknown")
            runner_ws_payloads[ws_id] = ws_data
            runner_ws_states[ws_id] = status

        # Check backend workspaces for this runner
        backend_workspaces = list(self.workspaces.list_by_runner(runner.id))
        backend_workspace_ids = {str(ws.id) for ws in backend_workspaces}
        runner_id_str = str(runner.id)

        # Drop stale pending cleanup entries that are no longer present on the
        # runner heartbeat.
        active_pending = {
            key
            for key in self._pending_unknown_workspace_cleanup
            if key[0] == runner_id_str
        }
        for key in active_pending:
            if key[1] not in runner_ws_states:
                self._pending_unknown_workspace_cleanup.discard(key)

        # Runner reported instances that backend does not know: request cleanup.
        unknown_workspace_ids = sorted(
            set(runner_ws_states.keys()) - backend_workspace_ids
        )
        for unknown_workspace_id in unknown_workspace_ids:
            self._request_workspace_cleanup(
                runner,
                workspace_id=unknown_workspace_id,
                reason="unknown_runtime_workspace",
            )

        credential_sync_ids: list[uuid.UUID] = []
        # Vanished process rows collected during the sync reconcile pass.
        # The heartbeat itself is sync (no RPC allowed), so verification
        # against the runner happens in ``reconcile_vanished_processes``
        # (async, called by the Socket.IO handler after this returns).
        pending_vanished: dict[str, list["WorkspaceProcess"]] = {}
        for ws in backend_workspaces:
            ws_id_str = str(ws.id)
            cleanup_key = (runner_id_str, ws_id_str)
            runner_status = runner_ws_states.get(ws_id_str)
            runner_payload = runner_ws_payloads.get(ws_id_str, {})

            if ws.status in (
                WorkspaceStatus.FAILED,
                WorkspaceStatus.REMOVED,
            ):
                if runner_status is not None:
                    self._request_workspace_cleanup(
                        runner,
                        workspace_id=ws_id_str,
                        reason=f"backend_terminal_state:{ws.status}",
                    )
                else:
                    self._pending_unknown_workspace_cleanup.discard(cleanup_key)
                continue

            if ws.status in (
                WorkspaceStatus.PENDING_DELETION,
                WorkspaceStatus.DELETING,
                WorkspaceStatus.DELETE_FAILED,
                WorkspaceStatus.DELETED,
            ):
                self._cleanup_desktop_state(ws_id_str)
                continue

            # This workspace is backend-managed and non-terminal.
            self._pending_unknown_workspace_cleanup.discard(cleanup_key)

            if runner_status is None:
                # Workspace exists in backend but not on runner —
                # container was removed externally. Terminal states
                # (FAILED/REMOVED/...) are steady: never touch them here
                # (a reconnecting runner whose cache is still syncing
                # would otherwise flip FAILED back and forth, and a
                # FAILED workspace must stay usable for its sessions
                # until the user deletes it).
                if ws.status in (
                    WorkspaceStatus.RUNNING,
                    WorkspaceStatus.STOPPED,
                ):
                    logger.warning(
                        "Workspace %s missing from runner %s, marking failed",
                        ws_id_str,
                        runner.id,
                    )
                    self.workspaces.update_status(ws, WorkspaceStatus.FAILED)
                    self._forward_workspace_status(ws, status="failed")
                    self.mark_processes_killed(
                        ws_id_str, reason="workspace_missing_from_runner"
                    )
                self._cleanup_desktop_state(ws_id_str)
            else:
                # Map Docker container status to workspace status
                new_status = self._map_instance_status(runner_status)
                if new_status and new_status != ws.status:
                    # Never promote CREATING → RUNNING via heartbeat.
                    # Only the explicit workspace:created event (sent after
                    # repos are cloned and SSH is established) may do that.
                    if (
                        ws.status == WorkspaceStatus.CREATING
                        and new_status == WorkspaceStatus.RUNNING
                    ):
                        continue
                    logger.info(
                        "Heartbeat: workspace %s status %s -> %s",
                        ws_id_str,
                        ws.status,
                        new_status,
                    )
                    self.workspaces.update_status(ws, new_status)
                    self._forward_workspace_status(ws, status=new_status)

                if not (
                    new_status == WorkspaceStatus.RUNNING
                    or (new_status is None and ws.status == WorkspaceStatus.RUNNING)
                ):
                    self._cleanup_desktop_state(ws_id_str)
                    effective = new_status or ws.status
                    if effective == WorkspaceStatus.STOPPED:
                        try:
                            self.mark_processes_killed(
                                ws_id_str, reason="workspace_stopped"
                            )
                        except Exception:
                            logger.exception(
                                "Failed killing processes for workspace %s",
                                ws_id_str,
                            )
                    continue

                has_credentials = ws.credentials.exists()
                if (not ws.credentials_present and has_credentials) or (
                    ws.credentials_present and not has_credentials
                ):
                    credential_sync_ids.append(ws.id)

                # Reconcile background processes with the reported list.
                # Only while the workspace is running. Vanished rows are
                # NOT marked exited here: the heartbeat is sync (no RPC),
                # so they are collected for the async
                # ``reconcile_vanished_processes`` pass after verify.
                # Live-exited rows are marked EXITED immediately.
                reported_processes = runner_payload.get("processes")
                if isinstance(reported_processes, list) and (
                    new_status == WorkspaceStatus.RUNNING
                    or (
                        new_status is None
                        and ws.status == WorkspaceStatus.RUNNING
                    )
                ):
                    try:
                        _, vanished = self.reconcile_workspace_processes(
                            ws_id_str, reported_processes
                        )
                        self.sweep_unconfirmed_processes(ws_id_str)
                        if vanished:
                            pending_vanished.setdefault(ws_id_str, []).extend(
                                vanished
                            )
                    except Exception:
                        logger.exception(
                            "Failed reconciling processes for workspace %s",
                            ws_id_str,
                        )

                if "desktop" in runner_payload:
                    self._sync_desktop_state_from_heartbeat(
                        ws_id_str,
                        runner_payload.get("desktop"),
                        runner_id=runner_id_str,
                    )

        async_to_sync(self.auto_stop_inactive_workspaces)(runner_id=runner.id)
        # Stash vanished candidates for the async verify pass: the sync
        # heartbeat cannot do RPCs, so ``reconcile_vanished_processes``
        # (called by the Socket.IO handler) picks them up. Merged per
        # workspace id so repeated heartbeats before the async pass
        # cannot duplicate rows.
        for ws_id_str, rows in pending_vanished.items():
            known = {
                str(process.id)
                for process in self._pending_process_verify.get(ws_id_str, [])
            }
            bucket = self._pending_process_verify.setdefault(ws_id_str, [])
            for process in rows:
                if str(process.id) not in known:
                    bucket.append(process)
                    known.add(str(process.id))
        return credential_sync_ids

    async def reconcile_vanished_processes(
        self,
        runner: "Runner",
    ) -> list[uuid.UUID]:
        """Verify heartbeat-vanished process rows against the runner.

        Called by the Socket.IO heartbeat handler after the sync
        :meth:`handle_heartbeat` stashed candidates in
        ``_pending_process_verify``. Live rows reattach as RUNNING,
        confirmed-gone rows become EXITED, unverifiable rows stay
        RUNNING (fail-open). The stash is always drained, even on
        failure, so one bad workspace cannot wedge later heartbeats.
        Returns IDs of changed processes.
        """
        pending = self._pending_process_verify
        self._pending_process_verify = {}
        changed: list[uuid.UUID] = []
        for ws_id_str, rows in pending.items():
            try:
                workspace_uuid = uuid.UUID(str(ws_id_str))
            except (ValueError, TypeError, AttributeError):
                continue
            workspace = await sync_to_async(self.workspaces.get_by_id)(
                workspace_uuid
            )
            if workspace is None:
                continue
            # Only verify while the workspace is still running and the
            # runner still owns it; anything else leaves rows untouched
            # (workspace lifecycle handlers own those transitions).
            if workspace.status != WorkspaceStatus.RUNNING:
                continue
            try:
                owner_id = await sync_to_async(self.workspaces.get_runner_id)(
                    workspace_uuid
                )
            except Exception:
                continue
            if owner_id is None or owner_id != runner.id:
                continue
            try:
                changed.extend(
                    await self.reverify_vanished_processes(
                        workspace_uuid, rows
                    )
                )
            except Exception:
                logger.exception(
                    "Failed verifying vanished processes for workspace %s",
                    ws_id_str,
                )
        return changed

    def handle_unknown_workspace_cleanup_result(
        self,
        runner: "Runner",
        workspace_id: str,
        *,
        cleaned: bool,
        error: str | None = None,
    ) -> None:
        """Handle result events for unknown-workspace cleanup requests."""
        cleanup_key = (str(runner.id), workspace_id)
        self._pending_unknown_workspace_cleanup.discard(cleanup_key)

        if error:
            logger.error(
                "Unknown workspace cleanup failed on runner %s (workspace=%s): %s",
                runner.id,
                workspace_id,
                error,
            )
            return

        logger.info(
            "Unknown workspace cleanup completed on runner %s (workspace=%s, cleaned=%s)",
            runner.id,
            workspace_id,
            cleaned,
        )

    def _request_workspace_cleanup(
        self,
        runner: "Runner",
        *,
        workspace_id: str,
        reason: str,
    ) -> None:
        """Request runner cleanup for a runtime workspace ID, deduplicated."""
        cleanup_key = (str(runner.id), workspace_id)
        if cleanup_key in self._pending_unknown_workspace_cleanup:
            return

        logger.warning(
            "Heartbeat: requesting cleanup for workspace %s on runner %s (%s)",
            workspace_id,
            runner.id,
            reason,
        )
        self._pending_unknown_workspace_cleanup.add(cleanup_key)
        async_to_sync(self._emit_to_runner)(
            runner,
            "task:cleanup_unknown_workspace",
            {"workspace_id": workspace_id},
        )

    @staticmethod
    def _map_instance_status(status: str) -> str | None:
        """Map a runtime instance status string to a WorkspaceStatus value.

        Supports both Docker container states and QEMU/libvirt domain states.
        """
        mapping = {
            # Docker states
            "running": WorkspaceStatus.RUNNING,
            "exited": WorkspaceStatus.STOPPED,
            "dead": WorkspaceStatus.FAILED,
            "removing": WorkspaceStatus.REMOVED,
            "created": WorkspaceStatus.CREATING,
            # QEMU/libvirt states
            "stopped": WorkspaceStatus.STOPPED,
            "failed": WorkspaceStatus.FAILED,
            "removed": WorkspaceStatus.REMOVED,
        }
        return mapping.get(status)
