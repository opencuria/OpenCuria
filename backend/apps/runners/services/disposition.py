"""Operator workflow shared by REST and automatic reconciliation."""

import uuid
from typing import Any

from asgiref.sync import sync_to_async

from apps.accounts.models import APIKeyPermission
from common.exceptions import ConflictError

from ..disposition_repository import DispositionRepository
from ..operations import OperationRepository, apply_result

#: API-key scope a retry needs, per retryable lifecycle task type.
RETRY_PERMISSIONS = {
    "stop_workspace": APIKeyPermission.WORKSPACES_STOP,
    "resume_workspace": APIKeyPermission.WORKSPACES_RESUME,
    "remove_workspace": APIKeyPermission.WORKSPACES_DELETE,
}


class DispositionService:
    """Inspect is non-destructive, including offline; disposition requires proof."""

    @staticmethod
    def list_operations(user: Any, organization_id: uuid.UUID) -> list[dict]:
        """List sanitized operations visible to an owner or organization admin."""
        return DispositionRepository.listing(user, organization_id)

    @staticmethod
    def retry_permission(
        user: Any, organization_id: uuid.UUID, operation_id: uuid.UUID
    ) -> APIKeyPermission | None:
        """Return the API-key scope retrying the operation needs, if retryable."""
        row = DispositionRepository.authorized(user, organization_id, operation_id)
        return RETRY_PERMISSIONS.get(row.task.type)

    async def inspect(
        self,
        transport: Any,
        user: Any,
        organization_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> dict:
        """Request journal status and a fresh scan on the current authenticated SID."""
        row = await sync_to_async(DispositionRepository.authorized)(
            user, organization_id, operation_id
        )
        result = DispositionRepository.summary(row)
        result["evidence"] = {
            "status": "unknown",
            "execution_finished": False,
            "diagnostic": "Runner offline or unavailable; actions disabled",
        }
        result["permitted_actions"] = []
        result["review_message"] = (
            "The runner is unavailable. Restore its connection, then check again."
        )
        if not row.task.runner.is_online or not row.task.runner.sid:
            return result
        try:
            evidence = await transport._call_runner(
                row.task.runner,
                "operation:inspect",
                OperationRepository.envelope(row, {}),
                timeout=60,
            )
            # Recheck connection binding after RPC: old SID responses cannot resolve.
            current = await sync_to_async(DispositionRepository.authorized)(
                user, organization_id, operation_id
            )
            if current.task.runner.sid != row.task.runner.sid:
                return result
            result["evidence"] = evidence
            result["permitted_actions"] = await sync_to_async(
                DispositionRepository.permitted_actions
            )(current, evidence, user, organization_id)
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
        row = await sync_to_async(DispositionRepository.authorized)(
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
                row.task.runner,
                "operation:seal",
                OperationRepository.envelope(row, {}),
                timeout=60,
            )
            if evidence.get("status") != "terminal":
                raise ConflictError("Runner could not seal delayed delivery identity")
        current = await sync_to_async(DispositionRepository.authorized)(
            user, organization_id, operation_id
        )
        if (
            current.task.runner.sid != row.task.runner.sid
            or not current.task.runner.is_online
        ):
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
                str(row.task.runner_id),
                evidence["event"],
                evidence["result"],
            )
            if not accepted:
                raise ConflictError("Journal result could not resolve this operation")
            return {"operation_id": str(operation_id), "reconciled": True}
        return await sync_to_async(DispositionRepository.dispose)(row, evidence, action)
