"""Incarnation-bound lifecycle evidence, independent of transport ownership."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .journal import Journal

if TYPE_CHECKING:
    from .service import WorkspaceService


class WorkspaceEvidence:
    """Persist and inspect readiness/scrub proofs at lifecycle effect boundaries."""

    def __init__(self, service: WorkspaceService, journal: Journal) -> None:
        self._service = service
        self.journal = journal

    async def scrub_proof(self, workspace_id: uuid.UUID) -> bool:
        """Verify the durable scrub checkpoint against a fresh unchanged incarnation."""
        observed = await self._service.workspace_incarnation(str(workspace_id))
        if not observed or observed[1] not in {"exited", "stopped"}:
            await self.journal.call("invalidate_checkpoint", str(workspace_id))
            return False
        proof = await self.journal.call("proof", str(workspace_id), observed[0])
        return bool(
            proof
            and proof.get("credentials_present") is False
            and proof.get("state") in {"exited", "stopped"}
            and proof.get("event")
            in {
                "scrubbed",
                "workspace:stopped",
                "workspace:created",
                "workspace:resumed",
            }
        )

    async def checkpoint(
        self, workspace_id: uuid.UUID, credentials_present: bool | None, event: str
    ) -> None:
        """Checkpoint scrub/readiness against inspected identity, not caller booleans."""
        if event in {"injecting", "scrubbing"}:
            await self.journal.call("invalidate_checkpoint", str(workspace_id))
            return
        try:
            observed = await self._service.workspace_incarnation(str(workspace_id))
        except Exception:  # noqa: BLE001 - inspection/transport is not proof
            observed = None
        if observed:
            from .journal import current_operation_id

            await self.journal.call(
                "checkpoint",
                str(workspace_id),
                observed[0],
                {
                    "event": event,
                    "state": observed[1],
                    "operation_id": current_operation_id(),
                    "credentials_present": credentials_present,
                    "initialized": event
                    in {
                        "workspace:created",
                        "workspace:resumed",
                        "workspace:credentials_injected",
                    },
                },
            )
