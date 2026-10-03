"""One safe operator workflow shared by REST, MCP and automatic reconciliation."""

import uuid
from typing import Any

from asgiref.sync import sync_to_async

from common.exceptions import ConflictError

from ..disposition_repository import DispositionRepository
from ..operations import OperationRepository, apply_result


class DispositionService:
    """Inspect is non-destructive, including offline; disposition requires proof."""

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
        except Exception:
            return result
        return result

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
