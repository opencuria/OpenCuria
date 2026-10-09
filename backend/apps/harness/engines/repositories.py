"""ORM repositories for engine connections and opaque transcripts."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F, Max

from common.utils import decrypt_value, encrypt_value

from ..models import (
    HarnessConnection,
    HarnessMessage,
    HarnessPart,
    HarnessRun,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTranscriptBatch,
)


class HarnessConnectionRepository:
    """Database access for personal engine connections."""

    @staticmethod
    def list_by_org_user(
        organization_id: uuid.UUID, user_id: object
    ) -> list[HarnessConnection]:
        """Return a user's connections in one organization, newest updated first."""
        return list(
            HarnessConnection.objects.filter(
                organization_id=organization_id, user_id=user_id
            ).order_by("-updated_at")
        )

    @staticmethod
    def fetch_connections(
        organization_id: uuid.UUID, user_id: object
    ) -> list[HarnessConnection]:
        """Alias for caller-facing list terminology."""
        return HarnessConnectionRepository.list_by_org_user(organization_id, user_id)

    @staticmethod
    def get_by_org_user(
        organization_id: uuid.UUID, user_id: object
    ) -> HarnessConnection | None:
        """Fetch the unique connection owned by this user in this org."""
        return (
            HarnessConnection.objects.filter(
                organization_id=organization_id, user_id=user_id
            )
            .select_related("user", "credential__service")
            .first()
        )

    @staticmethod
    def get_by_id_for_owner(
        connection_id: uuid.UUID | str,
        *,
        organization_id: uuid.UUID,
        user_id: object,
    ) -> HarnessConnection | None:
        """Fetch a connection only within its owner's exact scope."""
        return (
            HarnessConnection.objects.filter(
                id=connection_id,
                organization_id=organization_id,
                user_id=user_id,
            )
            .select_related("user", "credential__service")
            .first()
        )

    @staticmethod
    def upsert(
        *,
        organization_id: uuid.UUID,
        user_id: object,
        credential_id: uuid.UUID,
        auth_type: str,
        label: str,
    ) -> HarnessConnection:
        """Create or update a personal connection without storing secrets."""
        connection, _ = HarnessConnection.objects.update_or_create(
            organization_id=organization_id,
            user_id=user_id,
            defaults={
                "credential_id": credential_id,
                "auth_type": auth_type,
                "label": label,
            },
        )
        return connection

    @staticmethod
    def update(
        connection: HarnessConnection,
        *,
        credential_id: uuid.UUID | None = None,
        auth_type: str | None = None,
        label: str | None = None,
    ) -> HarnessConnection:
        """Update only supplied connection fields."""
        update_fields = ["updated_at"]
        if credential_id is not None:
            connection.credential_id = credential_id
            update_fields.append("credential")
        if auth_type is not None:
            connection.auth_type = auth_type
            update_fields.append("auth_type")
        if label is not None:
            connection.label = label
            update_fields.append("label")
        connection.save(update_fields=update_fields)
        return connection

    @staticmethod
    def delete(connection: HarnessConnection) -> None:
        """Delete a connection row."""
        connection.delete()


class HarnessEngineRepository:
    """Persistence boundary for resumable Claude sessions and transcripts."""

    @staticmethod
    def is_owner_for_finalization(run_id: uuid.UUID, owner_token: uuid.UUID) -> bool:
        """Check finalization authority, including an owner-fenced CLOSING row."""
        return HarnessRun.objects.filter(
            id=run_id,
            owner_token=owner_token,
            status__in=(
                HarnessRunStatus.STARTING,
                HarnessRunStatus.RUNNING,
                HarnessRunStatus.CLOSING,
            ),
        ).exists()

    @staticmethod
    def settle_owned_shell(
        run_id: uuid.UUID,
        owner_token: uuid.UUID,
        *,
        finish: str,
        error: str,
        assistant_content: str | None = None,
        engine_meta: dict[str, Any] | None = None,
        now=None,
    ) -> bool:
        """Persist an owner-fenced assistant shell while the run remains unresolved."""
        from django.utils import timezone

        moment = now or timezone.now()
        with transaction.atomic():
            attempt = (
                HarnessRun.objects.select_for_update()
                .filter(
                    id=run_id,
                    owner_token=owner_token,
                    status__in=(
                        HarnessRunStatus.STARTING,
                        HarnessRunStatus.RUNNING,
                        HarnessRunStatus.CLOSING,
                    ),
                )
                .first()
            )
            if attempt is None:
                return False
            updates: dict[str, Any] = {
                "finish": finish,
                "error": error,
                "completed_at": moment,
            }
            if assistant_content is not None:
                updates["content"] = assistant_content
            if engine_meta is not None:
                updates["engine_meta"] = dict(engine_meta)
            HarnessMessage.objects.filter(id=attempt.assistant_message_id).update(
                **updates
            )
            HarnessPart.objects.filter(
                message_id=attempt.assistant_message_id,
                state__in=("pending", "running"),
            ).exclude(type__in=("text", "reasoning")).update(
                state="error", updated_at=moment
            )
            HarnessPart.objects.filter(
                message_id=attempt.assistant_message_id,
                state__in=("pending", "running"),
                type__in=("text", "reasoning"),
            ).update(state="completed", updated_at=moment)
            return True

    @staticmethod
    def finalize_owned_turn(
        run_id: uuid.UUID,
        owner_token: uuid.UUID,
        *,
        status: str,
        finish: str,
        error: str = "",
        assistant_content: str | None = None,
        engine_meta: dict[str, Any] | None = None,
        session_usage: dict[str, int] | None = None,
        session_cost: float = 0.0,
        tail_remainder: str = "",
        now=None,
    ) -> bool:
        """Atomically finish an owned attempt, assistant turn and busy session."""
        from django.utils import timezone

        if status not in {
            HarnessRunStatus.COMPLETED,
            HarnessRunStatus.INTERRUPTED,
            HarnessRunStatus.ERROR,
        }:
            raise ValueError("status must be terminal")
        moment = now or timezone.now()
        with transaction.atomic():
            attempt = (
                HarnessRun.objects.select_for_update()
                .filter(
                    id=run_id,
                    owner_token=owner_token,
                    status__in=(
                        HarnessRunStatus.STARTING,
                        HarnessRunStatus.RUNNING,
                        HarnessRunStatus.CLOSING,
                    ),
                )
                .select_related("session")
                .first()
            )
            if attempt is None:
                return False
            changed = HarnessRun.objects.filter(
                id=attempt.id,
                owner_token=owner_token,
                status__in=(
                    HarnessRunStatus.STARTING,
                    HarnessRunStatus.RUNNING,
                    HarnessRunStatus.CLOSING,
                ),
            ).update(
                status=status,
                error="Engine run failed." if error else "",
                heartbeat_at=moment,
                completed_at=moment,
            )
            if not changed:
                return False
            message_updates: dict[str, Any] = {
                "finish": finish,
                "error": error,
                "completed_at": moment,
            }
            if assistant_content is not None:
                message_updates["content"] = assistant_content
            if engine_meta is not None:
                message_updates["engine_meta"] = dict(engine_meta)
            HarnessMessage.objects.filter(id=attempt.assistant_message_id).update(
                **message_updates
            )
            session_updates: dict[str, Any] = {
                "status": HarnessSessionStatus.IDLE,
                "last_message_at": moment,
                "updated_at": moment,
            }
            if session_usage is not None:
                session = attempt.session
                tokens = dict(session.tokens or {})
                for name in ("prompt", "completion", "total"):
                    tokens[name] = int(tokens.get(name, 0)) + int(
                        session_usage.get(name, 0)
                    )
                session_updates["tokens"] = tokens
                session_updates["cost"] = float(session.cost or 0.0) + float(
                    session_cost or 0.0
                )
            HarnessSession.objects.filter(id=attempt.session_id).update(
                **session_updates
            )
            if tail_remainder:
                HarnessPart.objects.create(
                    message_id=attempt.assistant_message_id,
                    type="text",
                    state="completed",
                    output=tail_remainder,
                    meta={"step": None, "tail": True},
                    position=(
                        HarnessPart.objects.filter(
                            message_id=attempt.assistant_message_id
                        ).aggregate(top=Max("position"))["top"]
                        or 0
                    )
                    + 1,
                )
            open_parts = HarnessPart.objects.filter(
                message_id=attempt.assistant_message_id,
                state__in=("pending", "running"),
            )
            if status == HarnessRunStatus.COMPLETED:
                open_parts.update(state="completed", updated_at=moment)
            else:
                open_parts.exclude(type__in=("text", "reasoning")).update(
                    state="error", updated_at=moment
                )
                open_parts.filter(type__in=("text", "reasoning")).update(
                    state="completed", updated_at=moment
                )
            return True

    @staticmethod
    def update_message_engine_meta(message_id: uuid.UUID, engine_meta: dict) -> None:
        """Update metadata only for an assistant message."""
        if not isinstance(engine_meta, dict):
            raise ValueError("engine_meta must be an object")
        HarnessMessage.objects.filter(id=message_id).update(
            engine_meta=dict(engine_meta)
        )

    @staticmethod
    def bind_external_session_if_owner(
        run_id: uuid.UUID,
        owner_token: uuid.UUID,
        session_id: uuid.UUID,
        *,
        external_session_id: str,
        engine_state: dict[str, Any],
    ) -> bool:
        """Persist one external binding while this run still owns writes."""
        external_id = (external_session_id or "").strip()
        if not external_id or len(external_id) > 64:
            raise ValueError("external_session_id must be 1..64 characters")
        with transaction.atomic():
            run = HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
            ).first()
            if run is None or run.session_id != session_id:
                return False
            session = HarnessSession.objects.select_for_update().get(id=session_id)
            current = (session.external_session_id or "").strip()
            if current and current != external_id:
                return False
            session.external_session_id = external_id
            session.engine_state = dict(engine_state or {})
            session.save(
                update_fields=["external_session_id", "engine_state", "updated_at"]
            )
            HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status__in=(HarnessRunStatus.STARTING, HarnessRunStatus.RUNNING),
            ).update(external_session_id=external_id)
            return True

    @staticmethod
    def set_session_engine_state(
        session_id: uuid.UUID,
        *,
        external_session_id: str | None = None,
        engine_state: dict[str, Any] | None = None,
    ) -> HarnessSession:
        """Persist engine resume state without mixing it into provider history."""
        session = HarnessSession.objects.get(id=session_id)
        fields = ["updated_at"]
        if external_session_id is not None:
            cleaned_id = external_session_id.strip()
            if len(cleaned_id) > 64:
                raise ValueError("external_session_id must be at most 64 characters")
            session.external_session_id = cleaned_id
            fields.append("external_session_id")
        if engine_state is not None:
            if not isinstance(engine_state, dict):
                raise ValueError("engine_state must be an object")
            session.engine_state = dict(engine_state)
            fields.append("engine_state")
        session.save(update_fields=fields)
        return session

    @staticmethod
    def append_transcript_entry(
        session_id: uuid.UUID,
        *,
        external_session_id: str,
        subpath: str,
        entry: dict[str, Any],
    ) -> HarnessTranscriptBatch | None:
        """Encrypt and append one opaque entry, deduplicating stable UUIDs."""
        rows = HarnessEngineRepository.append_transcript_entries(
            session_id,
            external_session_id=external_session_id,
            subpath=subpath,
            entries=[entry],
        )
        return rows[0] if rows else None

    @staticmethod
    def append_transcript_entries(
        session_id: uuid.UUID,
        *,
        external_session_id: str,
        subpath: str,
        entries: list[dict[str, Any]],
    ) -> list[HarnessTranscriptBatch]:
        """Atomically append an encrypted transcript batch in SDK order.

        Stable UUIDs are idempotency keys; entries without UUIDs remain
        append-only as required by the SDK transcript store contract.
        """
        external_id = (external_session_id or "").strip()
        cleaned_subpath = (subpath or "").strip()
        if not external_id or len(external_id) > 64:
            raise ValueError("external_session_id must be 1..64 characters")
        if len(cleaned_subpath) > 255:
            raise ValueError("subpath must be at most 255 characters")
        prepared: list[tuple[dict[str, Any], str, str, str | None]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("transcript entry must be an object")
            stable_uuid = entry.get("uuid")
            if stable_uuid is not None and (
                not isinstance(stable_uuid, str) or not stable_uuid.strip()
            ):
                raise ValueError("transcript entry uuid must be a non-empty string")
            canonical = json.dumps(
                entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
            digest_source = (
                f"uuid:{stable_uuid}"
                if stable_uuid is not None
                else f"unkeyed:{uuid.uuid4()}"
            )
            digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
            prepared.append(
                (entry, canonical, digest, stable_uuid if stable_uuid is not None else None)
            )
        if not prepared:
            return []

        attempts = 0
        while attempts < 5:
            attempts += 1
            try:
                with transaction.atomic():
                    # UPDATE provides SQLite and PostgreSQL write serialization
                    # for concurrent batches targeting the same session.
                    updated = HarnessSession.objects.filter(id=session_id).update(
                        updated_at=F("updated_at")
                    )
                    if not updated:
                        raise HarnessSession.DoesNotExist
                    session = HarnessSession.objects.get(id=session_id)
                    if session.external_session_id and (
                        session.external_session_id != external_id
                    ):
                        raise ValueError(
                            "Claude session ID changed for this harness session"
                        )
                    if not session.external_session_id:
                        session.external_session_id = external_id
                        session.save(update_fields=["external_session_id", "updated_at"])
                    top = HarnessTranscriptBatch.objects.filter(
                        session_id=session.id,
                        external_session_id=external_id,
                        subpath=cleaned_subpath,
                    ).aggregate(top=Max("position"))["top"]
                    position = int(top) + 1 if top is not None else 0
                    persisted: list[HarnessTranscriptBatch] = []
                    for entry, canonical, digest, stable_uuid in prepared:
                        existing = None
                        if stable_uuid is not None:
                            existing = HarnessTranscriptBatch.objects.filter(
                                session_id=session.id,
                                external_session_id=external_id,
                                subpath=cleaned_subpath,
                                digest=digest,
                            ).first()
                        if existing is not None:
                            persisted.append(existing)
                            continue
                        persisted.append(
                            HarnessTranscriptBatch.objects.create(
                                session=session,
                                external_session_id=external_id,
                                subpath=cleaned_subpath,
                                digest=digest,
                                payload_encrypted=encrypt_value(canonical),
                                position=position,
                            )
                        )
                        position += 1
                    return persisted
            except IntegrityError:
                if attempts >= 5:
                    raise
        raise RuntimeError("Unable to allocate transcript position after retries")

    @staticmethod
    def list_transcript_subpaths(
        session_id: uuid.UUID, *, external_session_id: str
    ) -> list[str]:
        """List distinct agent transcript paths for one mirrored SDK session."""
        return list(
            HarnessTranscriptBatch.objects.filter(
                session_id=session_id,
                external_session_id=(external_session_id or "").strip(),
            )
            .exclude(subpath="")
            .order_by("subpath")
            .values_list("subpath", flat=True)
            .distinct()
        )

    @staticmethod
    def get_session_engine_record(session_id: uuid.UUID) -> dict[str, Any]:
        """Return only the internal session identifier and current engine binding."""
        row = HarnessSession.objects.filter(id=session_id).values(
            "id", "external_session_id", "updated_at"
        ).first()
        if row is None:
            raise HarnessSession.DoesNotExist
        return row

    @staticmethod
    def list_transcript_entries(
        session_id: uuid.UUID,
        *,
        external_session_id: str,
        subpath: str = "",
    ) -> list[dict[str, Any]]:
        """Decrypt ordered transcript entries for an authorized session caller."""
        rows = HarnessTranscriptBatch.objects.filter(
            session_id=session_id,
            external_session_id=(external_session_id or "").strip(),
            subpath=(subpath or "").strip(),
        ).order_by("position", "created_at", "id")
        entries: list[dict[str, Any]] = []
        for row in rows:
            parsed = json.loads(decrypt_value(row.payload_encrypted))
            if not isinstance(parsed, dict):
                raise ValueError("Stored transcript entry is not an object")
            entries.append(parsed)
        return entries

    @staticmethod
    def clear_transcript(
        session_id: uuid.UUID,
        *,
        external_session_id: str | None = None,
        subpath: str | None = None,
    ) -> int:
        """Delete transcript rows for an engine session, optionally by path."""
        query = HarnessTranscriptBatch.objects.filter(session_id=session_id)
        if external_session_id is not None:
            query = query.filter(external_session_id=external_session_id)
        if subpath is not None:
            query = query.filter(subpath=subpath)
        count, _ = query.delete()
        return count


# Backwards-compatible, more domain-oriented name for service callers.
HarnessTranscriptRepository = HarnessEngineRepository
