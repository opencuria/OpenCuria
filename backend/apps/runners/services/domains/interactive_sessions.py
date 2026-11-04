"""Coordinate terminal sessions and authorized desktop interactions."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from common import realtime
from common.exceptions import ConflictError

from ...enums import TaskType, WorkspaceStatus
from ...exceptions import (
    HarnessActionError,
    RunnerOfflineError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)
from ..session_state import TerminalRoute, TerminalStart

if TYPE_CHECKING:
    from apps.accounts.models import User

    from ...models import Runner, Task, Workspace

if TYPE_CHECKING:
    from ...repositories import TaskRepository, WorkspaceRepository
    from ..domains.workspace_access import WorkspaceAccessService
    from ..infra.ownership import OwnershipService
    from ..infra.session_store import SessionStoreService
    from ..ports import HarnessGateway, RunnerTransport
    from ..session_state import InteractiveSessionState

logger = logging.getLogger(__name__)

#: Requests whose runner never confirmed a PTY are forgotten after this long.
TERMINAL_START_RETENTION_SECONDS = 600


class InteractiveSessionsService:
    """Terminal and desktop session flows with explicitly supplied collaborators."""

    def __init__(
        self,
        *,
        tasks: type[TaskRepository],
        workspaces: type[WorkspaceRepository],
        state: InteractiveSessionState,
        desktop_action: Callable[[str, str, dict], Awaitable[dict]],
        harness: Callable[[], HarnessGateway],
        ownership: OwnershipService,
        runner_transport: RunnerTransport,
        session_store: SessionStoreService,
        workspace_access: WorkspaceAccessService,
    ) -> None:
        """Compose this service without inheriting sibling behavior or state."""
        self.tasks = tasks
        self.workspaces = workspaces
        self.state = state
        self.desktop_action = desktop_action
        self.harness = harness
        self.ownership = ownership
        self.runner_transport = runner_transport
        self.session_store = session_store
        self.workspace_access = workspace_access

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
        *,
        user_id: uuid.UUID | str | None = None,
        client_id: str | None = None,
    ) -> Task:
        """Dispatch a start_terminal task to the runner.

        ``client_id`` is the requesting frontend connection; the terminal is
        bound to it once the runner confirms the PTY (see
        :meth:`handle_terminal_started`). Returns the Task record.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self.ownership.ensure_workspace_available(workspace)

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

        self._remember_terminal_start(
            str(task_id),
            TerminalStart(
                workspace_id=str(workspace_id),
                user_id=str(user_id) if user_id is not None else None,
                client_id=client_id or None,
                requested_at=time.monotonic(),
            ),
        )
        await self.runner_transport.emit_to_runner(
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

    def _remember_terminal_start(self, task_id: str, start: TerminalStart) -> None:
        """Track a requester until the runner confirms; forget stale requests."""
        starts = self.state.terminal_starts
        cutoff = start.requested_at - TERMINAL_START_RETENTION_SECONDS
        stale = [key for key, item in starts.items() if item.requested_at < cutoff]
        for key in stale:
            starts.pop(key, None)
        starts[task_id] = start

    @staticmethod
    def _terminal_owners(start: TerminalStart | None, workspace_id: str) -> set[str]:
        """Resolve the connected frontend clients that requested a terminal.

        A named requesting connection owns it while it is connected and
        belongs to the requester; without one, the requester's clients
        watching the workspace do. Unknown requesters own nothing, so nobody
        can keep such a terminal alive.
        """
        if start is None or start.user_id is None:
            return set()
        if start.client_id:
            if realtime.frontend_user(start.client_id) == start.user_id:
                return {start.client_id}
            return set()
        return realtime.workspace_subscribers(workspace_id, start.user_id)

    async def handle_terminal_started(
        self,
        task_id: str,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
    ) -> None:
        """Bind a confirmed PTY to its requester, or close it when none is left."""
        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self.ownership.validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        start = self.state.terminal_starts.pop(task_id, None)
        owners = self._terminal_owners(start, workspace_id)
        if not owners:
            logger.info(
                "Closing unowned terminal: workspace=%s, terminal=%s",
                workspace_id,
                terminal_id,
            )
            await self._close_runner_terminal(workspace_id, terminal_id)
            await realtime.emit_to_frontend(
                "workspace:error",
                {
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "error": "Terminal client disconnected before the shell started",
                },
                workspace_id,
            )
            return

        self.state.terminals[terminal_id] = TerminalRoute(
            workspace_id=workspace_id, runner_id=runner_id, owners=owners
        )
        logger.info(
            "Terminal started: workspace=%s, terminal=%s",
            workspace_id,
            terminal_id,
        )

        await realtime.emit_to_frontend(
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
        if not self._route_accepts(terminal_id, workspace_id, runner_id):
            logger.warning(
                "terminal:output rejected for terminal %s (workspace=%s, runner=%s)",
                terminal_id,
                workspace_id,
                runner_id,
            )
            return

        await realtime.emit_to_frontend(
            "terminal:output",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "data": data,
            },
            workspace_id,
        )

    def _route_accepts(
        self, terminal_id: str, workspace_id: str, runner_id: str | None
    ) -> bool:
        """Reject runner events that contradict a known terminal route."""
        route = self.state.terminals.get(terminal_id)
        if route is None:
            return True
        if route.workspace_id != workspace_id:
            return False
        return not (runner_id and route.runner_id and route.runner_id != runner_id)

    async def handle_terminal_closed(
        self,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Drop the route of a PTY the runner ended and tell its clients why."""
        if not self._route_accepts(terminal_id, workspace_id, runner_id):
            logger.warning(
                "terminal:closed rejected for terminal %s (workspace=%s, runner=%s)",
                terminal_id,
                workspace_id,
                runner_id,
            )
            return

        self.state.terminals.pop(terminal_id, None)
        logger.info(
            "Terminal closed: workspace=%s, terminal=%s, reason=%s",
            workspace_id,
            terminal_id,
            reason,
        )
        payload = {"workspace_id": workspace_id, "terminal_id": terminal_id}
        if reason:
            payload["reason"] = reason
        await realtime.emit_to_frontend("terminal:closed", payload, workspace_id)

    async def release_frontend_terminals(
        self, sid: str, workspace_id: str | None = None
    ) -> None:
        """Detach a frontend client; close terminals it was the last owner of.

        Called when the client disconnects (all workspaces) or unsubscribes
        from one workspace, so terminals never outlive the client using them.
        """
        orphaned: list[tuple[str, str]] = []
        for terminal_id, route in list(self.state.terminals.items()):
            if sid not in route.owners:
                continue
            if workspace_id is not None and route.workspace_id != workspace_id:
                continue
            route.owners.discard(sid)
            if not route.owners:
                self.state.terminals.pop(terminal_id, None)
                orphaned.append((route.workspace_id, terminal_id))
        for orphan_workspace_id, terminal_id in orphaned:
            logger.info(
                "Closing detached terminal: workspace=%s, terminal=%s",
                orphan_workspace_id,
                terminal_id,
            )
            try:
                await self._close_runner_terminal(orphan_workspace_id, terminal_id)
            except Exception:
                # The runner closes it on its own once lease renewals stop.
                logger.warning(
                    "Could not close detached terminal %s", terminal_id, exc_info=True
                )

    async def renew_terminal_leases(self, runner: Runner) -> None:
        """Renew the runner-side leases of terminals that still have owners.

        Terminals missing from renewals (no owner, backend restart, lost
        routes) are closed by the runner after its lease timeout.
        """
        runner_id = str(runner.id)
        leases = [
            {"terminal_id": terminal_id, "workspace_id": route.workspace_id}
            for terminal_id, route in self.state.terminals.items()
            if route.runner_id == runner_id and route.owners
        ]
        if not leases:
            return
        await self.runner_transport.emit_to_runner(
            runner, "terminal:renew", {"terminals": leases}
        )

    async def _close_runner_terminal(self, workspace_id: str, terminal_id: str) -> None:
        """Ask the owning runner to close a PTY without client-facing checks."""
        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None or not workspace.runner.is_online:
            return
        await self.runner_transport.emit_to_runner(
            workspace.runner,
            "terminal:close",
            {"workspace_id": workspace_id, "terminal_id": terminal_id},
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

        self.ownership.ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            return

        await self.runner_transport.emit_to_runner(
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

        self.ownership.ensure_workspace_available(workspace)
        runner = workspace.runner
        if not runner.is_online:
            return

        await self.runner_transport.emit_to_runner(
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

        self.ownership.ensure_workspace_available(workspace)
        route = self.state.terminals.get(terminal_id)
        if route is not None and route.workspace_id == workspace_id:
            # Stop renewing at once; the runner still confirms the close.
            self.state.terminals.pop(terminal_id, None)
        runner = workspace.runner
        if not runner.is_online:
            return

        await self.runner_transport.emit_to_runner(
            runner,
            "terminal:close",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
            },
        )

    @staticmethod
    def viewer_identity(
        workspace_id: uuid.UUID, user_id: uuid.UUID, viewer_client_id: uuid.UUID
    ) -> dict:
        """Derive a viewer identity; caller-provided owner IDs are never accepted."""
        client = uuid.UUID(str(viewer_client_id))
        return {
            "lease_id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"opencuria:viewer:{workspace_id}:{user_id}:{client}",
                )
            ),
            "owner_id": str(user_id),
            "kind": "viewer",
        }

    async def _viewer_workspace(
        self, workspace_id: uuid.UUID, *, user, organization_id: uuid.UUID
    ) -> Workspace:
        """Apply shared REST and proxy workspace authorization."""
        workspace = await sync_to_async(
            self.workspace_access.get_workspace_for_desktop_user
        )(workspace_id, user=user, organization_id=organization_id)
        self.ownership.ensure_workspace_available(workspace)
        if not workspace.runner.is_online:
            raise RunnerOfflineError(str(workspace.runner.id))
        return workspace

    async def _viewer_rpc(
        self, workspace_id: uuid.UUID, action: str, args: dict
    ) -> dict:
        """Use the existing correlated desktop_action transport."""
        try:
            return await self.desktop_action(str(workspace_id), action, args)
        except (HarnessActionError, TimeoutError) as exc:
            raise ConflictError(
                "Viewer desktop operation could not be confirmed"
            ) from exc

    async def take_desktop_control(
        self,
        workspace_id: uuid.UUID,
        *,
        user: User,
        organization_id: uuid.UUID,
    ) -> list[uuid.UUID]:
        """Abort busy computer-use sessions so a viewer can drive the desktop.

        Returns the ids of the aborted agent sessions.
        """
        workspace = await sync_to_async(
            self.workspace_access.get_workspace_for_desktop_user
        )(workspace_id, user=user, organization_id=organization_id)
        await sync_to_async(self.ownership.ensure_workspace_available)(workspace)
        return await self.harness().abort_busy_computeruse(workspace.id)

    async def viewer_desktop_status(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID | None = None,
    ) -> dict:
        """Return global summaries and only the authenticated client's intent."""
        await self._viewer_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        info = self.session_store.get_desktop_info(str(workspace_id)) or {}
        result = {
            "active": bool(info),
            "proxy_url": f"/ws/desktop/{workspace_id}/" if info else None,
            "viewer_held": bool(info.get("viewer")),
            "computer_use_active": bool(info.get("computer_use")),
            "mcp_active": bool(info.get("mcp")),
            "holder_count": int(info.get("holder_count", 0)),
            "viewer_lease_state": "unknown",
            "revision": None,
            "epoch": None,
        }
        if viewer_client_id is not None:
            identity = self.viewer_identity(workspace_id, user.id, viewer_client_id)
            own = await self._viewer_rpc(
                workspace_id, "lease_status", {"lease_id": identity["lease_id"]}
            )
            result.update(
                viewer_lease_state=own.get("lease_state", "unknown"),
                revision=own.get("revision"),
                epoch=own.get("epoch"),
            )
        return result

    def get_desktop_proxy_target(self, workspace_id: str) -> dict | None:
        """Return the connected runner serving the workspace's active desktop."""
        desktop_info = self.session_store.get_desktop_info(workspace_id)
        if desktop_info is None:
            return None
        workspace = self.workspaces.get_by_id(uuid.UUID(workspace_id))
        if workspace is None or not workspace.runner.sid:
            return None
        return {
            "runner_id": str(workspace.runner_id),
            "runner_sid": workspace.runner.sid,
            "desktop_info": desktop_info,
        }

    async def renew_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID,
        intent_revision: int,
    ) -> dict:
        """Extend an existing intent, never reserve or restart a desktop."""
        await self._viewer_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        identity = self.viewer_identity(workspace_id, user.id, viewer_client_id)
        own = await self._viewer_rpc(
            workspace_id, "lease_status", {"lease_id": identity["lease_id"]}
        )
        if own.get("lease_state") != "held" or own.get("revision") != intent_revision:
            raise ConflictError("Viewer lease is not held at this revision")
        response = await self._viewer_rpc(
            workspace_id,
            "renew",
            {
                "lease_id": identity["lease_id"],
                "epoch": own["epoch"],
                "revision": intent_revision,
            },
        )
        return {
            "viewer_lease_state": response.get("lease_state", "unknown"),
            "revision": response.get("revision", intent_revision),
            "epoch": response.get("epoch"),
        }

    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID,
        intent_revision: int,
    ) -> Task:
        """Dispatch a start_desktop task to the runner."""
        workspace = await self._viewer_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        runner = workspace.runner
        if intent_revision < 1:
            raise ValueError("intent_revision must be positive")
        identity = self.viewer_identity(workspace_id, user.id, viewer_client_id)
        binding = await self._viewer_rpc(workspace_id, "binding", {})
        ownership = {**identity, "revision": intent_revision, "epoch": binding["epoch"]}

        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError("Workspace must be running")

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.START_DESKTOP,
            workspace=workspace,
        )

        await self.runner_transport.emit_to_runner(
            runner,
            "task:start_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                **ownership,
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
        *,
        user,
        organization_id: uuid.UUID,
        viewer_client_id: uuid.UUID,
        intent_revision: int,
    ) -> Task:
        """Dispatch a stop_desktop task to the runner."""
        workspace = await self._viewer_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        runner = workspace.runner
        if intent_revision < 1:
            raise ValueError("intent_revision must be positive")
        identity = self.viewer_identity(workspace_id, user.id, viewer_client_id)
        binding = await self._viewer_rpc(workspace_id, "binding", {})
        ownership = {**identity, "revision": intent_revision, "epoch": binding["epoch"]}

        from common.utils import generate_uuid

        task_id = generate_uuid()
        task = await sync_to_async(self.tasks.create)(
            task_id=task_id,
            runner=runner,
            task_type=TaskType.STOP_DESKTOP,
            workspace=workspace,
        )

        await self.runner_transport.emit_to_runner(
            runner,
            "task:stop_desktop",
            {
                "task_id": str(task_id),
                "workspace_id": str(workspace_id),
                **ownership,
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
        *,
        user: User,
        organization_id: uuid.UUID,
    ) -> None:
        """Write plain text into a running desktop session clipboard."""
        workspace = await self._viewer_workspace(
            workspace_id, user=user, organization_id=organization_id
        )
        runner = workspace.runner

        if not self.session_store.is_desktop_active(str(workspace_id)):
            raise ConflictError(
                "Desktop session is not active. Start the desktop first."
            )

        response = await self.runner_transport.call_runner(
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
        *,
        user: User,
        organization_id: uuid.UUID,
        copy_selection: bool = False,
        shift: bool = False,
    ) -> str:
        """Read clipboard text, optionally copying the current desktop selection."""
        if type(copy_selection) is not bool:
            raise ValueError("copy_selection must be a boolean")
        if type(shift) is not bool:
            raise ValueError("shift must be a boolean")
        if shift and not copy_selection:
            raise ValueError("shift requires copy_selection")

        async def _read() -> str:
            workspace = await self._viewer_workspace(
                workspace_id, user=user, organization_id=organization_id
            )
            runner = workspace.runner

            if not self.session_store.is_desktop_active(str(workspace_id)):
                raise ConflictError(
                    "Desktop session is not active. Start the desktop first."
                )

            if copy_selection:
                info = self.session_store.get_desktop_info(str(workspace_id)) or {}
                if info.get("computer_use"):
                    raise ConflictError(
                        "Unable to copy the desktop selection. Try again."
                    )
                busy = await sync_to_async(self.harness().has_busy_computeruse_session)(
                    workspace_id
                )
                if busy:
                    raise ConflictError(
                        "Unable to copy the desktop selection. Try again."
                    )

            args = {"workspace_id": str(workspace_id)}
            if copy_selection:
                args["copy_selection"] = True
                if shift:
                    args["shift"] = True
            response = await self.runner_transport.call_runner(
                runner,
                "desktop:clipboard_read",
                args,
                timeout=3.5 if copy_selection else 120,
            )
            await sync_to_async(self.workspaces.touch_activity)(workspace)
            if copy_selection:
                if (
                    not isinstance(response, dict)
                    or response.get("ok") is not True
                    or not isinstance(response.get("text"), str)
                ):
                    raise RuntimeError("Unable to copy the desktop selection")
                return response["text"]

            if isinstance(response, dict) and response.get("ok") is False:
                raise RuntimeError(
                    str(response.get("error") or "Clipboard read failed")
                )
            if not isinstance(response, dict):
                return ""
            value = response.get("text", "")
            return value if isinstance(value, str) else str(value)

        if not copy_selection:
            return await _read()

        try:
            async with asyncio.timeout(4):
                return await _read()
        except (RuntimeError, TimeoutError, SocketIOTimeoutError):
            raise ConflictError(
                "Unable to copy the desktop selection. Try again."
            ) from None

    async def handle_desktop_started(
        self,
        task_id: str | None,
        workspace_id: str,
        port: int,
        guest_ip: str,
        runner_id: str,
        *,
        viewer: bool,
        computer_use: bool,
        mcp: bool,
        holder_count: int,
        generation: int,
        epoch: str,
    ) -> None:
        """Handle desktop:started after a viewer lease is acquired."""
        if self.session_store.desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        task = None
        if task_id:
            task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
            if task and not self.ownership.validate_task_runner(task, runner_id):
                return

        recorded = self.session_store.record_active_desktop(
            workspace_id,
            {
                "mcp": mcp,
                "holder_count": holder_count,
                "generation": generation,
                "epoch": epoch,
                "port": port,
                "guest_ip": guest_ip,
                "viewer": viewer,
                "computer_use": computer_use,
            },
            runner_id=runner_id,
        )
        if not recorded:
            return

        if task:
            await sync_to_async(self.tasks.complete)(task)

        logger.info(
            "Desktop started: workspace=%s, port=%s, ip=%s",
            workspace_id,
            port,
            guest_ip,
        )

        if not task_id:
            return

        await realtime.emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
                "computer_use_active": bool(
                    (self.session_store.get_desktop_info(workspace_id) or {}).get(
                        "computer_use"
                    )
                ),
                **self.session_store.desktop_public_summary(workspace_id),
            },
            workspace_id,
        )

    async def handle_desktop_process(
        self,
        workspace_id: str,
        port: int,
        guest_ip: str,
        runner_id: str,
        *,
        viewer: bool,
        computer_use: bool,
        mcp: bool,
        holder_count: int,
        generation: int,
        epoch: str,
    ) -> None:
        """Record live desktop process routing without a viewer acquire.

        Harness-induced hold/ensure announcements reuse the same cache path
        as reconnect announcements, but must also refresh the frontend proxy
        state immediately: without this, the viewer keeps a stale lease view
        until the next heartbeat. Ownership is validated against the real
        workspace owner (not just the in-memory cache) so a stray runner
        cannot hijack another runner's desktop even with an empty cache.
        """
        if not await self.ownership.desktop_event_owned_by_runner(
            workspace_id, runner_id
        ):
            return
        if self.session_store.desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        recorded = self.session_store.record_active_desktop(
            workspace_id,
            {
                "mcp": mcp,
                "holder_count": holder_count,
                "generation": generation,
                "epoch": epoch,
                "port": port,
                "guest_ip": guest_ip,
                "viewer": viewer,
                "computer_use": computer_use,
            },
            runner_id=runner_id,
        )
        if not recorded:
            return
        updated = self.session_store.get_desktop_info(workspace_id) or {}
        logger.info(
            "Desktop process announced: workspace=%s, viewer=%s, computer_use=%s",
            workspace_id,
            updated.get("viewer", False),
            updated.get("computer_use", False),
        )
        await realtime.emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
                "computer_use_active": bool(updated.get("computer_use")),
                **self.session_store.desktop_public_summary(workspace_id),
            },
            workspace_id,
        )

    async def handle_desktop_viewer_released(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str,
        *,
        computer_use_active: bool,
        viewer_held: bool,
        mcp: bool,
        holder_count: int,
        generation: int,
        epoch: str,
    ) -> None:
        """Handle viewer lease release while the desktop process stays up.

        Ownership is validated against the real workspace owner (not just
        the in-memory cache) so a foreign runner can never clear another
        runner's desktop state, even with an empty cache.
        """
        if not await self.ownership.desktop_event_owned_by_runner(
            workspace_id, runner_id
        ):
            return
        if self.session_store.desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self.ownership.validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        if self.session_store.desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        current = self.state.desktops.get(workspace_id)
        if current is not None:
            updated = dict(current)
            updated.update(
                viewer=viewer_held,
                computer_use=computer_use_active,
                mcp=mcp,
                holder_count=holder_count,
                generation=generation,
                epoch=epoch,
            )
            if not self.session_store.record_active_desktop(
                workspace_id,
                updated,
                runner_id=runner_id,
            ):
                return

        logger.info(
            "Desktop viewer released: workspace=%s, computer_use=%s",
            workspace_id,
            computer_use_active,
        )
        await realtime.emit_to_frontend(
            "desktop:viewer_released",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "computer_use_active": bool(
                    (self.session_store.get_desktop_info(workspace_id) or {}).get(
                        "computer_use", False
                    )
                ),
                **self.session_store.desktop_public_summary(workspace_id),
            },
            workspace_id,
        )

    async def handle_desktop_stopped(
        self,
        task_id: str | None,
        workspace_id: str,
        runner_id: str,
        *,
        generation: int,
        epoch: str,
    ) -> None:
        """Handle desktop:stopped event from a runner.

        ``task_id`` is optional: harness-induced releases announce the stop
        without a viewer task, while manual stops still complete theirs.
        Ownership is validated against the real workspace owner (not just
        the in-memory cache) so a foreign runner can never clear another
        runner's desktop state, even with an empty cache.
        """
        if not await self.ownership.desktop_event_owned_by_runner(
            workspace_id, runner_id
        ):
            return

        if self.session_store.desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
            active=False,
        ):
            logger.info(
                "Ignoring stale desktop stop: workspace=%s generation=%s epoch=%s",
                workspace_id,
                generation,
                epoch,
            )
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
                if not self.ownership.validate_task_runner(task, runner_id):
                    return
                await sync_to_async(self.tasks.complete)(task)

        # Task lookup/completion above can yield to newer process announcements.
        # Recheck immediately before cache removal so delayed stops cannot win.
        if self.session_store.desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
            active=False,
        ):
            return

        self.session_store.remember_desktop_state(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
            active=False,
        )
        self.state.desktops.pop(workspace_id, None)
        self.state.desktop_runners.pop(workspace_id, None)

        logger.info("Desktop stopped: workspace=%s", workspace_id)

        await realtime.emit_to_frontend(
            "desktop:stopped",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "generation": generation,
                "epoch": epoch,
            },
            workspace_id,
        )
