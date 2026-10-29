"""ORM repository for engine state and opaque transcripts."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F, Max

from common.utils import decrypt_value, encrypt_value

from ..models import (
    HarnessMessage,
    HarnessPart,
    HarnessRun,
    HarnessRunStatus,
    HarnessSession,
    HarnessSessionStatus,
    HarnessTranscriptBatch,
)

_TRANSCRIPT_LOOKUP_BATCH_SIZE = 500
# Limit PostgreSQL's transient SQL buffers for encrypted payloads.
_TRANSCRIPT_INSERT_BATCH_SIZE = 100


class HarnessEngineRepository:
    """Persistence boundary for resumable Claude sessions and transcripts."""

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
        engine_state_updates: dict[str, Any] | None = None,
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
            if engine_state_updates:
                session_updates["engine_state"] = {
                    **dict(attempt.session.engine_state or {}),
                    **engine_state_updates,
                }
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
        if len(external_id) > 64:
            raise ValueError("external_session_id must be at most 64 characters")
        with transaction.atomic():
            run = (
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
            if run is None or run.session_id != session_id:
                return False
            session = HarnessSession.objects.select_for_update().get(id=session_id)
            if session.harness_id != "codex" and (
                not external_id or run.status == HarnessRunStatus.CLOSING
            ):
                if not external_id:
                    raise ValueError("external_session_id must be 1..64 characters")
                return False
            current = (session.external_session_id or "").strip()
            if current and current != external_id:
                return False
            session.external_session_id = external_id
            previous = dict(session.engine_state or {})
            incoming = dict(engine_state or {})
            if session.harness_id == "codex":
                from .codex.catalog import codex_provider

                provider_id = codex_provider(session.model, previous)
                if incoming.get("provider_id", provider_id) != provider_id:
                    raise ValueError("Codex conversation provider cannot be changed")
                incoming["provider_id"] = provider_id
            session.engine_state = {**previous, **incoming}
            session.save(
                update_fields=["external_session_id", "engine_state", "updated_at"]
            )
            HarnessRun.objects.filter(
                id=run_id,
                owner_token=owner_token,
                status__in=(
                    HarnessRunStatus.STARTING,
                    HarnessRunStatus.RUNNING,
                    HarnessRunStatus.CLOSING,
                ),
            ).update(external_session_id=external_id)
            return True

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
        prepared: list[tuple[str, str, str | None]] = []
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
            prepared.append((canonical, digest, stable_uuid))
        if not prepared:
            return []
        keyed_digests = list(
            dict.fromkeys(
                digest for _, digest, stable_uuid in prepared if stable_uuid is not None
            )
        )

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
                        session.save(
                            update_fields=["external_session_id", "updated_at"]
                        )
                    transcript = HarnessTranscriptBatch.objects.filter(
                        session_id=session.id,
                        external_session_id=external_id,
                        subpath=cleaned_subpath,
                    )
                    top = transcript.aggregate(top=Max("position"))["top"]
                    position = int(top) + 1 if top is not None else 0
                    by_digest: dict[str, HarnessTranscriptBatch] = {}
                    # Bound lookup parameters; retain the session's write lock.
                    for offset in range(
                        0, len(keyed_digests), _TRANSCRIPT_LOOKUP_BATCH_SIZE
                    ):
                        batch = keyed_digests[
                            offset : offset + _TRANSCRIPT_LOOKUP_BATCH_SIZE
                        ]
                        by_digest.update(
                            (row.digest, row)
                            for row in transcript.filter(digest__in=batch).order_by()
                        )
                    persisted: list[HarnessTranscriptBatch] = []
                    new_rows: list[HarnessTranscriptBatch] = []
                    for canonical, digest, stable_uuid in prepared:
                        existing = (
                            by_digest.get(digest) if stable_uuid is not None else None
                        )
                        if existing is not None:
                            persisted.append(existing)
                            continue
                        row = HarnessTranscriptBatch(
                            session=session,
                            external_session_id=external_id,
                            subpath=cleaned_subpath,
                            digest=digest,
                            payload_encrypted=encrypt_value(canonical),
                            position=position,
                        )
                        new_rows.append(row)
                        persisted.append(row)
                        by_digest[digest] = row
                        position += 1
                    HarnessTranscriptBatch.objects.bulk_create(
                        new_rows, batch_size=_TRANSCRIPT_INSERT_BATCH_SIZE
                    )
                    return persisted
            except IntegrityError:
                if attempts >= 5:
                    raise
        raise RuntimeError("Unable to allocate transcript position after retries")

    @staticmethod
    def replace_owned_checkpoint(
        session_id: uuid.UUID,
        *,
        run_id: uuid.UUID | str,
        owner_token: uuid.UUID | str,
        external_session_id: str,
        subpath: str,
        generation: str,
        snapshot: dict[str, Any],
    ) -> None:
        """Atomically replace one encrypted checkpoint under the durable owner.

        Lock order matches owner-fenced binding updates (run before session).
        The no-op UPDATE also serializes SQLite writers, where SELECT FOR UPDATE
        is inert. The owner fence is checked under that write lock.
        Deletion, encryption and insertion share a transaction, so any failure
        preserves the previous generation. Domain snapshot validation belongs
        to the engine's store; this boundary validates persistence identifiers.
        """
        uuid.UUID(str(generation))
        uuid.UUID(str(external_session_id))
        with transaction.atomic():
            live_owner = HarnessRun.objects.filter(
                id=run_id, owner_token=owner_token, session_id=session_id,
                status__in=(
                    HarnessRunStatus.STARTING,
                    HarnessRunStatus.RUNNING,
                    HarnessRunStatus.CLOSING,
                ),
            )
            if not live_owner.update(heartbeat_at=F("heartbeat_at")):
                raise RuntimeError("Codex checkpoint ownership superseded")
            if not live_owner.select_for_update().exists():
                raise RuntimeError("Codex checkpoint ownership superseded")
            HarnessSession.objects.select_for_update().get(id=session_id)
            HarnessEngineRepository.clear_transcript(
                session_id, external_session_id=external_session_id, subpath=subpath
            )
            HarnessEngineRepository.append_transcript_entries(
                session_id,
                external_session_id=external_session_id,
                subpath=subpath,
                entries=[
                    {
                        "uuid": generation,
                        "generation": generation,
                        "snapshot": snapshot,
                    }
                ],
            )

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
    def get_transcript_mtime(
        session_id: uuid.UUID, *, external_session_id: str
    ) -> int | None:
        """Return the latest real transcript batch write time in milliseconds."""
        latest = HarnessTranscriptBatch.objects.filter(
            session_id=session_id,
            external_session_id=(external_session_id or "").strip(),
        ).aggregate(latest=Max("created_at"))["latest"]
        return int(latest.timestamp() * 1000) if latest is not None else None

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
