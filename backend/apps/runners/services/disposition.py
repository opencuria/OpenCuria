"""Operator workflow shared by REST and automatic reconciliation."""

from __future__ import annotations

import uuid
from typing import Any

from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, ConflictError, NotFoundError

from ..disposition_repository import DispositionRepository, DispositionState
from ..operations import OperationRepository
from .operation_results import apply_result

#: API-key scope a retry needs, per retryable lifecycle task type.
RETRY_PERMISSIONS = {
    "stop_workspace": APIKeyPermission.WORKSPACES_STOP,
    "resume_workspace": APIKeyPermission.WORKSPACES_RESUME,
    "remove_workspace": APIKeyPermission.WORKSPACES_DELETE,
}
RECOVERY_BLOCKED_CODES = {"publication_ownership_conflict"}


class DispositionService:
    """Inspect is non-destructive, including offline; disposition requires proof."""

    @staticmethod
    def recover_inspection(
        service: Any, runner_id: str, sid: str, evidence: dict
    ) -> bool:
        """Resolve sealed interrupted attempts automatically, without replaying work.

        Unknown execution outcomes remain failed history. A fresh, complete
        runtime observation only releases the reservation after the runner has
        fenced the exact identity against delayed delivery and ongoing effects.
        """
        identity = evidence.get("identity")
        if not isinstance(identity, dict):
            return False
        row = DispositionRepository.for_runner(
            runner_id, identity.get("operation_id", "")
        )
        if (
            row is None
            or row.runner.sid != sid
            or not row.runner.is_online
            or any(identity.get(k) != v for k, v in row.identity.items())
            or not evidence.get("instance_id")
        ):
            return False
        blocked_code = evidence.get("recovery_blocked_code")
        if blocked_code is not None:
            diagnostic = evidence.get("diagnostic")
            if (
                not isinstance(blocked_code, str)
                or blocked_code not in RECOVERY_BLOCKED_CODES
                or row.task_type not in {"build_image", "create_image_artifact"}
                or not isinstance(diagnostic, str)
                or not diagnostic.strip()
                or not isinstance(evidence.get("instance_id"), str)
                or evidence.get("status") != "unknown"
                or evidence.get("execution_finished") is not False
                or evidence.get("quiescent") is not False
            ):
                return False
            diagnostic = (
                f"Automatic recovery blocked ({blocked_code}): "
                + diagnostic.strip()[:1000]
            )
            changed = OperationRepository.require_attention(
                runner_id, sid, identity, diagnostic
            )
            if changed and row.workspace_id:
                workspace_id = str(row.workspace_id)
                service._forward_workspace_operation(workspace_id, None)
                service._forward_to_frontend(
                    "workspace:error",
                    {"workspace_id": workspace_id, "error": diagnostic},
                    workspace_id,
                )
            return changed
        if (
            evidence.get("status") != "terminal"
            or evidence.get("execution_finished") is not True
        ):
            return False
        if evidence.get("outcome_known") is True:
            with DispositionRepository.locked(row) as repository:
                current = repository.state()
                if current.runner.sid != sid or not current.runner.is_online:
                    return False
                return apply_result(
                    service,
                    runner_id,
                    evidence.get("event", ""),
                    evidence.get("result", {}),
                )
        if (
            evidence.get("outcome_known") is not False
            or evidence.get("quiescent") is not True
        ):
            return False
        try:
            DispositionService._dispose(row, evidence, "recover_interrupted")
        except ConflictError:
            return False
        if row.workspace_id:
            workspace_id = str(row.workspace_id)
            service._forward_workspace_operation(workspace_id, None)
            workspace = service.workspaces.get_by_id(row.workspace_id)
            if workspace is not None:
                service._forward_workspace_status(workspace)
        return True

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
            "intervention_required": state.phase
            in {"intervention", "attention_required"},
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
        if action in {
            "acknowledge_interrupted",
            "recover_interrupted",
        } and evidence.get("outcome_known"):
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
            if (
                action == "recover_interrupted"
                and current.workspace_id
                and current.current_task_id != current.operation_id
            ):
                raise ConflictError(
                    "The interrupted operation no longer owns the workspace"
                )
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
            if action not in {
                "acknowledge_interrupted",
                "recover_interrupted",
                "retry",
            }:
                raise ConflictError("Unsupported disposition")
            if current.status == "completed" or current.phase == "disposed":
                raise ConflictError("Operation already resolved")
            automatic = action == "recover_interrupted"
            repository.fail_task(
                "Execution was interrupted. The runner verified that it has ended; "
                "existing resources were kept. Retry the action if needed."
                if automatic
                else "Interrupted execution acknowledged after fresh runtime scan; "
                "no resources deleted or recreated"
            )
            if action in {"acknowledge_interrupted", "recover_interrupted"}:
                repository.interrupt_children(
                    capture_diagnostic=(
                        "Interrupted child closed; resources preserved, "
                        "no automatic restart"
                    ),
                    recreate_diagnostic=(
                        "Interrupted reset closed; retry the reset or "
                        "delete the workspace"
                    ),
                )
            workspace_status = current.workspace_status
            if current.workspace_id:
                if automatic:
                    workspace_status = {
                        "running": "running",
                        "stopped": "stopped",
                        "exited": "stopped",
                    }.get(evidence.get("workspace_status"), "failed")
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
            repository.release(
                workspace_status,
                (
                    evidence.get("credentials_present")
                    if isinstance(evidence.get("credentials_present"), bool)
                    else True
                    if current.task_type == "inject_credentials"
                    else None
                )
                if automatic
                else None,
                observed_status=evidence.get("workspace_status"),
            )
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
