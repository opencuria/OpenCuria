"""Transactional storage for durable execution commands."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection
from datetime import timedelta

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from common.utils import decrypt_value, encrypt_value

from .command_models import (
    ADMISSION_COMMAND_KINDS,
    CONTROL_COMMAND_KINDS,
    EXECUTION_HEARTBEAT_TTL_SECONDS,
    HarnessCommand,
    HarnessExecutionHeartbeat,
)


class HarnessCommandRepository:
    """Claim once and fence every completion by the execution owner's identity."""

    @staticmethod
    def enqueue(
        kind: str, payload: dict, session_id: uuid.UUID | None = None
    ) -> HarnessCommand:
        """Commit encrypted intent before an API request waits for execution."""
        if kind not in ADMISSION_COMMAND_KINDS | CONTROL_COMMAND_KINDS:
            raise ValueError(f"Unknown harness command: {kind}")
        payload_session = payload.get("session_id")
        if (
            session_id is not None
            and payload_session is not None
            and uuid.UUID(str(session_id)) != uuid.UUID(str(payload_session))
        ):
            raise ValueError("Command session identity does not match payload")
        return HarnessCommand.objects.create(
            kind=kind,
            session_id=session_id or payload_session,
            payload_encrypted=encrypt_value(json.dumps(payload)),
        )

    @staticmethod
    def get(command_id: uuid.UUID) -> HarnessCommand:
        """Read the request's durable state without touching the execution process."""
        return HarnessCommand.objects.get(pk=command_id)

    @staticmethod
    def withdraw(command_id: uuid.UUID) -> bool:
        """Remove a request before any execution owner claimed it.

        Returns False once the request is claimed; its owner then decides the outcome.
        """
        deleted, _ = HarnessCommand.objects.filter(
            pk=command_id, state=HarnessCommand.State.QUEUED
        ).delete()
        return deleted > 0

    @staticmethod
    def decode_payload(command: HarnessCommand) -> dict:
        """Decrypt request arguments only inside the execution process."""
        return json.loads(decrypt_value(command.payload_encrypted))

    @staticmethod
    def decode_result(command: HarnessCommand) -> dict:
        """Read a result after completion; prompts and failures remain encrypted."""
        return json.loads(decrypt_value(command.result_encrypted))

    @classmethod
    def claim(
        cls, owner_id: uuid.UUID, *, allowed_kinds: Collection[str] | None = None
    ) -> HarnessCommand | None:
        """Claim the oldest request in a lane with available execution capacity."""
        with transaction.atomic():
            queued = HarnessCommand.objects.select_for_update().filter(
                state=HarnessCommand.State.QUEUED
            )
            prior_controls = HarnessCommand.objects.filter(
                session_id=OuterRef("session_id"),
                kind__in={"abort", "delete"},
                state__in={HarnessCommand.State.QUEUED, HarnessCommand.State.CLAIMED},
            ).filter(
                Q(created_at__lt=OuterRef("created_at"))
                | Q(created_at=OuterRef("created_at"), id__lt=OuterRef("id"))
            )
            # A later start must remain queued until an earlier stop has joined
            # rollback and cleared its session stop intent. Other sessions and
            # control commands keep their reserved capacity.
            queued = queued.alias(prior_control=Exists(prior_controls)).filter(
                ~Q(kind__in=ADMISSION_COMMAND_KINDS) | Q(prior_control=False)
            )
            if allowed_kinds is not None:
                queued = queued.filter(kind__in=allowed_kinds)
            command = queued.order_by("created_at", "id").first()
            if command is None:
                return None
            changed = HarnessCommand.objects.filter(
                pk=command.id, state=HarnessCommand.State.QUEUED
            ).update(
                state=HarnessCommand.State.CLAIMED,
                owner_id=owner_id,
                claimed_at=timezone.now(),
            )
            if not changed:
                return None
            if command.kind in {"abort", "delete"} and command.session_id is not None:
                cls._supersede_queued_admissions(command, owner_id)
            command.refresh_from_db()
            return command

    @staticmethod
    def _supersede_queued_admissions(
        control: HarnessCommand, owner_id: uuid.UUID
    ) -> None:
        """A stop must fence earlier queued starts it bypassed, never later work."""
        earlier = Q(created_at__lt=control.created_at) | Q(
            created_at=control.created_at, id__lt=control.id
        )
        now = timezone.now()
        HarnessCommand.objects.filter(
            earlier,
            session_id=control.session_id,
            state=HarnessCommand.State.QUEUED,
            kind__in=ADMISSION_COMMAND_KINDS,
        ).update(
            state=HarnessCommand.State.COMPLETED,
            owner_id=owner_id,
            claimed_at=now,
            completed_at=now,
            payload_encrypted="",
            result_encrypted=encrypt_value(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "type": "ConflictError",
                            "code": "execution_superseded",
                            "message": (
                                "A later stop or deletion replaced this queued request"
                            ),
                        },
                    }
                )
            ),
        )

    @staticmethod
    def complete(command_id: uuid.UUID, owner_id: uuid.UUID, result: dict) -> bool:
        """Publish only while the caller still owns this exact claimed request."""
        return bool(
            HarnessCommand.objects.filter(
                pk=command_id,
                state=HarnessCommand.State.CLAIMED,
                owner_id=owner_id,
            ).update(
                state=HarnessCommand.State.COMPLETED,
                result_encrypted=encrypt_value(json.dumps(result)),
                completed_at=timezone.now(),
                # Once effects finish there is no reason to retain a second prompt copy.
                payload_encrypted="",
            )
        )

    @classmethod
    def interrupt_claimed(cls) -> int:
        """Settle crashed requests after acquiring exclusive execution ownership.

        Never replay a claimed request: effects might already exist although the
        reply was not committed. Queued requests have not had effects and survive.
        """
        rows = list(HarnessCommand.objects.filter(state=HarnessCommand.State.CLAIMED))
        return sum(
            cls.complete(
                row.id,
                row.owner_id,
                {
                    "ok": False,
                    "error": {
                        "type": "ConflictError",
                        "code": "execution_interrupted",
                        "message": (
                            "Execution process restarted before completion; "
                            "inspect the conversation before retrying"
                        ),
                    },
                },
            )
            for row in rows
        )


class ExecutionHeartbeatRepository:
    """Presence of execution worker lifetimes, judged by heartbeat age."""

    @staticmethod
    def beat(owner_id: uuid.UUID) -> None:
        """Renew this lifetime's presence."""
        HarnessExecutionHeartbeat.objects.update_or_create(
            owner_id=owner_id, defaults={"beat_at": timezone.now()}
        )

    @staticmethod
    def remove(owner_id: uuid.UUID) -> None:
        """Withdraw presence once this lifetime stops accepting commands."""
        HarnessExecutionHeartbeat.objects.filter(owner_id=owner_id).delete()

    @staticmethod
    def prune() -> int:
        """Delete presence left behind by lifetimes that ended without cleanup."""
        deleted, _ = HarnessExecutionHeartbeat.objects.filter(
            beat_at__lt=timezone.now()
            - timedelta(seconds=EXECUTION_HEARTBEAT_TTL_SECONDS)
        ).delete()
        return deleted

    @staticmethod
    def is_alive(owner_id: uuid.UUID | None = None) -> bool:
        """Whether a worker (or the given owner) renewed its presence recently."""
        fresh = HarnessExecutionHeartbeat.objects.filter(
            beat_at__gte=timezone.now()
            - timedelta(seconds=EXECUTION_HEARTBEAT_TTL_SECONDS)
        )
        if owner_id is not None:
            fresh = fresh.filter(owner_id=owner_id)
        return fresh.exists()


class SteeringSnapshotRepository:
    """Serve reconnect snapshots without reconstructing an execution queue."""

    @staticmethod
    def set(session_id: uuid.UUID, entries: list[dict]) -> None:
        """Publish the authoritative owner's latest queue as an encrypted snapshot."""
        from .command_models import HarnessSteeringSnapshot

        HarnessSteeringSnapshot.objects.update_or_create(
            session_id=session_id,
            defaults={"payload_encrypted": encrypt_value(json.dumps(entries))},
        )

    @staticmethod
    def get(session_id: uuid.UUID) -> list[dict]:
        """Read the last committed queue projection for API/reconnect clients."""
        from .command_models import HarnessSteeringSnapshot

        encrypted = (
            HarnessSteeringSnapshot.objects.filter(session_id=session_id)
            .values_list("payload_encrypted", flat=True)
            .first()
        )
        return json.loads(decrypt_value(encrypted)) if encrypted else []

    @staticmethod
    def clear() -> None:
        """Discard old process queues only after exclusive worker ownership."""
        from .command_models import HarnessSteeringSnapshot

        HarnessSteeringSnapshot.objects.all().delete()
