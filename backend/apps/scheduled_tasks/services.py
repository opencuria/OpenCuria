"""Scheduled-task orchestration. ORM access stays in repositories."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import structlog
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.harness.harness_service import HarnessService, get_harness_service
from apps.runners.enums import WorkspaceOperation, WorkspaceStatus
from apps.runners.sio_server import get_runner_service
from common.exceptions import ConflictError, NotFoundError

from .models import ScheduledTask, ScheduledTaskRun
from .recurrence import next_occurrence, valid_timezone
from .repositories import ScheduledTaskRepository

log = structlog.get_logger(__name__)


class ScheduledTaskService:
    """Validate schedules, create task runs, and coordinate harness dispatch."""

    def __init__(
        self,
        repository: type[ScheduledTaskRepository] | None = None,
        harness: HarnessService | None = None,
        runner: Any | None = None,
    ) -> None:
        self.repository = repository or ScheduledTaskRepository
        self.harness = harness
        self.runner = runner

    def _harness(self) -> HarnessService:
        return self.harness or get_harness_service()

    def _runner(self) -> Any:
        return self.runner or get_runner_service()

    def list(self, *, organization_id: uuid.UUID, owner_id: int) -> list[ScheduledTask]:
        return self.repository.list_for_owner(
            organization_id=organization_id, owner_id=owner_id
        )

    def get(
        self,
        task_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
        include_deleted: bool = False,
    ) -> ScheduledTask:
        task = self.repository.get_for_owner(
            task_id,
            organization_id=organization_id,
            owner_id=owner_id,
            include_deleted=include_deleted,
        )
        if task is None:
            raise NotFoundError("ScheduledTask", str(task_id))
        return task

    def create(
        self,
        *,
        organization_id: uuid.UUID,
        owner_id: int,
        workspace: Any,
        values: dict[str, Any],
    ) -> ScheduledTask:
        raw_prompt = values.get("prompt", "")
        raw_name = values.get("name", "")
        if not isinstance(raw_prompt, str) or not isinstance(raw_name, str):
            raise ValueError("name and prompt must be strings")
        prompt = raw_prompt.strip()
        name = raw_name.strip()
        if not prompt or not name:
            raise ValueError("name and prompt must not be empty")
        if len(name) > 255:
            raise ValueError("name must be at most 255 characters")
        if not isinstance(values.get("enabled", True), bool):
            raise ValueError("enabled must be a boolean")
        recurrence, weekdays, local_time, zone = self._validate_schedule(values)
        raw_mode = values.get("mode", "build")
        if not isinstance(raw_mode, str):
            raise ValueError("mode must be plan or build")
        mode = raw_mode.strip().lower()
        if mode not in {"plan", "build"}:
            raise ValueError("mode must be plan or build")
        now = timezone.now()
        next_at = next_occurrence(
            after=now,
            local_time=local_time,
            timezone_name=zone,
            recurrence=recurrence,
            weekdays=weekdays,
        )
        from apps.harness.providers.models_catalog import normalize_reasoning_effort

        raw_effort = values.get("reasoning_effort", "")
        raw_model = values.get("model", "")
        if not isinstance(raw_effort, str) or not isinstance(raw_model, str):
            raise ValueError("model and reasoning_effort must be strings")
        effort = normalize_reasoning_effort(raw_effort)
        model = raw_model.strip()
        skills = self._validate_skill_ids(values.get("skill_ids"))
        from apps.harness.harness_service import HarnessSession, resolve_skill_bodies

        if skills:
            resolve_skill_bodies(
                skills, user_id=owner_id, organization_id=organization_id
            )
        # Validate current model/provider availability without creating a throwaway
        # persisted chat; empty model/effort remains an agent-default reference.
        validation_session = HarnessSession(
            workspace_id=workspace.id,
            organization_id=organization_id,
            mode=mode,
            agent_name=mode,
            model=model,
            reasoning_effort=effort,
            skill_ids=skills,
        )
        self._harness().validate_provider_for_run(organization_id, validation_session)
        return self.repository.create(
            organization_id=organization_id,
            owner_id=owner_id,
            workspace=workspace,
            name=name,
            prompt=prompt,
            mode=mode,
            model=model,
            reasoning_effort=effort,
            skill_ids=skills,
            recurrence=recurrence,
            weekdays=weekdays,
            local_time=local_time,
            timezone_name=zone,
            enabled=bool(values.get("enabled", True)),
            next_run_at=next_at,
        )

    def update(self, task: ScheduledTask, values: dict[str, Any]) -> ScheduledTask:
        allowed = {
            "name",
            "prompt",
            "mode",
            "model",
            "reasoning_effort",
            "skill_ids",
            "recurrence",
            "weekdays",
            "local_time",
            "timezone_name",
            "enabled",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        combined = {key: getattr(task, key) for key in allowed}
        combined.update(changes)
        if "name" in changes:
            if not isinstance(changes["name"], str):
                raise ValueError("name must be a string")
            changes["name"] = changes["name"].strip()
            if not changes["name"]:
                raise ValueError("name must not be empty")
            if len(changes["name"]) > 255:
                raise ValueError("name must be at most 255 characters")
        if "prompt" in changes:
            if not isinstance(changes["prompt"], str):
                raise ValueError("prompt must be a string")
            changes["prompt"] = changes["prompt"].strip()
            if not changes["prompt"]:
                raise ValueError("prompt must not be empty")
        recurrence, weekdays, local_time, zone = self._validate_schedule(combined)
        changes.update(
            recurrence=recurrence,
            weekdays=weekdays,
            local_time=local_time,
            timezone_name=zone,
        )
        if "mode" in changes and not isinstance(changes["mode"], str):
            raise ValueError("mode must be plan or build")
        if "mode" in changes:
            changes["mode"] = changes["mode"].strip().lower()
            combined["mode"] = changes["mode"]
        if "mode" in changes and changes["mode"] not in {"plan", "build"}:
            raise ValueError("mode must be plan or build")
        if "model" in changes and not isinstance(changes["model"], str):
            raise ValueError("model must be a string")
        if "model" in changes:
            changes["model"] = changes["model"].strip()
            combined["model"] = changes["model"]
        if "reasoning_effort" in changes and not isinstance(
            changes["reasoning_effort"], str
        ):
            raise ValueError("reasoning_effort must be a string")
        if "reasoning_effort" in changes:
            from apps.harness.providers.models_catalog import normalize_reasoning_effort

            changes["reasoning_effort"] = normalize_reasoning_effort(
                changes["reasoning_effort"]
            )
            combined["reasoning_effort"] = changes["reasoning_effort"]
        if {"mode", "model", "reasoning_effort"} & changes.keys():
            from apps.harness.harness_service import HarnessSession

            validation_session = HarnessSession(
                workspace_id=task.workspace_id,
                organization_id=task.organization_id,
                mode=combined.get("mode", task.mode),
                agent_name=combined.get("mode", task.mode),
                model=combined.get("model", task.model) or "",
                reasoning_effort=combined.get("reasoning_effort", task.reasoning_effort)
                or "",
                skill_ids=combined.get("skill_ids", task.skill_ids) or [],
            )
            self._harness().validate_provider_for_run(
                task.organization_id, validation_session
            )
        if "skill_ids" in changes:
            changes["skill_ids"] = self._validate_skill_ids(changes["skill_ids"])
            if changes["skill_ids"]:
                from apps.harness.harness_service import resolve_skill_bodies

                resolve_skill_bodies(
                    changes["skill_ids"],
                    user_id=task.owner_id,
                    organization_id=task.organization_id,
                )
        if "enabled" in changes:
            if not isinstance(changes["enabled"], bool):
                raise ValueError("enabled must be a boolean")
        cadence_changed = any(
            combined[field] != getattr(task, field)
            for field in ("recurrence", "weekdays", "local_time", "timezone_name")
        )
        resumed = not task.enabled and combined.get("enabled", True)
        changes["next_run_at"] = (
            next_occurrence(
                after=timezone.now(),
                local_time=local_time,
                timezone_name=zone,
                recurrence=recurrence,
                weekdays=weekdays,
            )
            if combined.get("enabled", True) and (cadence_changed or resumed)
            else task.next_run_at
        )
        return self.repository.update(task, **changes)

    def delete(self, task: ScheduledTask) -> None:
        self.repository.delete(task)

    def list_runs(self, task: ScheduledTask) -> list[ScheduledTaskRun]:
        """Repair completed history for this owned task before returning it."""
        for run in self.repository.active_runs(task_id=task.id):
            self._finish_completed_run(run)
        return self.repository.list_runs(task)

    def _finish_completed_run(self, run: ScheduledTaskRun) -> bool:
        """Project the exact assistant outcome without overwriting terminal runs."""
        message = run.assistant_message
        if message is None or message.completed_at is None:
            return False
        status = (
            ScheduledTaskRun.Status.ERROR
            if message.error or message.finish == "error"
            else ScheduledTaskRun.Status.SUCCEEDED
        )
        return self.repository.finish_run(
            run,
            status=status,
            finished_at=message.completed_at,
            error=message.error or "",
        )

    def complete_assistant_run(self, message_id: uuid.UUID) -> None:
        """Settle linked task runs directly when their harness assistant finishes."""
        for run in self.repository.active_runs(message_id=message_id):
            self._finish_completed_run(run)

    @staticmethod
    def _validate_skill_ids(value: Any) -> list[str]:
        if value in (None, []):
            return []
        if not isinstance(value, list) or any(
            not isinstance(skill_id, str) or not skill_id.strip() for skill_id in value
        ):
            raise ValueError("skill_ids must be a list of non-empty UUID strings")
        normalized = list(dict.fromkeys(skill_id.strip() for skill_id in value))
        for skill_id in normalized:
            try:
                uuid.UUID(skill_id)
            except ValueError as exc:
                raise ValueError(f"Invalid skill_id: {skill_id}") from exc
        return normalized

    async def run_now(self, task: ScheduledTask) -> ScheduledTaskRun:
        create_run = getattr(
            self.repository, "create_manual_run", self.repository.create_run
        )
        run = await sync_to_async(create_run)(task, timezone.now())
        if run.status == ScheduledTaskRun.Status.SKIPPED:
            return run
        try:
            return await self._launch(
                task, scheduled_for=run.scheduled_for, manual=True, ledger=run
            )
        except asyncio.CancelledError:
            await self._interrupt_if_claimed(run)
            raise
        except ConflictError as exc:
            return await self._skip_admission_conflict(task, run, exc)
        except Exception as exc:
            log.exception("scheduled_task_manual_run_failed", task_id=str(task.id))
            return await sync_to_async(self.repository.update_run)(
                run,
                status=ScheduledTaskRun.Status.ERROR,
                finished_at=timezone.now(),
                error=str(exc),
            )

    async def dispatch_due(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: int = 60,
        on_claim=None,
    ) -> None:
        """Dispatch occurrences, skipping stale due occurrences without replay."""
        current = now or timezone.now()
        due = await sync_to_async(self.repository.due)(current)
        for task in due:
            scheduled_for = task.next_run_at
            following = next_occurrence(
                after=scheduled_for,
                local_time=task.local_time,
                timezone_name=task.timezone_name,
                recurrence=task.recurrence,
                weekdays=task.weekdays,
            )
            if current - scheduled_for > timedelta(seconds=grace_seconds):
                following = next_occurrence(
                    after=current,
                    local_time=task.local_time,
                    timezone_name=task.timezone_name,
                    recurrence=task.recurrence,
                    weekdays=task.weekdays,
                )
                await sync_to_async(self.repository.mark_missed)(
                    task, scheduled_for=scheduled_for, next_run_at=following
                )
                continue
            claim = await sync_to_async(self.repository.claim)(
                task, scheduled_for=scheduled_for, next_run_at=following
            )
            if claim is None:
                continue
            if claim.status == ScheduledTaskRun.Status.SKIPPED:
                continue
            if on_claim is not None:
                on_claim(task, scheduled_for, claim)
                continue
            await self._dispatch_claimed(task, scheduled_for, ledger=claim)

    async def _dispatch_claimed(
        self,
        task: ScheduledTask,
        scheduled_for: datetime,
        *,
        ledger: ScheduledTaskRun | None = None,
    ) -> None:
        claim = ledger
        if claim is None:
            claim = await sync_to_async(
                lambda: self.repository.runs_for_occurrence(task.id, scheduled_for)
            )()
        try:
            await self._launch(task, scheduled_for=scheduled_for, ledger=claim)
        except asyncio.CancelledError:
            await self._interrupt_if_claimed(claim)
            raise
        except ConflictError as exc:
            await self._skip_admission_conflict(task, claim, exc)
        except Exception as exc:
            log.exception("scheduled_task_dispatch_failed", task_id=str(task.id))
            await sync_to_async(self.repository.update_run)(
                claim,
                status=ScheduledTaskRun.Status.ERROR,
                finished_at=timezone.now(),
                error=str(exc),
            )

    async def _cleanup_unstarted_session(
        self,
        service: HarnessService,
        session: Any,
        run: ScheduledTaskRun,
        task: ScheduledTask,
    ) -> None:
        """Delete an empty scheduled chat when admission failed before a run."""
        try:
            deleted = await sync_to_async(self.repository.delete_unstarted_session)(
                run.id,
                session.id,
                task_id=task.id,
                workspace_id=task.workspace_id,
                organization_id=task.organization_id,
            )
            if deleted:
                run.session = None
                await service._emit_conversations_changed(task.workspace_id)
        except Exception:
            log.exception(
                "scheduled_task_unstarted_session_cleanup_failed",
                task_id=str(task.id),
                run_id=str(run.id),
            )

    async def _interrupt_if_claimed(self, run: ScheduledTaskRun) -> None:
        """Close a cancelled launch claim without making it eligible for retry."""
        if run is None or run.status != ScheduledTaskRun.Status.CLAIMED:
            return
        await asyncio.shield(
            sync_to_async(self.repository.update_run)(
                run,
                status=ScheduledTaskRun.Status.INTERRUPTED,
                reason="scheduler_stopped",
                finished_at=timezone.now(),
                error="Launch interrupted before the assistant run started",
            )
        )

    async def skip_due_on_startup(self, *, now: datetime | None = None) -> None:
        """Advance overdue schedules at boot without replaying downtime work."""
        current = now or timezone.now()
        due = await sync_to_async(self.repository.due)(current)
        for task in due:
            next_at = next_occurrence(
                after=current,
                local_time=task.local_time,
                timezone_name=task.timezone_name,
                recurrence=task.recurrence,
                weekdays=task.weekdays,
            )
            await sync_to_async(self.repository.mark_missed)(
                task, scheduled_for=task.next_run_at, next_run_at=next_at
            )

    async def reconcile_runs(self) -> None:
        """Project assistant completion onto history and apply idle auto-stop."""
        rows = await sync_to_async(self.repository.active_runs)()
        for run in rows:
            await sync_to_async(self._finish_completed_run)(run)
        pending = await sync_to_async(self.repository.pending_completion_checks)()
        for run in pending:
            workspace_id = (
                run.scheduled_task.workspace_id
                if run.scheduled_task is not None
                else run.session.workspace_id if run.session is not None else None
            )
            if workspace_id is not None:
                await self._maybe_stop_workspace(workspace_id)
            await sync_to_async(self.repository.clear_completion_check)(run.id)

    async def _launch(
        self,
        task: ScheduledTask,
        *,
        scheduled_for: datetime,
        manual: bool = False,
        ledger: ScheduledTaskRun | None = None,
    ) -> ScheduledTaskRun:
        run = ledger or await sync_to_async(self.repository.create_run)(
            task, scheduled_for
        )
        task = self._task_for_run(task, run)
        (
            status,
            active_operation,
            runner_status,
            owner_is_active,
            current_task_id,
        ) = await sync_to_async(self.repository.workspace_resume_state)(
            task.workspace_id
        )
        owned = await sync_to_async(self.repository.workspace_is_owned)(
            task.workspace_id,
            organization_id=task.organization_id,
            owner_id=task.owner_id,
        )
        if status is None or not owned or not owner_is_active:
            return await self._skip(
                run, "workspace_unavailable", "Workspace is no longer available"
            )
        if current_task_id and not active_operation:
            return await self._skip(
                run,
                "workspace_lifecycle_unresolved",
                "Workspace lifecycle outcome unresolved; intervention required",
            )
        if status == WorkspaceStatus.RUNNING:
            if runner_status != "online" or active_operation:
                return await self._skip(
                    run,
                    "workspace_operation_active",
                    "Workspace runner is offline or a lifecycle operation is active",
                )
        elif status == WorkspaceStatus.STOPPED:
            if active_operation == WorkspaceOperation.STARTING:
                if not await self._wait_for_workspace_running(task.workspace_id):
                    return await self._skip(
                        run,
                        "workspace_resume_failed",
                        "Workspace did not finish resuming before timeout",
                    )
            elif active_operation or runner_status != "online":
                return await self._skip(
                    run,
                    "workspace_resume_failed",
                    "Workspace operation is active or its runner is offline",
                )
            else:
                try:
                    await self._runner().resume_workspace(task.workspace_id)
                    if not await self._wait_for_workspace_running(task.workspace_id):
                        return await self._skip(
                            run,
                            "workspace_resume_failed",
                            "Workspace did not finish resuming before timeout",
                        )
                except ConflictError as exc:
                    return await self._skip_admission_conflict(task, run, exc)
                except Exception as exc:
                    return await self._skip(run, "workspace_resume_failed", str(exc))
        elif status == "resuming":
            if runner_status != "online" or active_operation not in {
                None,
                WorkspaceOperation.STARTING,
            }:
                return await self._skip(
                    run,
                    "workspace_resume_failed",
                    "Workspace runner is offline or another operation is active",
                )
            if not await self._wait_for_workspace_running(task.workspace_id):
                return await self._skip(
                    run,
                    "workspace_resume_failed",
                    "Workspace did not finish resuming before timeout",
                )
        elif status != WorkspaceStatus.RUNNING:
            return await self._skip(
                run, "workspace_not_running", f"Workspace is {status}"
            )
        (
            status,
            active_operation,
            runner_status,
            owner_is_active,
            current_task_id,
        ) = await sync_to_async(self.repository.workspace_resume_state)(
            task.workspace_id
        )
        owned = await sync_to_async(self.repository.workspace_is_owned)(
            task.workspace_id,
            organization_id=task.organization_id,
            owner_id=task.owner_id,
        )
        if not owner_is_active or not owned:
            return await self._skip(
                run, "workspace_unavailable", "Workspace is no longer available"
            )
        if current_task_id and not active_operation:
            return await self._skip(
                run,
                "workspace_lifecycle_unresolved",
                "Workspace lifecycle outcome unresolved; intervention required",
            )
        if (
            status != WorkspaceStatus.RUNNING
            or runner_status != "online"
            or active_operation
        ):
            return await self._skip(
                run,
                "workspace_operation_active",
                "Workspace is not running cleanly or a lifecycle operation is active",
            )
        launch = asyncio.create_task(
            self._create_and_start_run(task, run, self._harness())
        )
        try:
            return await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Let the indivisible admission step finish: either the claim is
            # associated with the already-running assistant, or its new empty
            # session is deleted. Never leave an untracked launch behind.
            try:
                await asyncio.shield(launch)
            except Exception:
                log.exception(
                    "scheduled_task_launch_cancelled_during_admission",
                    task_id=str(task.id),
                    run_id=str(run.id),
                )
            raise

    @staticmethod
    def _task_for_run(task: ScheduledTask, run: ScheduledTaskRun) -> Any:
        """Use the durable run snapshot, falling back for legacy ledger rows."""
        snapshot = getattr(run, "configuration_snapshot", None) or {}
        if not snapshot:
            return task
        values = {
            "id": task.id,
            "workspace_id": uuid.UUID(
                snapshot.get("workspace_id", str(task.workspace_id))
            ),
            "organization_id": uuid.UUID(
                snapshot.get("organization_id", str(task.organization_id))
            ),
            "owner_id": snapshot.get("owner_id", task.owner_id),
            "prompt": snapshot.get("prompt", task.prompt),
            "name": snapshot.get("name", task.name),
            "mode": snapshot.get("mode", task.mode),
            "model": snapshot.get("model", task.model),
            "reasoning_effort": snapshot.get(
                "reasoning_effort", task.reasoning_effort
            ),
            "skill_ids": snapshot.get("skill_ids", list(task.skill_ids or [])),
        }
        return SimpleNamespace(**values)

    async def _create_and_start_run(
        self, task: ScheduledTask, run: ScheduledTaskRun, service: HarnessService
    ) -> ScheduledTaskRun:
        """Create, persist, and admit a chat before exposing cancellation."""
        session = await sync_to_async(service.create_session)(
            workspace_id=task.workspace_id,
            organization_id=task.organization_id,
            prompt=task.prompt,
            mode=task.mode,
            model=task.model or "",
            reasoning_effort=task.reasoning_effort or "",
            skill_ids=list(task.skill_ids or []),
            user_id=task.owner_id,
        )
        run = await sync_to_async(self.repository.update_run)(run, session=session)
        try:
            assistant = await service.start_run(
                session,
                task.prompt,
                organization_id=task.organization_id,
                workspace_id=str(task.workspace_id),
                user_id=task.owner_id,
                skill_ids=list(task.skill_ids or []),
                scheduled=True,
            )
        except BaseException:
            if run.status == ScheduledTaskRun.Status.CLAIMED:
                await self._cleanup_unstarted_session(service, session, run, task)
            raise
        run = await sync_to_async(self.repository.update_run)(
            run,
            session=session,
            assistant_message=assistant,
            status=ScheduledTaskRun.Status.RUNNING,
            started_at=timezone.now(),
        )
        # The background assistant can finish before its ledger link is saved.
        # Re-read persisted completion instead of trusting start_run's instance.
        try:
            await sync_to_async(self.complete_assistant_run)(assistant.id)
        except Exception:
            log.exception(
                "scheduled_task_completion_failed", run_id=str(run.id)
            )
        try:
            await sync_to_async(self.repository.touch_workspace_activity)(
                task.workspace_id
            )
        except Exception:
            log.exception(
                "scheduled_task_workspace_activity_touch_failed",
                task_id=str(task.id),
                run_id=str(run.id),
            )
        return run

    async def _wait_for_workspace_running(
        self, workspace_id: uuid.UUID, *, timeout_seconds: int = 120
    ) -> bool:
        """Wait for the runner lifecycle event to confirm workspace resume."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            status = await sync_to_async(self.repository.workspace_status)(workspace_id)
            if status == WorkspaceStatus.RUNNING:
                return True
            if status not in {
                WorkspaceStatus.CREATING,
                WorkspaceStatus.STOPPED,
                "resuming",
            }:
                active_operation = await sync_to_async(
                    self.repository.workspace_resume_state
                )(workspace_id)
                if (
                    status == WorkspaceStatus.STOPPED
                    and active_operation[1] == WorkspaceOperation.STARTING
                ):
                    await asyncio.sleep(1)
                    continue
                return False
            await asyncio.sleep(1)
        return False

    async def _skip_admission_conflict(
        self, task: ScheduledTask, run: ScheduledTaskRun, exc: ConflictError
    ) -> ScheduledTaskRun:
        """Classify admission races from persisted lifecycle state, not error text."""
        task = self._task_for_run(task, run)
        (
            status,
            operation,
            runner_status,
            owner_active,
            current_task_id,
        ) = await sync_to_async(self.repository.workspace_resume_state)(
            task.workspace_id
        )
        if status is None or not owner_active:
            reason = "workspace_unavailable"
        elif current_task_id and not operation:
            reason = "workspace_lifecycle_unresolved"
        elif operation or runner_status != "online":
            reason = "workspace_operation_active"
        elif status != WorkspaceStatus.RUNNING:
            reason = "workspace_not_running" if status else "workspace_unavailable"
        else:
            reason = "other_chat_active"
        return await self._skip(run, reason, str(exc))

    async def _skip(
        self, run: ScheduledTaskRun, reason: str, error: str
    ) -> ScheduledTaskRun:
        return await sync_to_async(self.repository.update_run)(
            run,
            status=ScheduledTaskRun.Status.SKIPPED,
            reason=reason,
            error=error,
            finished_at=timezone.now(),
        )

    async def _maybe_stop_workspace(self, workspace_id: uuid.UUID) -> None:
        """Stop only when configured and no harness sessions are busy."""
        # Respect org auto-stop policy but never stop another active chat.
        workspace_details = await sync_to_async(
            self.repository.workspace_autostop_settings
        )(workspace_id)
        if workspace_details is None or workspace_details[0] != WorkspaceStatus.RUNNING:
            return
        _, has_active_session, timeout, idle_since = workspace_details
        if timeout is None or timeout <= 0 or has_active_session:
            return
        active = await sync_to_async(self.repository.has_busy_harness_session)(
            workspace_id
        )
        if active:
            return
        if idle_since and timezone.now() - idle_since >= timedelta(minutes=timeout):
            try:
                await self._runner().stop_workspace(workspace_id, auto_stop=True)
            except ConflictError as exc:
                # A chat or lifecycle operation may have won admission since
                # the reconciliation snapshot. Treat this as a normal no-op.
                log.info(
                    "scheduled_task_auto_stop_skipped",
                    workspace_id=str(workspace_id),
                    reason=str(exc),
                )

    @staticmethod
    def _validate_schedule(values: dict[str, Any]) -> tuple[str, list[int], Any, str]:
        from datetime import time as dt_time

        recurrence = values.get("recurrence", "daily")
        if not isinstance(recurrence, str):
            raise ValueError("recurrence must be daily or weekly")
        if recurrence not in {"daily", "weekly"}:
            raise ValueError("recurrence must be daily or weekly")
        raw_weekdays = values.get("weekdays")
        if raw_weekdays is None:
            raw_weekdays = []
        if not isinstance(raw_weekdays, (list, tuple)):
            raise ValueError("weekdays must be a list of integers")
        if any(isinstance(day, bool) or not isinstance(day, int) for day in raw_weekdays):
            raise ValueError("weekdays must contain integers from 0 (Mon) to 6 (Sun)")
        weekdays = list(raw_weekdays)
        if recurrence == "daily":
            weekdays = []
        elif not weekdays or any(day not in range(7) for day in weekdays):
            raise ValueError(
                "weekly recurrence requires weekdays from 0 (Mon) to 6 (Sun)"
            )
        raw_time = values.get("local_time", "09:00")
        if isinstance(raw_time, str):
            if re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", raw_time) is None:
                raise ValueError("local_time must be HH:MM")
            try:
                local_time = dt_time.fromisoformat(raw_time)
            except ValueError as exc:
                raise ValueError("local_time must be HH:MM") from exc
        else:
            local_time = raw_time
        if not isinstance(local_time, dt_time):
            raise ValueError("local_time must be HH:MM")
        if local_time.second or local_time.microsecond or local_time.tzinfo:
            raise ValueError("local_time must be HH:MM without seconds or timezone")
        zone = values.get("timezone_name", "UTC")
        if not isinstance(zone, str) or not zone.strip() or len(zone) > 64:
            raise ValueError("timezone_name must be a valid IANA timezone")
        zone = zone.strip()
        valid_timezone(zone)
        return recurrence, sorted(set(weekdays)), local_time, zone


class ScheduledTaskScheduler:
    """Single asyncio-loop scheduler started only by ASGI lifespan."""

    def __init__(
        self, service: ScheduledTaskService | None = None, *, interval: float = 5.0
    ) -> None:
        self.service = service or ScheduledTaskService()
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._lease_task: asyncio.Task[None] | None = None
        self._launch_tasks: set[asyncio.Task[None]] = set()
        self._lease = None
        self._started = False

    async def start(self) -> None:
        """Acquire process ownership before recovery and start one scheduler."""
        if self._started:
            return
        from .lease import ScheduledTaskLease

        lease = ScheduledTaskLease()
        if not await lease.acquire():
            await lease.release()
            raise RuntimeError(
                "Another backend process owns the scheduled-task scheduler; "
                "run exactly one backend ASGI worker"
            )
        self._lease = lease
        try:
            await sync_to_async(self.service.repository.recover_backend_restart)()
            await self.service.skip_due_on_startup()
        except BaseException:
            try:
                await asyncio.shield(lease.release())
            finally:
                self._lease = None
            raise
        self._started = True
        self._lease_task = asyncio.create_task(
            self._renew_lease(), name="scheduled-task-lease-renewal"
        )
        self._task = asyncio.create_task(self._run(), name="scheduled-task-scheduler")

    async def stop(self) -> None:
        """Stop supervised work and release ownership even on teardown errors."""
        for task in (self._task, self._lease_task):
            if task is not None:
                task.cancel()
        try:
            pending = [task for task in (self._task, self._lease_task) if task]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for launch in self._launch_tasks:
                launch.cancel()
            if self._launch_tasks:
                await asyncio.gather(*self._launch_tasks, return_exceptions=True)
        finally:
            if self._lease is not None:
                await asyncio.shield(self._lease.release())
            self._lease = None
            self._task = None
            self._lease_task = None
            self._started = False

    async def _renew_lease(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                held = self._lease is not None and await self._lease.renew()
            except Exception:
                log.exception("scheduled_task_lease_renewal_failed")
                held = False
            if not held:
                log.critical("scheduled_task_lease_lost")
                stopping = list(self._launch_tasks)
                if self._task is not None:
                    self._task.cancel()
                    stopping.append(self._task)
                for launch in stopping:
                    launch.cancel()
                if stopping:
                    await asyncio.gather(*stopping, return_exceptions=True)
                if self._lease is not None:
                    await asyncio.shield(self._lease.release())
                self._lease = None
                self._task = None
                self._lease_task = None
                self._started = False
                return

    def _supervise_launch(
        self,
        task: ScheduledTask,
        scheduled_for: datetime,
        ledger: ScheduledTaskRun,
    ) -> None:
        if len(self._launch_tasks) >= 10:
            launch = asyncio.create_task(
                sync_to_async(self.service.repository.update_run)(
                    ledger,
                    status=ScheduledTaskRun.Status.SKIPPED,
                    reason="scheduler_at_capacity",
                    finished_at=timezone.now(),
                    error="Too many scheduled launches are already in progress",
                ),
                name=f"scheduled-task-capacity-skip-{task.id}",
            )
            self._launch_tasks.add(launch)
            launch.add_done_callback(self._launch_tasks.discard)
            return
        launch = asyncio.create_task(
            self.service._dispatch_claimed(task, scheduled_for, ledger=ledger),
            name=f"scheduled-task-launch-{task.id}",
        )
        self._launch_tasks.add(launch)
        launch.add_done_callback(self._launch_tasks.discard)

    async def _run(self) -> None:
        while True:
            try:
                await self.service.reconcile_runs()
                await self.service.dispatch_due(on_claim=self._supervise_launch)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduled_task_scheduler_iteration_failed")
            await asyncio.sleep(self.interval)
