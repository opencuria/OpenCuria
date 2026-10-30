"""Operator workflow shared by REST and automatic reconciliation."""

from __future__ import annotations

import uuid
from typing import Any

from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from ..disposition_repository import DispositionRepository, DispositionState
from .operation_results import apply_result

#: API-key scope a retry needs, per retryable lifecycle task type.
RETRY_PERMISSIONS = {
    "stop_workspace": APIKeyPermission.WORKSPACES_STOP,
    "resume_workspace": APIKeyPermission.WORKSPACES_RESUME,
    "remove_workspace": APIKeyPermission.WORKSPACES_DELETE,
}


class DispositionService:
    """Inspect is non-destructive, including offline; disposition requires proof."""

    @staticmethod
    def authorized(
        user: Any,
        organization_id: uuid.UUID,
        operation_id: uuid.UUID,
        *,
        admin: bool = False,
    ) -> DispositionState:
        """Owners inspect workspace operations; org admins inspect every operation."""
        organizations = OrganizationService()
        organizations.require_membership(user, organization_id)
        state = DispositionRepository.get(organization_id, operation_id)
        if state is None:
            raise NotFoundError("Operation", str(operation_id))
        if admin or not state.workspace_id or state.workspace_owner_id != user.id:
            organizations.require_admin(user, organization_id)
        return state

    @staticmethod
    def _summary(state: DispositionState) -> dict:
        """Hide recipes, repository URLs and credential associations."""
        return {
            "operation_id": str(state.operation_id),
            "task_type": state.task_type,
            "status": state.status,
            "phase": state.phase,
            "runner_id": str(state.runner.id),
            "target": state.identity["target"],
            "attempt": state.identity["attempt"],
            "diagnostic": state.diagnostic,
            "intervention_required": state.phase == "intervention",
        }

    @staticmethod
    def permitted_actions(
        state: DispositionState, evidence: dict, user: Any, organization_id: uuid.UUID
    ) -> list[str]:
        """Presentation-only eligibility from current inspection; POST revalidates."""
        if state.status == "completed" or state.phase == "disposed":
            return []
        terminal = evidence.get("status") == "terminal"
        known = evidence.get("outcome_known")
        finished = evidence.get("execution_finished")
        actions = ["reconcile"] if terminal and known and finished else []
        identity = evidence.get("identity", {})
        exact = all(identity.get(k) == v for k, v in state.identity.items())
        safe = (
            exact
            and finished
            and evidence.get("instance_id")
            and evidence.get("quiescent")
        )
        if (
            safe
            and terminal
            and known
            and evidence.get("event") == "workspace:error"
            and state.task_type in RETRY_PERMISSIONS
            and state.retry_count < 2
        ):
            actions.append("retry")
        if safe and not known and evidence.get("status") in {"terminal", "unknown"}:
            try:
                OrganizationService().require_admin(user, organization_id)
                actions.append("acknowledge_interrupted")
            except AuthenticationError:
                pass
        return actions

    @staticmethod
    def _dispose(state: DispositionState, evidence: dict, action: str) -> dict:
        """Revalidate a preauthorized disposition and apply it atomically."""
        if (
            not evidence.get("instance_id")
            or not evidence.get("execution_finished")
            or not evidence.get("quiescent")
            or evidence.get("status") not in {"terminal", "unknown"}
        ):
            raise ConflictError(
                "No fresh proof of finished execution and complete resource scan"
            )
        identity = evidence.get("identity", {})
        if any(identity.get(k) != v for k, v in state.identity.items()):
            raise ConflictError("Runner evidence identity mismatch")
        if action == "acknowledge_interrupted" and evidence.get("outcome_known"):
            raise ConflictError(
                "Known outcome must be reconciled, not acknowledged as interrupted"
            )
        with DispositionRepository.locked(state) as repository:
            current = repository.state()
            if current.runner.sid != state.runner.sid or not current.runner.is_online:
                raise ConflictError("Runner session changed during inspection")
            if current.workspace_id and current.current_task_id not in {
                None,
                current.operation_id,
            }:
                raise ConflictError("Another operation owns the workspace")
            if action == "retry" and current.retry_count >= 2:
                raise ConflictError("Safe retry limit reached")
            if action == "retry" and (
                evidence.get("status") != "terminal"
                or current.task_type not in RETRY_PERMISSIONS
                or not evidence.get("outcome_known")
                or evidence.get("event") != "workspace:error"
            ):
                raise ConflictError(
                    "Only proven finished failed start/stop/remove may retry"
                )
            if action not in {"acknowledge_interrupted", "retry"}:
                raise ConflictError("Unsupported disposition")
            if current.status == "completed" or current.phase == "disposed":
                raise ConflictError("Operation already resolved")
            repository.fail_task(
                "Interrupted execution acknowledged after fresh runtime scan; "
                "no resources deleted or recreated"
            )
            if action == "acknowledge_interrupted":
                repository.interrupt_children(
                    capture_diagnostic=(
                        "Interrupted child acknowledged; resources preserved, "
                        "no automatic restart"
                    ),
                    recreate_diagnostic=(
                        "Interrupted reset acknowledged; retry the reset or "
                        "delete the workspace"
                    ),
                )
            workspace_status = current.workspace_status
            if current.workspace_id:
                if current.task_type in {
                    "create_workspace",
                    "create_workspace_from_image_artifact",
                }:
                    workspace_status = "failed"
                elif current.task_type == "remove_workspace":
                    workspace_status = (
                        "failed"
                        if repository.has_recreate_request()
                        else "delete_failed"
                    )
            repository.release(workspace_status)
            if action == "retry":
                new_id = repository.create_retry(current.retry_count + 1)
                return {
                    "operation_id": str(new_id),
                    "previous_operation_id": str(current.operation_id),
                }
            return {
                "operation_id": str(current.operation_id),
                "released": True,
                "resources_preserved": True,
            }

    @staticmethod
    def retry_permission(
        user: Any, organization_id: uuid.UUID, operation_id: uuid.UUID
    ) -> APIKeyPermission | None:
        """Return the API-key scope retrying the operation needs, if retryable."""
        row = DispositionService.authorized(user, organization_id, operation_id)
        return RETRY_PERMISSIONS.get(row.task_type)

    async def inspect(
        self,
        transport: Any,
        user: Any,
        organization_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> dict:
        """Request journal status and a fresh scan on the current authenticated SID."""
        row = await sync_to_async(DispositionService.authorized)(
            user, organization_id, operation_id
        )
        result = self._summary(row)
        result["evidence"] = {
            "status": "unknown",
            "execution_finished": False,
            "diagnostic": "Runner offline or unavailable; actions disabled",
        }
        result["permitted_actions"] = []
        result["review_message"] = (
            "The runner is unavailable. Restore its connection, then check again."
        )
        if not row.runner.is_online or not row.runner.sid:
            return result
        try:
            evidence = await transport._call_runner(
                row.runner,
                "operation:inspect",
                row.identity,
                timeout=60,
            )
            # Recheck connection binding after RPC: old SID responses cannot resolve.
            current = await sync_to_async(DispositionService.authorized)(
                user, organization_id, operation_id
            )
            if current.runner.sid != row.runner.sid:
                return result
            result["evidence"] = evidence
            result["permitted_actions"] = await sync_to_async(self.permitted_actions)(
                current, evidence, user, organization_id
            )
            result["review_message"] = self.review_message(
                evidence, result["permitted_actions"]
            )
        except Exception:
            return result
        return result

    @staticmethod
    def review_message(evidence: dict, actions: list[str]) -> str:
        """Describe the permitted next step while keeping proof details separate."""
        if evidence.get("status") == "running":
            return "This operation is still running. Wait, then check again."
        terminal_result = evidence.get("result")
        error = (
            terminal_result.get("error", "")
            if isinstance(terminal_result, dict)
            else ""
        )
        error = error if isinstance(error, str) else ""
        if "SSH" in error:
            message = (
                "The workspace is not reachable. Restore its connection before "
                "trying again."
            )
            if "acknowledge_interrupted" in actions:
                message += (
                    " Closing the interrupted operation only releases the block "
                    "and keeps existing resources."
                )
            return message
        if not actions:
            return (
                "The runner could not verify that this operation can be closed. "
                "Check its connection and see Details."
            )
        if "acknowledge_interrupted" in actions:
            return (
                "The operation has ended, but its result is uncertain. "
                "You can close it and keep existing resources."
            )
        if "retry" in actions:
            return (
                "The runner confirmed that the operation failed. "
                "Apply its result or try again."
            )
        return (
            "The runner confirmed the result. Apply it to update the workspace status."
        )

    async def act(
        self,
        transport: Any,
        user: Any,
        organization_id: uuid.UUID,
        operation_id: uuid.UUID,
        action: str,
    ) -> dict:
        """Explicit reconciliation/retry/acknowledgment; never rerun create/build."""
        row = await sync_to_async(DispositionService.authorized)(
            user,
            organization_id,
            operation_id,
            admin=action == "acknowledge_interrupted",
        )
        inspected = await self.inspect(transport, user, organization_id, operation_id)
        evidence = inspected["evidence"]
        if (
            action == "acknowledge_interrupted"
            and evidence.get("diagnostic_code") == "identity_not_recorded"
        ):
            evidence = await transport._call_runner(
                row.runner,
                "operation:seal",
                row.identity,
                timeout=60,
            )
            if evidence.get("status") != "terminal":
                raise ConflictError("Runner could not seal delayed delivery identity")
        current = await sync_to_async(DispositionService.authorized)(
            user, organization_id, operation_id
        )
        if current.runner.sid != row.runner.sid or not current.runner.is_online:
            raise ConflictError("Runner session changed during inspection")
        if action == "reconcile":
            if (
                evidence.get("status") != "terminal"
                or not evidence.get("outcome_known")
                or not evidence.get("execution_finished")
            ):
                raise ConflictError("Exact terminal journal outcome unavailable")
            accepted = await sync_to_async(apply_result)(
                transport,
                str(row.runner.id),
                evidence["event"],
                evidence["result"],
            )
            if not accepted:
                raise ConflictError("Journal result could not resolve this operation")
            return {"operation_id": str(operation_id), "reconciled": True}
        return await sync_to_async(self._dispose)(row, evidence, action)
