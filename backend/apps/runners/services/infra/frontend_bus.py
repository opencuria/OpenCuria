"""Frontend event bus fan-out (Phase 2 infra mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces``. The
``sio_server.emit_to_frontend`` import stays lazy inside
``_forward_to_frontend`` (note the deeper relative level ``...`` from
``infra/``); this module has no top-level harness/credential imports.
Phase 4: only ``_forward_to_frontend`` honours an injected
``frontend_bus`` port. The further lazy ``emit_to_frontend`` call sites
in ``interactive_sessions`` (7x) and ``file_transfer`` (2x) are
intentionally untouched (documented Phase-5 remainder).
"""

from __future__ import annotations

import asyncio
import logging

from asgiref.sync import async_to_sync

from ...enums import WorkspaceStatus

logger = logging.getLogger(__name__)


class FrontendBusMixin:
    """Frontend event fan-out shared by RunnerService."""

    def _forward_to_frontend(
        self,
        event: str,
        data: dict,
        workspace_id: str,
    ) -> None:
        """
        Schedule a Socket.IO emit to subscribed frontend clients.

        Since service methods are called synchronously (via sync_to_async),
        this schedules the async emit on the running event loop.

        Phase 4: when the facade was constructed with ``frontend_bus=...``,
        the injected bus is used (async callable ``(event, data,
        workspace_id)`` or object with an ``emit`` method, sync or async).
        Otherwise the previous lazy ``emit_to_frontend`` path is kept
        unchanged (including monkeypatch compatibility).
        """
        injected = getattr(self, "_frontend_bus", None)
        if injected is not None:
            try:
                self._emit_via_injected_frontend_bus(
                    injected, event, data, workspace_id
                )
            except Exception:
                logger.exception(
                    "Failed forwarding event to frontend",
                    extra={
                        "event": event,
                        "workspace_id": workspace_id,
                    },
                )
            return
        from ...sio_server import emit_to_frontend

        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                async_to_sync(emit_to_frontend)(event, data, workspace_id)
            else:
                loop.create_task(emit_to_frontend(event, data, workspace_id))
        except Exception:
            logger.exception(
                "Failed forwarding event to frontend",
                extra={
                    "event": event,
                    "workspace_id": workspace_id,
                },
            )

    def _emit_via_injected_frontend_bus(
        self,
        bus,
        event: str,
        data: dict,
        workspace_id: str,
    ) -> None:
        """Deliver a frontend event through an injected Phase-4 bus.

        Accepts an async callable ``(event, data, workspace_id)`` or an
        object exposing ``emit`` (sync or async, same signature). Async
        targets are scheduled on the running loop (or driven via
        ``async_to_sync`` outside a loop); sync targets run inline. Never
        swallows: failures propagate to the caller for logging.
        """
        target = getattr(bus, "emit", None)
        if target is None:
            target = bus
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if asyncio.iscoroutinefunction(target):
            if loop is not None:
                loop.create_task(target(event, data, workspace_id))
            else:
                async_to_sync(target)(event, data, workspace_id)
            return
        result = target(event, data, workspace_id)
        if asyncio.iscoroutine(result):
            if loop is not None:
                loop.create_task(result)
            else:
                async_to_sync(lambda: result)()

    def _forward_runner_status_to_frontend(
        self,
        runner: "Runner",
        status: str,
    ) -> None:
        """Emit runner status change events for all workspaces of this runner.

        Sends a ``runner:offline`` or ``runner:online`` event for every
        workspace managed by the runner so subscribed frontend clients can
        update their display without a full page refresh.
        """
        workspaces = list(
            self.workspaces.list_by_runner(runner.id).exclude(
                status__in=[WorkspaceStatus.REMOVED, WorkspaceStatus.FAILED]
            )
        )
        event = "runner:offline" if status == "offline" else "runner:online"
        for ws in workspaces:
            ws_id = str(ws.id)
            self._forward_to_frontend(
                event,
                {"workspace_id": ws_id, "runner_id": str(runner.id)},
                ws_id,
            )

    def _forward_workspace_operation(
        self,
        workspace_id: str,
        active_operation: str | None,
    ) -> None:
        """Forward workspace operation changes to subscribed frontend clients."""
        self._forward_to_frontend(
            "workspace:operation_changed",
            {
                "workspace_id": workspace_id,
                "active_operation": active_operation,
            },
            workspace_id,
        )

    def _forward_workspace_status(
        self,
        workspace: "Workspace",
        *,
        task_id: str | None = None,
        status: str | None = None,
    ) -> None:
        """Forward workspace status and credential presence to the frontend."""
        payload = {
            "workspace_id": str(workspace.id),
            "status": status or workspace.status,
            "credentials_present": bool(workspace.credentials_present),
        }
        if task_id:
            payload["task_id"] = task_id
        self._forward_to_frontend(
            "workspace:status_changed",
            payload,
            str(workspace.id),
        )

    def _push_process_status(
        self,
        *,
        workspace_id: str,
        process_id: str,
        status: str,
        exit_code: int | None = None,
        pid: int | None = None,
        log_path: str | None = None,
        run_count: int | None = None,
    ) -> None:
        """Forward a background-process status change to the frontend."""
        payload: dict = {
            "workspace_id": workspace_id,
            "process_id": process_id,
            "status": status,
            "exit_code": exit_code,
            "pid": pid,
        }
        if log_path is not None:
            payload["log_path"] = log_path
        if run_count is not None:
            payload["run_count"] = run_count
        self._forward_to_frontend(
            "process:status_changed",
            payload,
            workspace_id,
        )

    def _push_process_removed(
        self,
        *,
        workspace_id: str,
        process_id: str,
    ) -> None:
        """Forward a background-process removal to the frontend."""
        self._forward_to_frontend(
            "process:removed",
            {
                "workspace_id": workspace_id,
                "process_id": process_id,
            },
            workspace_id,
        )
