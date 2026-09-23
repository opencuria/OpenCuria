"""Interactive sessions: terminal PTY + desktop (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes. The backing dicts
``_active_terminals`` / ``_terminal_workspace_runner`` /
``_active_desktops`` / ``_desktop_workspace_runner`` intentionally remain
class attributes on the ``RunnerService`` facade (tests read and write them
directly); this mixin only accesses them via ``self``. The mixin further
relies on the facade providing ``self.tasks`` / ``self.workspaces``,
``_emit_to_runner`` / ``_call_runner`` (``RunnerTransportMixin``),
``_forward_*`` (``FrontendBusMixin``), ``_desktop_event_owned_by_runner``
(``OwnershipMixin``) and ``_record_active_desktop`` et al.
(``SessionStoreMixin``). Lazy ``from ...sio_server`` / ``common.utils``
imports keep their depth/semantics.
"""

from __future__ import annotations

import logging
import uuid

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ...enums import TaskType, WorkspaceStatus
from ...exceptions import (
    RunnerOfflineError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)

logger = logging.getLogger(__name__)


class InteractiveSessionsMixin:
    """Terminal and desktop session flows shared by RunnerService."""

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> "Task":
        """Dispatch a start_terminal task to the runner.

        Returns the Task record.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a terminal"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_TERMINAL,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:start_terminal",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "cols": cols,
                "rows": rows,
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_terminal to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def handle_terminal_started(
        self,
        task_id: str,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:started event from a runner."""
        from ...sio_server import emit_to_frontend

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        self._active_terminals[workspace_id] = terminal_id
        # Cache runner ownership so terminal:output validation is O(1)
        if runner_id:
            self._terminal_workspace_runner[workspace_id] = runner_id
        logger.info(
            "Terminal started: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )

        await emit_to_frontend(
            "terminal:started",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "task_id": task_id,
            },
            workspace_id,
        )

    async def handle_terminal_output(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:output from runner — forward to frontend (no DB).

        Runner ownership is validated against the in-memory cache populated
        when the terminal session was started, so no DB lookup is required
        on the hot path.
        """
        if runner_id:
            cached = self._terminal_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "terminal:output rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        from ...sio_server import emit_to_frontend

        await emit_to_frontend(
            "terminal:output",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
            workspace_id,
        )

    async def handle_terminal_closed(
        self,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle terminal:closed from runner."""
        if runner_id:
            cached = self._terminal_workspace_runner.get(workspace_id)
            if cached is not None and cached != runner_id:
                logger.warning(
                    "terminal:closed rejected: workspace %s is owned by "
                    "runner %s, not %s",
                    workspace_id,
                    cached,
                    runner_id,
                )
                return

        from ...sio_server import emit_to_frontend

        self._active_terminals.pop(workspace_id, None)
        self._terminal_workspace_runner.pop(workspace_id, None)
        logger.info(
            "Terminal closed: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )
        await emit_to_frontend(
            "terminal:closed",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
            workspace_id,
        )

    async def forward_terminal_input(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
    ) -> None:
        """Forward terminal input from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:input",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def forward_terminal_resize(
        self,
        workspace_id: str,
        terminal_id: str,
        cols: int,
        rows: int,
    ) -> None:
        """Forward terminal resize from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:resize",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "cols": cols,
                "rows": rows,
            },
        )

    async def forward_terminal_close(
        self,
        workspace_id: str,
        terminal_id: str,
    ) -> None:
        """Forward terminal close request from frontend to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return

        runner = workspace.runner
        if not runner.is_online:
            return

        await self._emit_to_runner(
            runner,
            "terminal:close",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
        )

    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> "Task":
        """Dispatch a start_desktop task to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a desktop"
            )

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_DESKTOP,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:start_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                "desktop_width": workspace.desktop_width,
                "desktop_height": workspace.desktop_height,
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_desktop to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def stop_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> "Task":
        """Dispatch a stop_desktop task to the runner."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.STOP_DESKTOP,
            workspace=workspace,
        )

        await self._emit_to_runner(
            runner,
            "task:stop_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
            },
        )

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched stop_desktop to runner %s (workspace=%s, task=%s)",
            runner.id,
            workspace_id,
            task_id,
        )
        return task

    async def write_desktop_clipboard(
        self,
        workspace_id: uuid.UUID,
        text: str,
    ) -> None:
        """Write plain text into a running desktop session clipboard."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        if not self.is_desktop_active(str(workspace_id)):
            raise ConflictError(
                "Desktop session is not active. Start the desktop first."
            )

        response = await self._call_runner(
            runner,
            "desktop:clipboard_write",
            {
                "workspace_id": str(workspace_id),
                "text": text,
            },
            timeout=120,
        )
        if isinstance(response, dict) and response.get("ok") is False:
            raise RuntimeError(str(response.get("error") or "Clipboard write failed"))
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def read_desktop_clipboard(
        self,
        workspace_id: uuid.UUID,
    ) -> str:
        """Read plain text from a running desktop session clipboard."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        if not self.is_desktop_active(str(workspace_id)):
            raise ConflictError(
                "Desktop session is not active. Start the desktop first."
            )

        response = await self._call_runner(
            runner,
            "desktop:clipboard_read",
            {
                "workspace_id": str(workspace_id),
            },
            timeout=120,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        if isinstance(response, dict) and response.get("ok") is False:
            raise RuntimeError(str(response.get("error") or "Clipboard read failed"))
        if not isinstance(response, dict):
            return ""
        value = response.get("text", "")
        return value if isinstance(value, str) else str(value)

    async def handle_desktop_started(
        self,
        task_id: str | None,
        workspace_id: str,
        port: int,
        container_ip: str,
        network_name: str,
        runner_id: str | None = None,
        *,
        viewer: bool = True,
        computer_use: bool = False,
    ) -> None:
        """Handle desktop:started after a viewer lease is acquired."""
        from ...sio_server import emit_to_frontend

        task = None
        if task_id:
            task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
            if task and not self._validate_task_runner(task, runner_id):
                return

        self._record_active_desktop(
            workspace_id,
            {
                "port": port,
                "container_ip": container_ip,
                "network_name": network_name,
                "viewer": viewer,
                "computer_use": computer_use,
            },
            runner_id=runner_id,
        )

        if task:
            await sync_to_async(self.tasks.complete)(task)

        logger.info(
            "Desktop started: workspace=%s, port=%s, ip=%s",
            workspace_id,
            port,
            container_ip,
        )

        if not task_id:
            return

        await emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
                "computer_use_active": computer_use,
            },
            workspace_id,
        )

    async def handle_desktop_process(
        self,
        workspace_id: str,
        port: int,
        container_ip: str,
        network_name: str,
        runner_id: str | None = None,
        *,
        viewer: bool = False,
        computer_use: bool = False,
    ) -> None:
        """Record live desktop process routing without a viewer acquire.

        Harness-induced hold/ensure announcements reuse the same cache path
        as reconnect announcements, but must also refresh the frontend proxy
        state immediately: without this, the viewer keeps a stale lease view
        until the next heartbeat. Ownership is validated against the real
        workspace owner (not just the in-memory cache) so a stray runner
        cannot hijack another runner's desktop even with an empty cache.
        """
        from ...sio_server import emit_to_frontend

        if not await self._desktop_event_owned_by_runner(workspace_id, runner_id):
            return

        self._record_active_desktop(
            workspace_id,
            {
                "port": port,
                "container_ip": container_ip,
                "network_name": network_name,
                "viewer": viewer,
                "computer_use": computer_use,
            },
            runner_id=runner_id,
        )
        logger.info(
            "Desktop process announced: workspace=%s, viewer=%s, computer_use=%s",
            workspace_id,
            viewer,
            computer_use,
        )
        await emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
                "computer_use_active": computer_use,
            },
            workspace_id,
        )

    async def handle_desktop_viewer_released(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        *,
        computer_use_active: bool = False,
    ) -> None:
        """Handle viewer lease release while the desktop process stays up.

        Ownership is validated against the real workspace owner (not just
        the in-memory cache) so a foreign runner can never clear another
        runner's desktop state, even with an empty cache.
        """
        from ...sio_server import emit_to_frontend

        if not await self._desktop_event_owned_by_runner(workspace_id, runner_id):
            return

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        current = self._active_desktops.get(workspace_id)
        if current is not None:
            updated = dict(current)
            updated["viewer"] = False
            updated["computer_use"] = computer_use_active
            self._record_active_desktop(
                workspace_id,
                updated,
                runner_id=runner_id,
            )

        logger.info(
            "Desktop viewer released: workspace=%s, computer_use=%s",
            workspace_id,
            computer_use_active,
        )
        await emit_to_frontend(
            "desktop:viewer_released",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "computer_use_active": computer_use_active,
            },
            workspace_id,
        )

    async def handle_desktop_stopped(
        self,
        task_id: str | None,
        workspace_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Handle desktop:stopped event from a runner.

        ``task_id`` is optional: harness-induced releases announce the stop
        without a viewer task, while manual stops still complete theirs.
        Ownership is validated against the real workspace owner (not just
        the in-memory cache) so a foreign runner can never clear another
        runner's desktop state, even with an empty cache.
        """
        from ...sio_server import emit_to_frontend

        if not await self._desktop_event_owned_by_runner(workspace_id, runner_id):
            return

        task = None
        if task_id:
            try:
                task_uuid = uuid.UUID(str(task_id))
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "desktop:stopped rejected: invalid task_id %s",
                    task_id,
                )
                return
            task = await sync_to_async(self.tasks.get_by_id)(task_uuid)
            if task:
                if not self._validate_task_runner(task, runner_id):
                    return
                await sync_to_async(self.tasks.complete)(task)

        desktop_info = self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)

        logger.info("Desktop stopped: workspace=%s", workspace_id)

        await emit_to_frontend(
            "desktop:stopped",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
            },
            workspace_id,
        )
