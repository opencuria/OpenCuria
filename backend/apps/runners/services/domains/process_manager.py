"""Background process management (Phase 3 domain mixin).

Byte-identical method bodies extracted from the former monolithic
``apps/runners/services/__init__.py``. No logic changes; the mixin relies
on the facade (``RunnerService``) providing ``self.workspaces``,
``self.tasks``, ``self.processes``, ``self._process_pending`` plus sibling
helpers (``_emit_to_runner`` via ``RunnerTransportMixin``,
``_forward_*`` / ``_push_process_*`` via ``FrontendBusMixin``,
``_ensure_process_dispatchable`` / ``_validate_task_runner`` /
``_validate_harness_workspace_runner`` via ``OwnershipMixin``). Lazy
``django`` / ``apps.harness`` imports stay inside the method bodies (no new
top-level coupling). The single ``RunnerService._process_log_paths`` call
inside the static ``_process_verify_candidates`` became the explicit
``ProcessManagerMixin._process_log_paths`` (same staticmethod, class name
instead of facade name — no circular import, no behaviour change).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError
from common.utils import generate_uuid

from ...enums import ProcessStatus, WorkspaceStatus
from ...exceptions import RunnerOfflineError, WorkspaceNotFoundError

logger = logging.getLogger(__name__)


class ProcessManagerMixin:
    """Workspace background-process RPCs shared by RunnerService."""

    # ------------------------------------------------------------------
    # Background processes (workspace-bound, no auto-restart)
    # ------------------------------------------------------------------

    # Runner harness:process_* handlers only emit *_result events (no
    # Socket.IO ACK payload), so process RPCs correlate via request_id
    # futures stored in ``self._process_pending`` (cf. harness accessor).
    _PROCESS_RPC_TIMEOUT_SECONDS = 30
    _PROCESS_LIVE_TIMEOUT_SECONDS = 10

    _PROCESS_RESULT_EVENTS = frozenset(
        {
            "harness:process_start_result",
            "harness:process_list_result",
            "harness:process_get_result",
            "harness:process_stop_result",
            "harness:process_verify_result",
        }
    )

    #: Max vanished candidates sent in one process_verify RPC. Mirrors the
    #: runner-side cap (``_BACKGROUND_VERIFY_MAX_ENTRIES``); larger sets
    #: are chunked into multiple RPCs.
    _PROCESS_VERIFY_BATCH_SIZE = 100

    #: Max age of a RUNNING row that never got a pid (start RPC never
    #: acknowledged, e.g. backend restart during start). Older rows are
    #: swept to FAILED so they cannot stay RUNNING forever.
    _PROCESS_UNCONFIRMED_MAX_AGE_SECONDS = 2 * _PROCESS_RPC_TIMEOUT_SECONDS

    def handle_process_reply(
        self,
        event: str,
        data: dict,
        runner_id: str | None = None,
    ) -> None:
        """Route a harness:process_*_result reply to its waiter (sync).

        Called from Socket.IO handlers via ``sync_to_async``. Validates
        that the workspace belongs to the sending runner, then resolves
        the correlated request future.
        """
        if event not in self._PROCESS_RESULT_EVENTS:
            return
        workspace_id = str(data.get("workspace_id", ""))
        request_id = str(data.get("request_id", ""))
        if not request_id:
            logger.warning("%s rejected: missing request_id", event)
            return
        if runner_id and workspace_id:
            try:
                workspace_uuid = uuid.UUID(workspace_id)
            except (ValueError, TypeError):
                logger.warning(
                    "%s rejected: invalid workspace_id %s",
                    event,
                    workspace_id,
                )
                return
            if not self._validate_harness_workspace_runner(
                workspace_uuid, runner_id
            ):
                return
        self._resolve_process_future(request_id, data)

    async def _await_process_result(
        self,
        *,
        request_id: str,
        event: str,
        payload: dict,
        runner: "Runner",
        timeout: float,
    ) -> dict:
        """Emit a process RPC and wait for its correlated result."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._process_pending[request_id] = future
        try:
            await self._emit_to_runner(runner, event, payload)
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise ConflictError(
                f"Runner did not respond to '{event}' within {timeout:.0f}s"
            ) from exc
        finally:
            self._process_pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    @staticmethod
    def _clean_process_name(name: str | None) -> str:
        """Validate a process name (identity per workspace, case-sensitive).

        Strips surrounding whitespace; empty names and names longer than
        255 characters raise ``ValueError``.
        """
        cleaned = (name or "").strip()
        if not cleaned:
            raise ValueError("name must not be empty")
        if len(cleaned) > 255:
            raise ValueError("name must be at most 255 characters")
        return cleaned

    @staticmethod
    def _process_log_paths(
        process_id: uuid.UUID,
        run_count: int,
    ) -> tuple[str, str]:
        """Return the absolute (log_path, exit_path) for a process run.

        The first run keeps the legacy ``<id>.log`` layout (backward
        compat); later runs get ``<id>_r<run>.log`` so earlier logs are
        never overwritten or deleted.
        """
        base = f"/workspace/.opencuria/processes/{process_id}"
        if (run_count or 0) > 1:
            return (f"{base}_r{run_count}.log", f"{base}_r{run_count}.exit")
        return (f"{base}.log", f"{base}.exit")

    async def _resolve_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        kind: str | None = None,
        session_id: uuid.UUID | str | None = None,
    ) -> "WorkspaceProcess":
        """Resolve a process by id or exact name, scoped to a workspace.

        ``kind="temp"`` requires ``session_id``: temp rows are
        session-scoped, so a UUID/name only resolves within the owning
        session. ``kind=None`` keeps the agent semantics (own temp rows
        first, then persistent rows).
        """
        parsed_session = self._coerce_session_id(session_id)
        resolved = await sync_to_async(self.processes.resolve_for_workspace)(
            workspace_id, id_or_name, kind=kind, session_id=parsed_session
        )
        if resolved is None:
            raise WorkspaceNotFoundError(str(id_or_name))
        return resolved

    @staticmethod
    def _coerce_session_id(session_id: uuid.UUID | str | None) -> uuid.UUID | None:
        """Parse an optional session id or raise ValueError."""
        if session_id is None:
            return None
        if isinstance(session_id, uuid.UUID):
            return session_id
        try:
            return uuid.UUID(str(session_id))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid session_id: {session_id}") from exc

    @staticmethod
    def _coerce_process_kind(kind: str | None) -> str:
        """Normalize a process kind (persistent default) or raise ValueError."""
        cleaned = (kind or "persistent").strip().lower()
        if cleaned not in ("persistent", "temp"):
            raise ValueError(f"Invalid process kind: {kind!r}")
        return cleaned

    @staticmethod
    def _assert_not_temp(process: "WorkspaceProcess", *, action: str) -> None:
        """Reject user-driven start/restart/delete of temp processes."""
        if str(getattr(process, "kind", "") or "") == "temp":
            raise ConflictError(
                f"Temporary processes cannot be {action} by users; "
                "they live only for their agent session (stop is allowed)."
            )

    async def start_process(
        self,
        workspace_id: uuid.UUID,
        command: str,
        *,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str,
        user=None,
        session_id: uuid.UUID | str | None = None,
        kind: str | None = None,
    ) -> "WorkspaceProcess":
        """Start a detached background process in a workspace (upsert by name).

        The process ``name`` is the identity within its workspace: a new
        name creates a fresh row, an existing name reuses the same row
        (stable id) with ``run_count + 1`` and a new log file — earlier
        logs are kept. The stored command/workdir config is overwritten
        on a name restart. A still-running row is stopped first (same
        SIGTERM->SIGKILL logic as :meth:`stop_process`); if that stop
        fails the new run is aborted and the DB is left unchanged.
        There is no auto-restart — every run is explicit (same name).

        ``kind="temp"`` creates a session-scoped row instead: the name
        is unique per ``(workspace, name, session_id)`` and requires
        ``session_id``. Temp rows are stopped by the harness cleanup
        hook when the owning run finishes and are never started,
        restarted, or deleted by users (REST/MCP reject those actions).

        Raises:
            WorkspaceNotFoundError: Unknown workspace.
            WorkspaceStateError: Workspace not running / pending deletion.
            RunnerOfflineError: Owning runner is offline.
            ConflictError: Runner reported an error or timed out.
            ValueError: Empty command or name, invalid kind, or temp
                without session_id.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        runner = self._ensure_process_dispatchable(workspace)

        cleaned_name = self._clean_process_name(name)
        cleaned_command = (command or "").strip()
        if not cleaned_command:
            raise ValueError("command must not be empty")
        safe_workdir = (workdir or "/workspace").strip() or "/workspace"
        parsed_kind = self._coerce_process_kind(kind)
        parsed_session = self._coerce_session_id(session_id)
        if parsed_kind == "temp" and parsed_session is None:
            raise ValueError("session_id is required for temporary processes")

        if parsed_kind == "temp":
            existing = await sync_to_async(self.processes.get_temp_by_name)(
                workspace_id, cleaned_name, parsed_session
            )
        else:
            existing = await sync_to_async(self.processes.get_by_name)(
                workspace_id, cleaned_name
            )
        if existing is not None and existing.status == ProcessStatus.RUNNING:
            await self._stop_running_process(workspace, existing)

        process_id: uuid.UUID
        run_count: int
        if existing is not None:
            process_id = existing.id
            run_count = (existing.run_count or 1) + 1
            if existing.run_count is None or existing.run_count <= 0:
                run_count = 2
            await sync_to_async(self.processes.update_for_restart)(
                process_id,
                command=cleaned_command,
                workdir=safe_workdir,
                log_path="",
                run_count=run_count,
                created_by=user,
                session_id=parsed_session,
            )
        else:
            process_id = generate_uuid()
            run_count = 1
            try:
                await sync_to_async(self.processes.create)(
                    process_id=process_id,
                    workspace=workspace,
                    command=cleaned_command,
                    workdir=safe_workdir,
                    name=cleaned_name,
                    created_by=user,
                    session_id=parsed_session,
                    run_count=run_count,
                    kind=parsed_kind,
                )
            except Exception as exc:
                from django.db import IntegrityError

                if not isinstance(exc, IntegrityError):
                    raise
                # Race: a concurrent start won the unique name slot —
                # retry once as a restart of the winner (same kind scope).
                if parsed_kind == "temp":
                    winner = await sync_to_async(
                        self.processes.get_temp_by_name
                    )(workspace_id, cleaned_name, parsed_session)
                else:
                    winner = await sync_to_async(self.processes.get_by_name)(
                        workspace_id, cleaned_name
                    )
                if winner is None:
                    raise
                existing = winner
                process_id = winner.id
                if winner.status == ProcessStatus.RUNNING:
                    await self._stop_running_process(workspace, winner)
                    winner = await sync_to_async(self.processes.get_by_id)(
                        winner.id
                    ) or winner
                    existing = winner
                run_count = (winner.run_count or 1) + 1
                if winner.run_count is None or winner.run_count <= 0:
                    run_count = 2
                await sync_to_async(self.processes.update_for_restart)(
                    process_id,
                    command=cleaned_command,
                    workdir=safe_workdir,
                    log_path="",
                    run_count=run_count,
                    created_by=user,
                    session_id=parsed_session,
                )

        log_path_abs, exit_path_abs = self._process_log_paths(
            process_id, run_count
        )
        request_id = uuid.uuid4().hex
        try:
            result = await self._await_process_result(
                request_id=request_id,
                event="harness:process_start",
                payload={
                    "request_id": request_id,
                    "workspace_id": str(workspace_id),
                    "process_id": str(process_id),
                    "command": cleaned_command,
                    "workdir": safe_workdir,
                    "env": dict(env or {}),
                    "name": cleaned_name,
                    "log_path": log_path_abs,
                    "exit_path": exit_path_abs,
                    "run_count": run_count,
                },
                runner=runner,
                timeout=self._PROCESS_RPC_TIMEOUT_SECONDS,
            )
        except (ConflictError, RunnerOfflineError, RuntimeError):
            await sync_to_async(self.processes.mark_finished)(
                process_id,
                status=ProcessStatus.FAILED,
                exit_code=None,
            )
            self._push_process_status(
                workspace_id=str(workspace_id),
                process_id=str(process_id),
                status=ProcessStatus.FAILED,
                run_count=run_count,
            )
            raise

        error = result.get("error")
        if error:
            await sync_to_async(self.processes.mark_finished)(
                process_id,
                status=ProcessStatus.FAILED,
                exit_code=None,
            )
            self._push_process_status(
                workspace_id=str(workspace_id),
                process_id=str(process_id),
                status=ProcessStatus.FAILED,
                run_count=run_count,
            )
            raise ConflictError(f"Runner failed to start process: {error}")

        pid = result.get("pid")
        log_path = str(result.get("log_path") or "") or log_path_abs
        await sync_to_async(self.processes.update_status)(
            process_id,
            status=ProcessStatus.RUNNING,
            pid=int(pid) if pid is not None else None,
            log_path=log_path or None,
        )
        refreshed = await sync_to_async(self.processes.get_by_id)(process_id)
        if refreshed is None:
            raise ConflictError(
                f"Process record {process_id} vanished after start"
            )
        self._push_process_status(
            workspace_id=str(workspace_id),
            process_id=str(process_id),
            status=ProcessStatus.RUNNING,
            pid=refreshed.pid,
            log_path=log_path,
            run_count=run_count,
        )
        logger.info(
            "Started background process %s in workspace %s (pid=%s, run=%s)",
            process_id,
            workspace_id,
            refreshed.pid,
            run_count,
        )
        return refreshed

    async def _stop_running_process(
        self,
        workspace: "Workspace",
        stored: "WorkspaceProcess",
    ) -> "WorkspaceProcess":
        """Stop a RUNNING record via the runner (SIGTERM, then SIGKILL).

        Shared by :meth:`stop_process`, :meth:`start_process` (restart via
        the same name), :meth:`restart_process` and :meth:`delete_process`.
        A runner-side "not found" is treated as already gone (EXITED);
        other errors propagate and the DB is left to the caller.
        """
        workspace_id = workspace.id
        process_id = stored.id
        request_id = uuid.uuid4().hex
        result = await self._await_process_result(
            request_id=request_id,
            event="harness:process_stop",
            payload={
                "request_id": request_id,
                "workspace_id": str(workspace_id),
                "process_id": str(process_id),
            },
            runner=workspace.runner,
            timeout=self._PROCESS_RPC_TIMEOUT_SECONDS,
        )

        error = result.get("error")
        if error:
            if "not found" in str(error).lower():
                # The runner lost tracking (e.g. restart) but the OS
                # process may still live: verify once — a reattached
                # "running" answer means the stop must be retried
                # against the revived entry instead of marking EXITED.
                verified = await self._verify_process_candidates(
                    workspace, [stored]
                )
                live_status = str((verified.get(str(process_id)) or {}).get(
                    "status") or "")
                if live_status == "running":
                    logger.info(
                        "Process %s reattached on runner during stop, "
                        "retrying stop",
                        process_id,
                    )
                    retry_request_id = uuid.uuid4().hex
                    result = await self._await_process_result(
                        request_id=retry_request_id,
                        event="harness:process_stop",
                        payload={
                            "request_id": retry_request_id,
                            "workspace_id": str(workspace_id),
                            "process_id": str(process_id),
                        },
                        runner=workspace.runner,
                        timeout=self._PROCESS_RPC_TIMEOUT_SECONDS,
                    )
                    retry_error = result.get("error")
                    if retry_error:
                        if "not found" in str(retry_error).lower():
                            return await self._mark_process_vanished(
                                workspace, stored
                            )
                        raise ConflictError(
                            f"Runner failed to stop process: {retry_error}"
                        )
                    return await self._mark_process_stopped(
                        workspace, stored, result
                    )
                # Verify confirms the process is gone (exited/unknown)
                # or the verify itself failed: fall through to EXITED
                # only when the process is really gone; a verify
                # timeout keeps the row RUNNING (fail-open, DB fallback).
                if live_status in ("exited", "unknown"):
                    return await self._mark_process_vanished(
                        workspace,
                        stored,
                        exit_code=(verified.get(str(process_id)) or {}).get(
                            "exit_code"
                        ),
                    )
                return stored
            raise ConflictError(f"Runner failed to stop process: {error}")

        return await self._mark_process_stopped(workspace, stored, result)

    async def _mark_process_stopped(
        self,
        workspace: "Workspace",
        stored: "WorkspaceProcess",
        result: dict,
    ) -> "WorkspaceProcess":
        """Persist a successful stop reply (EXITED vs KILLED mapping)."""
        workspace_id = workspace.id
        process_id = stored.id
        live_exit = result.get("exit_code")
        exit_code = int(live_exit) if live_exit is not None else None
        status = (
            ProcessStatus.EXITED
            if str(result.get("status") or "") == "exited"
            else ProcessStatus.KILLED
        )
        await sync_to_async(self.processes.mark_finished)(
            process_id,
            status=status,
            exit_code=exit_code,
        )
        self._push_process_status(
            workspace_id=str(workspace_id),
            process_id=str(process_id),
            status=status,
            exit_code=exit_code,
            pid=stored.pid,
            run_count=stored.run_count,
        )
        logger.info(
            "Stopped background process %s in workspace %s (status=%s)",
            process_id,
            workspace_id,
            status,
        )
        refreshed = await sync_to_async(self.processes.get_for_workspace)(
            process_id, workspace_id
        )
        return refreshed or stored

    async def _mark_process_vanished(
        self,
        workspace: "Workspace",
        stored: "WorkspaceProcess",
        *,
        exit_code: int | None = None,
    ) -> "WorkspaceProcess":
        """Mark a RUNNING row EXITED after the runner confirmed it is gone."""
        workspace_id = workspace.id
        process_id = stored.id
        await sync_to_async(self.processes.mark_finished)(
            process_id,
            status=ProcessStatus.EXITED,
            exit_code=exit_code,
        )
        self._push_process_status(
            workspace_id=str(workspace_id),
            process_id=str(process_id),
            status=ProcessStatus.EXITED,
            exit_code=exit_code,
            pid=stored.pid,
            run_count=stored.run_count,
        )
        refreshed = await sync_to_async(self.processes.get_for_workspace)(
            process_id, workspace_id
        )
        return refreshed or stored

    @staticmethod
    def _process_verify_candidates(
        processes: list["WorkspaceProcess"],
    ) -> list[dict]:
        """Build ``expected`` payloads for a process_verify RPC."""
        candidates: list[dict] = []
        for process in processes:
            if process.pid is None:
                continue
            try:
                process_uuid = uuid.UUID(str(process.id))
            except (ValueError, TypeError, AttributeError):
                continue
            log_path_abs, exit_path_abs = ProcessManagerMixin._process_log_paths(
                process_uuid, process.run_count or 1
            )
            candidates.append(
                {
                    "process_id": str(process.id),
                    "pid": process.pid,
                    "log_path": process.log_path or log_path_abs,
                    "exit_path": exit_path_abs,
                    "command": process.command or "",
                    "workdir": process.workdir or "/workspace",
                    "name": process.name or "",
                }
            )
        return candidates

    async def _verify_process_candidates(
        self,
        workspace: "Workspace",
        processes: list["WorkspaceProcess"],
    ) -> dict[str, dict]:
        """Verify vanished candidates via ``harness:process_verify`` RPC.

        Returns ``{process_id: verify_result}``. An empty dict means the
        verify failed (timeout/offline/error) — callers must keep the DB
        rows RUNNING in that case (fail-open, never mark EXITED on a
        failed verify).
        """
        candidates = self._process_verify_candidates(processes)
        if not candidates:
            return {}
        runner = workspace.runner
        merged: dict[str, dict] = {}
        try:
            for offset in range(0, len(candidates), self._PROCESS_VERIFY_BATCH_SIZE):
                batch = candidates[offset:offset + self._PROCESS_VERIFY_BATCH_SIZE]
                request_id = uuid.uuid4().hex
                result = await self._await_process_result(
                    request_id=request_id,
                    event="harness:process_verify",
                    payload={
                        "request_id": request_id,
                        "workspace_id": str(workspace.id),
                        "expected": batch,
                    },
                    runner=runner,
                    timeout=self._PROCESS_LIVE_TIMEOUT_SECONDS,
                )
                if result.get("error"):
                    logger.warning(
                        "Process verify failed for workspace %s: %s",
                        workspace.id,
                        result.get("error"),
                    )
                    return {}
                reported = result.get("processes") or []
                if not isinstance(reported, list):
                    return {}
                for entry in reported:
                    if isinstance(entry, dict) and entry.get("process_id"):
                        merged[str(entry["process_id"])] = entry
        except (ConflictError, RunnerOfflineError, RuntimeError) as exc:
            logger.debug(
                "Process verify unavailable for workspace %s: %s",
                workspace.id,
                exc,
            )
            return {}
        return merged

    async def reverify_vanished_processes(
        self,
        workspace_id: uuid.UUID,
        vanished: list["WorkspaceProcess"],
    ) -> list[uuid.UUID]:
        """Verify vanished rows and apply the outcome (async, RPC path).

        Rows the runner reattached as ``running`` are revived to RUNNING
        (pid/log_path refreshed, frontend push); rows confirmed
        ``exited``/``unknown`` become EXITED; rows the verify could not
        confirm stay RUNNING (fail-open). Returns IDs of changed rows.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        if not vanished:
            return []
        verified = await self._verify_process_candidates(workspace, vanished)
        changed: list[uuid.UUID] = []
        for process in vanished:
            key = str(process.id)
            live = verified.get(key)
            if live is None:
                continue
            live_status = str(live.get("status") or "")
            if live_status == "running":
                live_pid = live.get("pid")
                try:
                    pid = int(live_pid) if live_pid is not None else process.pid
                except (TypeError, ValueError):
                    pid = process.pid
                await sync_to_async(self.processes.update_status)(
                    process.id,
                    status=ProcessStatus.RUNNING,
                    pid=pid,
                )
                refreshed = await sync_to_async(self.processes.get_by_id)(
                    process.id
                )
                current = refreshed or process
                self._push_process_status(
                    workspace_id=str(workspace_id),
                    process_id=key,
                    status=ProcessStatus.RUNNING,
                    pid=current.pid,
                    log_path=current.log_path or None,
                    run_count=current.run_count,
                )
                logger.info(
                    "Process %s reattached on runner (pid=%s)",
                    process.id,
                    current.pid,
                )
                changed.append(process.id)
            elif live_status in ("exited", "unknown"):
                live_exit = live.get("exit_code")
                try:
                    exit_code = (
                        int(live_exit) if live_exit is not None else None
                    )
                except (TypeError, ValueError):
                    exit_code = None
                # "unknown" without an exit code means the runner probed
                # and found nothing — the process is gone.
                await sync_to_async(self.processes.mark_finished)(
                    process.id,
                    status=ProcessStatus.EXITED,
                    exit_code=exit_code,
                )
                self._push_process_status(
                    workspace_id=str(workspace_id),
                    process_id=key,
                    status=ProcessStatus.EXITED,
                    exit_code=exit_code,
                    pid=process.pid,
                )
                logger.info(
                    "Process %s vanished from runner report, marking exited",
                    process.id,
                )
                changed.append(process.id)
        return changed

    def sweep_unconfirmed_processes(
        self,
        workspace_id: str,
        *,
        max_age_seconds: float | None = None,
    ) -> list[uuid.UUID]:
        """Fail stale RUNNING rows that never got a pid (sync).

        A row without pid means the start RPC was never acknowledged
        (e.g. backend restart mid-start). Such rows are skipped by
        :meth:`reconcile_workspace_processes` forever; this sweeper
        fails them once they are older than *max_age_seconds* so they
        cannot stay RUNNING indefinitely. Pushes frontend updates per
        change. Returns IDs of changed processes.
        """
        from django.utils import timezone as tz

        try:
            workspace_uuid = uuid.UUID(str(workspace_id))
        except (ValueError, TypeError):
            return []
        limit = (
            self._PROCESS_UNCONFIRMED_MAX_AGE_SECONDS
            if max_age_seconds is None
            else max_age_seconds
        )
        cutoff = tz.now() - timedelta(seconds=limit)
        changed: list[uuid.UUID] = []
        running = list(
            self.processes.list_running_by_workspace(workspace_uuid)
        )
        for process in running:
            if process.pid is not None:
                continue
            started_at = getattr(process, "started_at", None)
            if started_at is not None and started_at >= cutoff:
                continue
            self.processes.mark_finished(
                process.id,
                status=ProcessStatus.FAILED,
                exit_code=None,
            )
            self._push_process_status(
                workspace_id=str(workspace_id),
                process_id=str(process.id),
                status=ProcessStatus.FAILED,
                pid=None,
            )
            logger.info(
                "Process %s never confirmed start (pid=None), marking failed",
                process.id,
            )
            changed.append(process.id)
        return changed

    async def restart_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        user=None,
        session_id: uuid.UUID | str | None = None,
    ) -> "WorkspaceProcess":
        """Restart a named process on the same row (stable id, new log).

        Reuses the stored command/workdir (no overwrite, unlike
        :meth:`start_process` with the same name). A still-running row is
        stopped first; old logs are kept (new ``_r<run>`` log path).

        Temp rows only restart from within their owning session
        (``session_id`` must match); user-driven restarts are rejected
        without a session scope (see :meth:`_assert_not_temp` usage in
        the API layer — a plain restart without ``session_id`` never
        touches temp rows).
        """
        parsed_session = self._coerce_session_id(session_id)
        stored = await self._resolve_process(
            workspace_id, id_or_name, session_id=parsed_session
        )
        if str(getattr(stored, "kind", "") or "") == "temp":
            if parsed_session is None or stored.session_id != parsed_session:
                raise WorkspaceNotFoundError(str(id_or_name))
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        self._ensure_process_dispatchable(workspace)

        current = stored
        if current.status == ProcessStatus.RUNNING:
            current = await self._stop_running_process(workspace, current)
            current = (
                await sync_to_async(self.processes.get_by_id)(current.id)
                or current
            )

        run_count = (current.run_count or 1) + 1
        if current.run_count is None or current.run_count <= 0:
            run_count = 2
        process_id = current.id
        command = current.command
        workdir = current.workdir
        runner = workspace.runner
        log_path_abs, exit_path_abs = self._process_log_paths(
            process_id, run_count
        )
        await sync_to_async(self.processes.update_for_restart)(
            process_id,
            command=command,
            workdir=workdir,
            log_path="",
            run_count=run_count,
            created_by=user,
            session_id=parsed_session,
        )

        request_id = uuid.uuid4().hex
        try:
            result = await self._await_process_result(
                request_id=request_id,
                event="harness:process_start",
                payload={
                    "request_id": request_id,
                    "workspace_id": str(workspace_id),
                    "process_id": str(process_id),
                    "command": command,
                    "workdir": workdir,
                    "env": {},
                    "name": current.name,
                    "log_path": log_path_abs,
                    "exit_path": exit_path_abs,
                    "run_count": run_count,
                },
                runner=runner,
                timeout=self._PROCESS_RPC_TIMEOUT_SECONDS,
            )
        except (ConflictError, RunnerOfflineError, RuntimeError):
            await sync_to_async(self.processes.mark_finished)(
                process_id,
                status=ProcessStatus.FAILED,
                exit_code=None,
            )
            self._push_process_status(
                workspace_id=str(workspace_id),
                process_id=str(process_id),
                status=ProcessStatus.FAILED,
                run_count=run_count,
            )
            raise

        error = result.get("error")
        if error:
            await sync_to_async(self.processes.mark_finished)(
                process_id,
                status=ProcessStatus.FAILED,
                exit_code=None,
            )
            self._push_process_status(
                workspace_id=str(workspace_id),
                process_id=str(process_id),
                status=ProcessStatus.FAILED,
                run_count=run_count,
            )
            raise ConflictError(f"Runner failed to start process: {error}")

        pid = result.get("pid")
        log_path = str(result.get("log_path") or "") or log_path_abs
        await sync_to_async(self.processes.update_status)(
            process_id,
            status=ProcessStatus.RUNNING,
            pid=int(pid) if pid is not None else None,
            log_path=log_path or None,
        )
        refreshed = await sync_to_async(self.processes.get_by_id)(process_id)
        process = refreshed or current
        self._push_process_status(
            workspace_id=str(workspace_id),
            process_id=str(process_id),
            status=ProcessStatus.RUNNING,
            pid=process.pid,
            log_path=log_path,
            run_count=run_count,
        )
        logger.info(
            "Restarted background process %s in workspace %s (pid=%s, run=%s)",
            process_id,
            workspace_id,
            process.pid,
            run_count,
        )
        return process

    async def delete_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
    ) -> uuid.UUID:
        """Delete a process row by id or name (stops it first if running).

        Temp rows cannot be deleted (they stay in the DB as finished
        rows); use :meth:`stop_process` instead.

        Returns the deleted process id. Pushes ``process:removed``.
        """
        stored = await self._resolve_process(workspace_id, id_or_name)
        self._assert_not_temp(stored, action="deleted")
        if stored.status == ProcessStatus.RUNNING:
            workspace = await sync_to_async(self.workspaces.get_by_id)(
                workspace_id
            )
            if workspace is None:
                raise WorkspaceNotFoundError(str(workspace_id))
            self._ensure_process_dispatchable(workspace)
            await self._stop_running_process(workspace, stored)
        process_id = stored.id
        await sync_to_async(self.processes.delete_for_workspace)(
            process_id, workspace_id
        )
        self._push_process_removed(
            workspace_id=str(workspace_id),
            process_id=str(process_id),
        )
        logger.info(
            "Deleted background process %s in workspace %s",
            process_id,
            workspace_id,
        )
        return process_id

    async def list_processes(
        self,
        workspace_id: uuid.UUID,
        *,
        kinds: tuple[str, ...] | None = None,
        running_only: bool = False,
        session_id: uuid.UUID | str | None = None,
    ) -> list["WorkspaceProcess"]:
        """Return DB processes, merged with live runner state when online.

        Falls back to plain DB records when the runner is offline or the
        live lookup times out.

        ``session_id`` scopes temp rows to one agent session (the agent
        only sees its own temps); persistent rows are always included.
        ``kinds``/``running_only`` further restrict the result — the
        user list passes ``running_only=True`` so finished temps vanish
        from the UI while their DB rows are kept.
        """
        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))

        if workspace.status == WorkspaceStatus.RUNNING and workspace.runner.is_online:
            try:
                request_id = uuid.uuid4().hex
                result = await self._await_process_result(
                    request_id=request_id,
                    event="harness:process_list",
                    payload={
                        "request_id": request_id,
                        "workspace_id": str(workspace_id),
                    },
                    runner=workspace.runner,
                    timeout=self._PROCESS_LIVE_TIMEOUT_SECONDS,
                )
                if not result.get("error"):
                    reported = result.get("processes") or []
                    if isinstance(reported, list):
                        _, vanished = await sync_to_async(
                            self.reconcile_workspace_processes
                        )(str(workspace_id), reported)
                        # Vanished rows may still live on the runner
                        # (restart wiped tracking): verify once — live
                        # rows reattach as RUNNING, dead rows become
                        # EXITED, unverifiable rows stay RUNNING.
                        if vanished:
                            await self.reverify_vanished_processes(
                                workspace_id, vanished
                            )
                        await sync_to_async(self.sweep_unconfirmed_processes)(
                            str(workspace_id)
                        )
            except (ConflictError, RunnerOfflineError, RuntimeError):
                logger.debug(
                    "Live process list unavailable for workspace %s, "
                    "falling back to DB",
                    workspace_id,
                )

        queryset = await sync_to_async(self.processes.list_by_workspace)(
            workspace_id, kinds=kinds, running_only=running_only
        )
        rows = await sync_to_async(list)(queryset)
        parsed_session = self._coerce_session_id(session_id)
        if parsed_session is None:
            return rows
        return [
            row
            for row in rows
            if str(getattr(row, "kind", "") or "") != "temp"
            or row.session_id == parsed_session
        ]

    async def get_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        session_id: uuid.UUID | str | None = None,
    ) -> "WorkspaceProcess":
        """Return one process scoped to a workspace, live-merged when online.

        ``id_or_name`` accepts a process UUID or the exact process name
        (resolved via :meth:`_resolve_process`). A temp row only resolves
        within its owning session (``session_id`` must match); a foreign
        temp id/name raises NotFound.
        """
        parsed_session = self._coerce_session_id(session_id)
        process = await self._resolve_process(
            workspace_id, id_or_name, session_id=parsed_session
        )
        process_id = process.id

        if (
            process.status == ProcessStatus.RUNNING
            and process.pid is not None
            and process.workspace.status == WorkspaceStatus.RUNNING
            and process.workspace.runner.is_online
        ):
            try:
                request_id = uuid.uuid4().hex
                result = await self._await_process_result(
                    request_id=request_id,
                    event="harness:process_get",
                    payload={
                        "request_id": request_id,
                        "workspace_id": str(workspace_id),
                        "process_id": str(process_id),
                    },
                    runner=process.workspace.runner,
                    timeout=self._PROCESS_LIVE_TIMEOUT_SECONDS,
                )
                live = result.get("process") or {}
                if isinstance(live, dict) and not result.get("error"):
                    live_status = str(live.get("status") or "")
                    live_exit = live.get("exit_code")
                    if live_status == "exited" or (
                        live_status == "unknown" and live_exit is not None
                    ):
                        exit_code = (
                            int(live_exit) if live_exit is not None else None
                        )
                        await sync_to_async(self.processes.mark_finished)(
                            process_id,
                            status=ProcessStatus.EXITED,
                            exit_code=exit_code,
                        )
                        self._push_process_status(
                            workspace_id=str(workspace_id),
                            process_id=str(process_id),
                            status=ProcessStatus.EXITED,
                            exit_code=exit_code,
                            pid=process.pid,
                        )
                        refreshed = await sync_to_async(
                            self.processes.get_for_workspace
                        )(process_id, workspace_id)
                        process = refreshed or process
            except (ConflictError, RunnerOfflineError, RuntimeError):
                logger.debug(
                    "Live process lookup unavailable for %s, using DB record",
                    process_id,
                )
        return process

    async def stop_process(
        self,
        workspace_id: uuid.UUID,
        id_or_name: uuid.UUID | str,
        *,
        session_id: uuid.UUID | str | None = None,
    ) -> "WorkspaceProcess":
        """Stop a tracked background process (SIGTERM, then SIGKILL after grace).

        Already-finished records are returned unchanged (idempotent).
        ``id_or_name`` accepts a process UUID or the exact process name
        (resolved via :meth:`_resolve_process`). Temp rows stop like any
        other process — from the owning session via the agent, or by id
        from the user UI; a foreign temp id/name raises NotFound.
        """
        parsed_session = self._coerce_session_id(session_id)
        stored = await self._resolve_process(
            workspace_id, id_or_name, session_id=parsed_session
        )
        if stored.status != ProcessStatus.RUNNING:
            return stored

        workspace = await sync_to_async(self.workspaces.get_by_id)(workspace_id)
        if workspace is None:
            raise WorkspaceNotFoundError(str(workspace_id))
        self._ensure_process_dispatchable(workspace)
        return await self._stop_running_process(workspace, stored)

    async def stop_session_processes(
        self,
        workspace_id: uuid.UUID,
        session_id: uuid.UUID | str,
        *,
        kind: str = "temp",
        reason: str = "session_finished",
    ) -> list["WorkspaceProcess"]:
        """Stop all RUNNING rows of one agent session (cleanup hook).

        Best-effort per row: each stop is attempted individually, failures
        are logged and do not abort the remaining stops. Already-finished
        rows are returned unchanged by :meth:`stop_process`. Finished rows
        are kept in the DB (never deleted here). Idempotent — a second
        call for the same session finds no RUNNING rows and is a no-op.

        Returns the final records (stopped or already finished).
        """
        parsed_session = self._coerce_session_id(session_id)
        if parsed_session is None:
            raise ValueError("session_id is required")
        parsed_kind = self._coerce_process_kind(kind)
        rows = await sync_to_async(list)(
            self.processes.list_running_session_processes(
                workspace_id, parsed_session, kind=parsed_kind
            )
        )
        stopped: list["WorkspaceProcess"] = []
        for row in rows:
            try:
                final = await self.stop_process(
                    workspace_id, row.id, session_id=parsed_session
                )
            except (ConflictError, RunnerOfflineError, RuntimeError) as exc:
                logger.warning(
                    "session process cleanup stop failed",
                    workspace_id=str(workspace_id),
                    session_id=str(parsed_session),
                    process_id=str(row.id),
                    reason=reason,
                    error=str(exc),
                )
                continue
            except Exception:
                logger.exception(
                    "session process cleanup stop failed",
                    workspace_id=str(workspace_id),
                    session_id=str(parsed_session),
                    process_id=str(row.id),
                    reason=reason,
                )
                continue
            stopped.append(final)
        if stopped:
            logger.info(
                "Stopped %d session process(es) for session %s (%s)",
                len(stopped),
                parsed_session,
                reason,
            )
        return stopped

    def reconcile_workspace_processes(
        self,
        workspace_id: str,
        reported: list[dict],
    ) -> tuple[list[uuid.UUID], list["WorkspaceProcess"]]:
        """Reconcile DB running processes with a runner heartbeat list (sync).

        *reported* holds ``{process_id, status, exit_code, pid}`` dicts.
        Running records the runner reports as exited become exited.
        Running records missing from the report (e.g. after a runner
        restart that wiped in-memory tracking) are **not** marked
        finished here — they are returned as *vanished* candidates so
        the async caller can verify them against the runner via
        ``harness:process_verify`` (which reattaches live processes).
        See :meth:`reverify_vanished_processes`.
        Records without a confirmed pid are skipped — the runner has not
        acknowledged their start yet (see :meth:`sweep_unconfirmed_processes`
        for the stale-row sweeper). Pushes frontend updates per change.

        Returns ``(changed_ids, vanished_processes)``.
        """
        try:
            workspace_uuid = uuid.UUID(str(workspace_id))
        except (ValueError, TypeError):
            return [], []
        by_id: dict[str, dict] = {}
        for entry in reported or []:
            if isinstance(entry, dict) and entry.get("process_id"):
                by_id[str(entry["process_id"])] = entry

        changed: list[uuid.UUID] = []
        vanished: list["WorkspaceProcess"] = []
        running = list(
            self.processes.list_running_by_workspace(workspace_uuid)
        )
        for process in running:
            if process.pid is None:
                continue
            live = by_id.get(str(process.id))
            if live is None:
                # Vanished from the runner report: do NOT mark exited
                # here (runner restart wipes in-memory tracking while
                # the OS process lives on). The async caller verifies
                # these candidates via harness:process_verify.
                vanished.append(process)
                continue
            live_status = str(live.get("status") or "")
            live_exit = live.get("exit_code")
            if live_status == "exited" or (
                live_status == "unknown" and live_exit is not None
            ):
                exit_code = int(live_exit) if live_exit is not None else None
                self.processes.mark_finished(
                    process.id,
                    status=ProcessStatus.EXITED,
                    exit_code=exit_code,
                )
                self._push_process_status(
                    workspace_id=str(workspace_id),
                    process_id=str(process.id),
                    status=ProcessStatus.EXITED,
                    exit_code=exit_code,
                    pid=process.pid,
                )
                logger.info(
                    "Process %s exited on runner (exit_code=%s)",
                    process.id,
                    exit_code,
                )
                changed.append(process.id)
        return changed, vanished

    def mark_processes_killed(
        self, workspace_id: str, *, reason: str = "workspace_stopped"
    ) -> int:
        """Mark all running processes of a workspace killed (sync).

        Called on workspace stop/remove — processes are workspace-bound
        and die with it. Pushes a frontend update per process.
        """
        try:
            workspace_uuid = uuid.UUID(str(workspace_id))
        except (ValueError, TypeError):
            return 0
        running = list(
            self.processes.list_running_by_workspace(workspace_uuid)
        )
        if not running:
            return 0
        self.processes.mark_processes_killed(
            workspace_uuid,
            status=ProcessStatus.KILLED,
        )
        for process in running:
            self._push_process_status(
                workspace_id=str(workspace_id),
                process_id=str(process.id),
                status=ProcessStatus.KILLED,
                pid=process.pid,
            )
        logger.info(
            "Marked %d processes killed for workspace %s (%s)",
            len(running),
            workspace_id,
            reason,
        )
        # Streams are workspace-bound too: fail them so harness waiters
        # surface the lifecycle event instead of hanging.
        try:
            from apps.harness.access import runner_accessor as _accessor_mod

            for accessor in list(_accessor_mod._ACCESSORS_BY_STREAM.values()):
                if str(accessor.workspace_id) == str(workspace_id):
                    accessor.fail_all_streams(
                        f"workspace {workspace_id} lifecycle: {reason}"
                    )
        except Exception:
            logger.exception(
                "Failed failing streams for workspace %s", workspace_id
            )
        return len(running)
