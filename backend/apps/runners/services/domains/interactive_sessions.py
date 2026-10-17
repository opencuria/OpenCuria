"""Interactive sessions: terminal PTY + desktop (Phase 3 domain mixin).

The terminal routing registry is durable in PostgreSQL; desktop state retains
its current process-local cache. The mixin further
relies on the facade providing ``self.tasks`` / ``self.workspaces``,
``_emit_to_runner`` / ``_call_runner`` (``RunnerTransportMixin``),
``_forward_*`` (``FrontendBusMixin``), ``_desktop_event_owned_by_runner``
(``OwnershipMixin``) and ``_record_active_desktop`` et al.
(``SessionStoreMixin``). Lazy ``from ...sio_server`` / ``common.utils``
imports keep their depth/semantics.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from asgiref.sync import sync_to_async
from socketio.exceptions import TimeoutError as SocketIOTimeoutError

from common.exceptions import ConflictError

from ...enums import TaskStatus, TaskType, WorkspaceStatus
from ...exceptions import (
    RunnerOfflineError,
    WorkspaceNotFoundError,
    WorkspaceStateError,
)

if TYPE_CHECKING:
    from ...models import Task

logger = logging.getLogger(__name__)


class InteractiveSessionsMixin:
    """Terminal and desktop session flows shared by RunnerService."""

    async def _active_terminal_for_workspace(
        self, workspace_id: str, terminal_id: str, runner_id: str
    ):
        """Resolve an active terminal route and verify its workspace runner."""
        from ...terminal_registry import TerminalSessionRepository

        try:
            session = await sync_to_async(TerminalSessionRepository.get_active)(
                uuid.UUID(workspace_id), terminal_id
            )
        except (ValueError, TypeError):
            return None
        if session is None or str(session.runner_id) != runner_id:
            return None
        return session

    async def validate_terminal_owner(
        self, workspace_id: str, terminal_id: str, runner_id: str
    ) -> bool:
        """Return whether an authenticated runner owns this active terminal."""
        return (
            await self._active_terminal_for_workspace(
                workspace_id, terminal_id, runner_id
            )
            is not None
        )

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
        *,
        terminal_id: uuid.UUID | str | None = None,
        after_seq: int = 0,
    ) -> Task | None:
        """Start a PTY only if absent, otherwise reattach to its stable ID.

        ``after_seq`` is the latest output sequence already rendered by a
        reconnecting client; the runner replays retained output after it.
        """
        from ...terminal_registry import TerminalSessionRepository

        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        self._ensure_workspace_available(workspace)
        if type(cols) is not int or not 2 <= cols <= 500:
            raise ValueError("cols must be between 2 and 500")
        if type(rows) is not int or not 1 <= rows <= 300:
            raise ValueError("rows must be between 1 and 300")
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("after_seq must be a non-negative integer")
        if workspace.status != WorkspaceStatus.RUNNING:
            raise WorkspaceStateError(
                f"Workspace '{workspace_id}' is '{workspace.status}', "
                f"must be '{WorkspaceStatus.RUNNING}' to start a terminal"
            )
        runner = workspace.runner
        if not runner.is_online:
            raise RunnerOfflineError(str(runner.id))

        if terminal_id is not None:
            try:
                terminal_id = uuid.UUID(str(terminal_id))
            except (ValueError, TypeError):
                raise ValueError("terminal_id must be a valid UUID") from None

        existing = await sync_to_async(TerminalSessionRepository.get_active)(
            workspace_id, terminal_id
        )
        if terminal_id is not None and existing is None:
            raise ConflictError("Terminal session is no longer available")
        if existing is not None:
            task = (
                await sync_to_async(self.tasks.get_by_id)(existing.task_id)
                if existing.task_id
                else None
            )
            if existing.status == "starting":
                if task is None:
                    raise ConflictError("Terminal start task is no longer available")
                # Same stable terminal UUID makes a repeated command idempotent
                # at runner level, including recovery from a lost ACK.
                await self._emit_to_runner(
                    runner,
                    "task:start_terminal",
                    {
                        "task_id": str(task.id),
                        "workspace_id": str(workspace_id),
                        "terminal_id": str(existing.terminal_id),
                        "cols": cols,
                        "rows": rows,
                    },
                )
                return task
            payload = {
                "workspace_id": str(workspace_id),
                "terminal_id": str(existing.terminal_id),
                "after_seq": max(0, after_seq),
            }
            await self._emit_to_runner(runner, "terminal:attach", payload)
            return task

        session, task, created = await sync_to_async(
            TerminalSessionRepository.claim
        )(workspace)
        if not created:
            payload = {
                "workspace_id": str(workspace_id),
                "terminal_id": str(session.terminal_id),
                "after_seq": max(0, after_seq),
            }
            await self._emit_to_runner(runner, "terminal:attach", payload)
            return task

        try:
            await self._emit_to_runner(
                runner,
                "task:start_terminal",
                {
                    "task_id": str(task.id),
                    "workspace_id": str(workspace_id),
                    "terminal_id": str(session.terminal_id),
                    "cols": cols,
                    "rows": rows,
                },
            )
        except Exception:
            await sync_to_async(task.delete)()
            await sync_to_async(session.delete)()
            raise

        await sync_to_async(self.tasks.mark_in_progress)(task)
        logger.info(
            "Dispatched start_terminal to runner %s (workspace=%s, terminal=%s)",
            runner.id,
            workspace_id,
            session.terminal_id,
        )
        await sync_to_async(self.workspaces.touch_activity)(workspace)
        return task

    async def handle_terminal_started(
        self,
        task_id: str | None,
        workspace_id: str,
        terminal_id: str,
        runner_id: str | None = None,
        *,
        replayed: bool = False,
        output_sequence: int | None = None,
        confirmed_input_ids: list[str] | None = None,
    ) -> None:
        """Handle a terminal start/reattach announcement from its runner."""
        from ...sio_server import emit_to_frontend
        from ...terminal_registry import TerminalSessionRepository

        try:
            workspace_uuid = uuid.UUID(workspace_id)
            terminal_uuid = uuid.UUID(terminal_id)
        except (ValueError, TypeError):
            return
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_uuid)
        if (
            workspace is None
            or runner_id is None
            or str(workspace.runner_id) != runner_id
            or not await sync_to_async(TerminalSessionRepository.mark_started)(
                workspace_id=workspace_id,
                terminal_id=terminal_uuid,
                runner_id=runner_id,
            )
        ):
            logger.warning(
                "terminal:started rejected for unknown or foreign terminal %s",
                terminal_id,
            )
            return

        if task_id and not replayed:
            try:
                task_uuid = uuid.UUID(task_id)
            except (ValueError, TypeError):
                logger.warning("Invalid task id on terminal start: %s", task_id)
                return
            task = await sync_to_async(self.tasks.get_by_id)(task_uuid)
            if task:
                if not self._validate_task_runner(task, runner_id):
                    return
                if task.status != TaskStatus.COMPLETED:
                    await sync_to_async(self.tasks.complete)(task)

        payload = {
            "workspace_id": workspace_id,
            "terminal_id": terminal_id,
            "task_id": task_id,
            "replayed": replayed,
            **({"recovery": True} if task_id and replayed else {}),
        }
        if output_sequence is not None:
            payload["output_sequence"] = output_sequence
        if confirmed_input_ids is not None:
            payload["confirmed_input_ids"] = confirmed_input_ids
        logger.info(
            "Terminal attached: workspace=%s, terminal=%s, replayed=%s",
            workspace_id,
            terminal_id,
            replayed,
        )
        await emit_to_frontend("terminal:started", payload, workspace_id)

    async def handle_terminal_attach_error(
        self,
        workspace_id: str,
        terminal_id: str,
        error: str,
        runner_id: str | None = None,
    ) -> None:
        """Forward an explicit replay failure without replacing a live PTY."""
        from ...sio_server import emit_to_frontend

        session = await self._active_terminal_for_workspace(
            workspace_id, terminal_id, runner_id or ""
        )
        if session is None:
            return
        await emit_to_frontend(
            "terminal:error",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "error": error,
                "code": "terminal_replay_unavailable",
            },
            workspace_id,
        )

    async def handle_terminal_output(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
        runner_id: str | None = None,
        *,
        sequence: int | None = None,
    ) -> None:
        """Validate, sequence and forward PTY output to workspace viewers."""
        from ...sio_server import emit_to_frontend
        from ...terminal_registry import TerminalSessionRepository

        if runner_id is None or sequence is None:
            logger.warning("Unsequenced terminal output rejected: %s", terminal_id)
            return
        accepted = await sync_to_async(TerminalSessionRepository.record_output)(
            workspace_id=workspace_id,
            terminal_id=terminal_id,
            runner_id=runner_id,
            sequence=sequence,
        )
        if accepted in {"gap", "unknown", "invalid"}:
            logger.warning(
                "Terminal output %s for session %s (sequence=%s)",
                accepted,
                terminal_id,
                sequence,
            )
            if accepted == "gap":
                await emit_to_frontend(
                    "terminal:error",
                    {
                        "workspace_id": workspace_id,
                        "terminal_id": terminal_id,
                        "error": "Terminal output sequence gap detected",
                        "code": "terminal_output_gap",
                    },
                    workspace_id,
                )
            return
        await emit_to_frontend(
            "terminal:output",
            {
                "workspace_id": workspace_id,
                "terminal_id": terminal_id,
                "sequence": sequence,
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
        """Handle terminal:closed from its owning runner."""
        from ...sio_server import emit_to_frontend
        from ...terminal_registry import TerminalSessionRepository

        if runner_id is None or not await sync_to_async(TerminalSessionRepository.close)(
            workspace_id=workspace_id,
            terminal_id=terminal_id,
            runner_id=runner_id,
        ):
            logger.warning("Terminal close rejected for unknown session: %s", terminal_id)
            return
        logger.info("Terminal closed: workspace=%s, terminal=%s", workspace_id, terminal_id)
        await emit_to_frontend(
            "terminal:closed",
            {"workspace_id": workspace_id, "terminal_id": terminal_id},
            workspace_id,
        )

    async def forward_terminal_input(
        self,
        workspace_id: str,
        terminal_id: str,
        data: str,
        *,
        input_id: str | None = None,
    ) -> None:
        """Forward terminal input from frontend to the runner."""
        from ...terminal_registry import TerminalSessionRepository

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return
        self._ensure_workspace_available(workspace)
        try:
            terminal_uuid = uuid.UUID(str(terminal_id))
        except (ValueError, TypeError):
            terminal_uuid = None
        active = (
            await sync_to_async(TerminalSessionRepository.get_active)(
                uuid.UUID(workspace_id), terminal_uuid
            )
            if terminal_uuid is not None
            else None
        )
        if active is None and terminal_uuid is not None:
            raise ConflictError("Terminal session is not active in this workspace")
        if active is not None and active.runner_id != workspace.runner_id:
            raise ConflictError("Terminal session is not active in this workspace")

        runner = workspace.runner
        if not runner.is_online:
            return

        payload = {
            "workspace_id": workspace_id,
            "terminal_id": terminal_id,
            "data": data,
            "input_id": input_id,
        }
        await self._emit_to_runner(runner, "terminal:input", payload)
        await sync_to_async(self.workspaces.touch_activity)(workspace)

    async def forward_terminal_resize(
        self,
        workspace_id: str,
        terminal_id: str,
        cols: int,
        rows: int,
    ) -> None:
        """Forward terminal resize from frontend to the runner."""
        from ...terminal_registry import TerminalSessionRepository

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return
        self._ensure_workspace_available(workspace)
        try:
            terminal_uuid = uuid.UUID(str(terminal_id))
        except (ValueError, TypeError):
            raise ConflictError("Terminal identity is invalid") from None
        active = await sync_to_async(TerminalSessionRepository.get_active)(
            uuid.UUID(workspace_id), terminal_uuid
        )
        if active is None or active.runner_id != workspace.runner_id:
            raise ConflictError("Terminal session is not active in this workspace")

        runner = workspace.runner
        if not runner.is_online:
            return

        payload = {
            "workspace_id": workspace_id,
            "terminal_id": terminal_id,
            "cols": cols,
            "rows": rows,
        }
        if getattr(getattr(self, "sio", None), "supervised_owner", False):
            await self.sio.emit("terminal:resize", payload, to=runner.sid)
        else:
            await self._emit_to_runner(runner, "terminal:resize", payload)

    async def forward_terminal_close(
        self,
        workspace_id: str,
        terminal_id: str,
    ) -> None:
        """Forward terminal close request from frontend to the runner."""
        from ...terminal_registry import TerminalSessionRepository

        workspace = await sync_to_async(self.workspaces.get_by_id)(
            uuid.UUID(workspace_id)
        )
        if workspace is None:
            return
        self._ensure_workspace_available(workspace)
        try:
            terminal_uuid = uuid.UUID(str(terminal_id))
        except (ValueError, TypeError):
            raise ConflictError("Terminal identity is invalid") from None
        active = await sync_to_async(TerminalSessionRepository.get_active)(
            uuid.UUID(workspace_id), terminal_uuid
        )
        if active is None or active.runner_id != workspace.runner_id:
            raise ConflictError("Terminal session is not active in this workspace")

        runner = workspace.runner
        if not runner.is_online:
            return

        payload = {"workspace_id": workspace_id, "terminal_id": terminal_id}
        if getattr(getattr(self, "sio", None), "supervised_owner", False):
            await self.sio.emit("terminal:close", payload, to=runner.sid)
        else:
            await self._emit_to_runner(runner, "terminal:close", payload)

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
    ):
        """Apply shared REST, MCP and proxy workspace authorization."""
        from apps.organizations.services import OrganizationService

        orgs = OrganizationService()
        await sync_to_async(orgs.require_membership)(user, organization_id)
        role = await sync_to_async(orgs.get_user_role)(user, organization_id)
        workspace = await sync_to_async(self.get_workspace)(workspace_id)
        if workspace.runner.organization_id != organization_id or (
            role != "admin" and workspace.created_by_id != user.id
        ):
            raise WorkspaceNotFoundError(str(workspace_id))
        self._ensure_workspace_available(workspace)
        if not workspace.runner.is_online:
            raise RunnerOfflineError(str(workspace.runner.id))
        return workspace

    async def _viewer_rpc(
        self, workspace_id: uuid.UUID, action: str, args: dict
    ) -> dict:
        """Use the existing correlated desktop_action transport."""
        from apps.harness.access.runner_accessor import (
            RunnerAccessorError,
            create_harness_accessor,
        )

        accessor = await create_harness_accessor(self, str(workspace_id))
        try:
            return await accessor.desktop_action(action, args)
        except (RunnerAccessorError, TimeoutError) as exc:
            raise ConflictError(
                "Viewer desktop operation could not be confirmed"
            ) from exc

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
        info = self.get_desktop_info(str(workspace_id)) or {}
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

        await self._emit_to_runner(
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

        await self._emit_to_runner(
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
        *,
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

            if copy_selection:
                info = self.get_desktop_info(str(workspace_id)) or {}
                if info.get("computer_use"):
                    raise ConflictError(
                        "Unable to copy the desktop selection. Try again."
                    )
                from apps.harness.repositories import HarnessSessionRepository

                busy_sessions = await sync_to_async(
                    HarnessSessionRepository.list_busy_computeruse_for_workspace
                )(workspace_id)
                if busy_sessions:
                    raise ConflictError(
                        "Unable to copy the desktop selection. Try again."
                    )

            args = {"workspace_id": str(workspace_id)}
            if copy_selection:
                args["copy_selection"] = True
                if shift:
                    args["shift"] = True
            response = await self._call_runner(
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
        container_ip: str,
        network_name: str,
        runner_id: str | None = None,
        *,
        viewer: bool | None = None,
        computer_use: bool | None = None,
        mcp: bool | None = None,
        holder_count: int | None = None,
        generation: int | None = None,
        epoch: str | None = None,
    ) -> None:
        """Handle desktop:started after a viewer lease is acquired."""
        from ...sio_server import emit_to_frontend

        current = self.get_desktop_info(workspace_id) or {}
        epoch, generation = self._inherit_current_desktop_version(
            workspace_id,
            runner_id=runner_id,
            epoch=epoch,
            generation=generation,
        )
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        task = None
        if task_id:
            task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
            if task and not self._validate_task_runner(task, runner_id):
                return

        current = self.get_desktop_info(workspace_id) or {}
        recorded = self._record_active_desktop(
            workspace_id,
            {
                **current,
                **{
                    key: value
                    for key, value in {
                        "mcp": mcp,
                        "holder_count": holder_count,
                        "generation": generation,
                        "epoch": epoch,
                    }.items()
                    if value is not None
                },
                "port": port,
                "container_ip": container_ip,
                "network_name": network_name,
                "viewer": True if viewer is None else viewer,
                **({"computer_use": computer_use} if computer_use is not None else {}),
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
                "computer_use_active": bool(
                    (self.get_desktop_info(workspace_id) or {}).get("computer_use")
                ),
                **self._desktop_public_summary(workspace_id),
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
        viewer: bool | None = None,
        computer_use: bool | None = None,
        mcp: bool | None = None,
        holder_count: int | None = None,
        generation: int | None = None,
        epoch: str | None = None,
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
        epoch, generation = self._inherit_current_desktop_version(
            workspace_id,
            runner_id=runner_id,
            epoch=epoch,
            generation=generation,
        )
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        current = self.get_desktop_info(workspace_id) or {}
        recorded = self._record_active_desktop(
            workspace_id,
            {
                **current,
                **{
                    key: value
                    for key, value in {
                        "mcp": mcp,
                        "holder_count": holder_count,
                        "generation": generation,
                        "epoch": epoch,
                    }.items()
                    if value is not None
                },
                "port": port,
                "container_ip": container_ip,
                "network_name": network_name,
                **({"viewer": viewer} if viewer is not None else {}),
                **({"computer_use": computer_use} if computer_use is not None else {}),
            },
            runner_id=runner_id,
        )
        if not recorded:
            return
        updated = self.get_desktop_info(workspace_id) or {}
        logger.info(
            "Desktop process announced: workspace=%s, viewer=%s, computer_use=%s",
            workspace_id,
            updated.get("viewer", False),
            updated.get("computer_use", False),
        )
        await emit_to_frontend(
            "desktop:started",
            {
                "workspace_id": workspace_id,
                "proxy_url": f"/ws/desktop/{workspace_id}/",
                "computer_use_active": bool(updated.get("computer_use")),
                **self._desktop_public_summary(workspace_id),
            },
            workspace_id,
        )

    async def handle_desktop_viewer_released(
        self,
        task_id: str,
        workspace_id: str,
        runner_id: str | None = None,
        *,
        computer_use_active: bool | None = None,
        viewer_held: bool | None = None,
        mcp: bool | None = None,
        holder_count: int | None = None,
        generation: int | None = None,
        epoch: str | None = None,
    ) -> None:
        """Handle viewer lease release while the desktop process stays up.

        Ownership is validated against the real workspace owner (not just
        the in-memory cache) so a foreign runner can never clear another
        runner's desktop state, even with an empty cache.
        """
        from ...sio_server import emit_to_frontend

        if not await self._desktop_event_owned_by_runner(workspace_id, runner_id):
            return
        epoch, generation = self._inherit_current_desktop_version(
            workspace_id,
            runner_id=runner_id,
            epoch=epoch,
            generation=generation,
        )
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        task = await sync_to_async(self.tasks.get_by_id)(uuid.UUID(task_id))
        if task:
            if not self._validate_task_runner(task, runner_id):
                return
            await sync_to_async(self.tasks.complete)(task)

        epoch, generation = self._inherit_current_desktop_version(
            workspace_id,
            runner_id=runner_id,
            epoch=epoch,
            generation=generation,
        )
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
        ):
            return

        current = self._active_desktops.get(workspace_id)
        if current is not None:
            updated = dict(current)
            if viewer_held is not None:
                updated["viewer"] = viewer_held
            elif holder_count is None:
                updated["viewer"] = False
            updated.update(
                {
                    key: value
                    for key, value in {
                        "mcp": mcp,
                        "holder_count": holder_count,
                        "generation": generation,
                        "epoch": epoch,
                    }.items()
                    if value is not None
                }
            )
            if computer_use_active is not None:
                updated["computer_use"] = computer_use_active
            if not self._record_active_desktop(
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
        await emit_to_frontend(
            "desktop:viewer_released",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "computer_use_active": bool(
                    (self.get_desktop_info(workspace_id) or {}).get(
                        "computer_use", False
                    )
                ),
                **self._desktop_public_summary(workspace_id),
            },
            workspace_id,
        )

    async def handle_desktop_stopped(
        self,
        task_id: str | None,
        workspace_id: str,
        runner_id: str | None = None,
        *,
        generation: int | None = None,
        epoch: str | None = None,
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

        if self._desktop_state_is_stale(
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
                if not self._validate_task_runner(task, runner_id):
                    return
                await sync_to_async(self.tasks.complete)(task)

        # Task lookup/completion above can yield to newer process announcements.
        # Recheck immediately before cache removal so delayed stops cannot win.
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=epoch,
            generation=generation,
            runner_id=runner_id,
            active=False,
        ):
            return

        current = self.get_desktop_info(workspace_id) or {}
        stopped_epoch = epoch or current.get("epoch")
        stopped_generation = (
            generation if generation is not None else current.get("generation")
        )
        if self._desktop_state_is_stale(
            workspace_id,
            epoch=stopped_epoch,
            generation=stopped_generation,
            runner_id=runner_id,
            active=False,
        ):
            return
        self._remember_desktop_state(
            workspace_id,
            epoch=stopped_epoch,
            generation=stopped_generation,
            runner_id=runner_id or self._desktop_workspace_runner.get(workspace_id),
            active=False,
        )
        self._active_desktops.pop(workspace_id, None)
        self._desktop_workspace_runner.pop(workspace_id, None)

        logger.info("Desktop stopped: workspace=%s", workspace_id)

        await emit_to_frontend(
            "desktop:stopped",
            {
                "workspace_id": workspace_id,
                "task_id": task_id,
                "generation": generation,
                "epoch": epoch,
            },
            workspace_id,
        )
