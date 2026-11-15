"""Transcript batches preserve ordering, retry safety and storage isolation."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from django.db import IntegrityError, close_old_connections, connection
from django.test.utils import CaptureQueriesContext

from apps.harness.engines import repositories
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.models import HarnessSession, HarnessTranscriptBatch
from common.utils import decrypt_value


@pytest.fixture
def transcript_session(harness_workspace: Any) -> HarnessSession:
    """Create an unbound session without starting an agent or contacting a runner."""
    return HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id="claude",
    )


def _append(
    session: HarnessSession, entries: list[dict], *, subpath: str = ""
) -> list[HarnessTranscriptBatch]:
    return HarnessEngineRepository.append_transcript_entries(
        session.id, external_session_id="sdk-session", subpath=subpath, entries=entries
    )


@pytest.mark.django_db
def test_mixed_batch_preserves_first_write_and_unkeyed_entries(
    transcript_session: HarnessSession,
) -> None:
    """Duplicate UUIDs return their original row without consuming positions."""
    (original,) = _append(transcript_session, [{"uuid": "old", "text": "original"}])
    entries = [
        {"uuid": "old", "text": "changed"},
        {"uuid": "new", "text": "first 雪"},
        {"type": "title", "text": "unkeyed"},
        {"uuid": "new", "text": "second"},
        {"type": "title", "text": "unkeyed"},
    ]
    rows = _append(transcript_session, entries)
    assert [row.position for row in rows] == [0, 1, 2, 1, 3]
    assert rows[0].id == original.id
    assert rows[1].id == rows[3].id
    assert rows[2].id != rows[4].id
    assert json.loads(decrypt_value(rows[0].payload_encrypted))["text"] == "original"
    assert json.loads(decrypt_value(rows[3].payload_encrypted))["text"] == "first 雪"
    assert all(not row._state.adding and row.created_at is not None for row in rows)
    assert all("unkeyed" not in row.payload_encrypted for row in rows)

    replayed = _append(transcript_session, entries)
    assert [row.position for row in replayed] == [0, 1, 4, 1, 5]
    assert (
        HarnessTranscriptBatch.objects.filter(session=transcript_session).count() == 6
    )


@pytest.mark.django_db
def test_duplicate_keys_are_isolated_by_session_and_subpath(
    transcript_session: HarnessSession,
) -> None:
    """A stable UUID in another transcript must not suppress this transcript's row."""
    other = HarnessSession.objects.create(
        workspace=transcript_session.workspace,
        organization_id=uuid.uuid4(),
        harness_id="claude",
    )
    entries = [{"uuid": "same", "text": "secret transcript"}]
    first = _append(transcript_session, entries)[0]
    child = _append(transcript_session, entries, subpath="subagents/child")[0]
    foreign = _append(other, entries)[0]
    assert len({first.id, child.id, foreign.id}) == 3
    assert [first.position, child.position, foreign.position] == [0, 0, 0]
    assert _append(transcript_session, entries)[0].id == first.id


@pytest.mark.django_db
def test_external_binding_cannot_change(transcript_session: HarnessSession) -> None:
    """An external session mismatch fails before reading or writing transcript data."""
    rows = _append(transcript_session, [{"uuid": "old"}])
    with pytest.raises(ValueError, match="session ID changed"):
        HarnessEngineRepository.append_transcript_entries(
            transcript_session.id,
            external_session_id="another-session",
            subpath="",
            entries=[{"uuid": "new"}],
        )
    assert (
        list(HarnessTranscriptBatch.objects.filter(session=transcript_session)) == rows
    )


@pytest.mark.django_db
@pytest.mark.parametrize("bad", [[], {"uuid": " "}, {"uuid": 7}])
def test_invalid_entry_does_not_bind_or_partially_append(
    transcript_session: HarnessSession, bad: Any
) -> None:
    """Validation applies to the whole batch, including entries after a valid one."""
    with pytest.raises(ValueError, match="transcript entry"):
        _append(transcript_session, [{"uuid": "valid"}, bad])
    transcript_session.refresh_from_db()
    assert transcript_session.external_session_id == ""
    assert not HarnessTranscriptBatch.objects.filter(
        session=transcript_session
    ).exists()


@pytest.mark.django_db
def test_encryption_failure_rolls_back_batch_and_binding(
    transcript_session: HarnessSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed batch cannot expose a prefix or leave a new external binding."""
    encrypt = repositories.encrypt_value

    def fail_second(value: str) -> str:
        if '"uuid":"second"' in value:
            raise ValueError("synthetic encryption failure")
        return encrypt(value)

    monkeypatch.setattr(repositories, "encrypt_value", fail_second)
    with pytest.raises(ValueError, match="synthetic encryption failure"):
        _append(transcript_session, [{"uuid": "first"}, {"uuid": "second"}])
    transcript_session.refresh_from_db()
    assert transcript_session.external_session_id == ""
    assert not HarnessTranscriptBatch.objects.filter(
        session=transcript_session
    ).exists()


@pytest.mark.django_db
def test_empty_batch_is_query_free(
    transcript_session: HarnessSession, django_assert_num_queries: Any
) -> None:
    """Empty flushes do not bind the session or open a transaction."""
    with django_assert_num_queries(0):
        assert _append(transcript_session, []) == []


@pytest.mark.django_db
def test_failed_insert_rolls_back_before_retrying(
    transcript_session: HarnessSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried batch must rebuild its rows after a rolled-back database write."""
    manager = HarnessTranscriptBatch.objects
    bulk_create = manager.bulk_create
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> list[HarnessTranscriptBatch]:
        nonlocal calls
        calls += 1
        rows = bulk_create(*args, **kwargs)
        if calls == 1:
            raise IntegrityError("synthetic failed write")
        return rows

    monkeypatch.setattr(manager, "bulk_create", fail_once)
    rows = _append(transcript_session, [{"uuid": "first"}, {"uuid": "second"}])
    assert calls == 2
    assert [row.position for row in rows] == [0, 1]
    assert (
        list(HarnessTranscriptBatch.objects.filter(session=transcript_session)) == rows
    )


@pytest.mark.django_db(transaction=True)
def test_overlapping_concurrent_batches_are_atomic(
    transcript_session: HarnessSession,
) -> None:
    """Real connections serialize whole batches and retain one shared UUID."""
    ready = Barrier(2)

    def append_batch(index: int) -> list[HarnessTranscriptBatch]:
        close_old_connections()
        try:
            ready.wait(timeout=10)
            return _append(
                transcript_session,
                [
                    {"uuid": "shared", "writer": index},
                    *[{"uuid": f"{index}-{item}"} for item in range(20)],
                ],
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(append_batch, index) for index in range(2)]
        batches = [future.result(timeout=30) for future in futures]
    assert batches[0][0].id == batches[1][0].id
    rows = list(HarnessTranscriptBatch.objects.filter(session=transcript_session))
    assert [row.position for row in rows] == list(range(41))
    writer = json.loads(decrypt_value(rows[0].payload_encrypted))["writer"]
    expected = ["shared"] + [f"{writer}-{item}" for item in range(20)]
    expected += [f"{1 - writer}-{item}" for item in range(20)]
    assert [
        json.loads(decrypt_value(row.payload_encrypted))["uuid"] for row in rows
    ] == (expected)


@pytest.mark.django_db
def test_sdk_sized_batch_uses_bounded_queries(
    transcript_session: HarnessSession,
) -> None:
    """SDK-sized flushes and replays must not issue one lookup/insert per entry."""
    entries = [{"uuid": f"entry-{index}"} for index in range(500)]
    with CaptureQueriesContext(connection) as queries:
        rows = _append(transcript_session, entries)
    # SQLite splits INSERTs at its parameter limit; PostgreSQL needs only one.
    assert len(queries) <= 12
    assert [row.position for row in rows] == list(range(500))
    with CaptureQueriesContext(connection) as replay_queries:
        replay = _append(transcript_session, entries)
    assert len(replay_queries) <= 7
    assert [row.id for row in replay] == [row.id for row in rows]


@pytest.mark.django_db
def test_large_batch_respects_database_parameter_limits(
    transcript_session: HarnessSession,
) -> None:
    """Imports larger than SDK flushes must also batch lookups and inserts safely."""
    entries = [{"uuid": f"entry-{index}"} for index in range(1100)]

    def check_parameters(
        execute: Any, sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        if connection.features.max_query_params is not None:
            assert params is None or len(params) <= connection.features.max_query_params
        return execute(sql, params, many, context)

    with connection.execute_wrapper(check_parameters):
        first = _append(transcript_session, entries)
        replay = _append(transcript_session, list(reversed(entries)))
    assert [row.id for row in replay] == [row.id for row in reversed(first)]
    assert [row.position for row in first] == list(range(1100))
