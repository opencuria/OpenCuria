"""Fork-copy contracts, bounded database work and atomic destination writes."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, get_ident
from typing import Any

import pytest
from django.db import IntegrityError, close_old_connections, connection, connections
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.repositories import HarnessMessageRepository
from apps.harness.timeline import build_part_display


def _session(workspace: Any) -> HarnessSession:
    return HarnessSession.objects.create(
        workspace=workspace, organization_id=workspace.runner.organization_id
    )


@pytest.mark.django_db
def test_copy_preserves_message_fields_and_appends_after_existing_positions(
    harness_workspace: Any,
) -> None:
    """New IDs/timestamps/positions accompany identical replay and usage fields."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    previous = HarnessMessage.objects.create(
        session=destination, role="assistant", position=7, content="keep"
    )
    completed = timezone.now() - timedelta(days=1)
    first = HarnessMessage.objects.create(
        session=source, position=4, role="user", content="prompt", skill_ids=["skill"]
    )
    second = HarnessMessage.objects.create(
        session=source,
        position=19,
        role="assistant",
        content="answer",
        model="model",
        reasoning_effort="high",
        provider="provider",
        cost=0.125,
        tokens={"prompt": 12, "completion": 34, "total": 46},
        finish="aborted",
        error="stopped",
        harness_id="codex",
        engine_meta={"usage": {"cached": 5}},
        completed_at=completed,
        notice_dismissed_at=completed,
    )
    HarnessMessage.objects.filter(session=source).update(created_at=completed)
    for position in (3, 12):
        HarnessPart.objects.create(
            message=second,
            position=position,
            type="text",
            state="running",
            output=f"part {position}",
            meta={"step": 2},
        )

    mapping = HarnessMessageRepository.copy_prefix(source.id, destination.id, None)

    copied = list(HarnessMessage.objects.filter(session=destination))
    assert copied[0].id == previous.id
    assert [row.position for row in copied] == [7, 8, 9]
    assert [row.id for row in copied[1:]] == [mapping[first.id], mapping[second.id]]
    assert not set(mapping).intersection(mapping.values())
    for original, duplicate in zip((first, second), copied[1:], strict=True):
        for field in (
            "role",
            "content",
            "model",
            "reasoning_effort",
            "provider",
            "cost",
            "tokens",
            "finish",
            "error",
            "skill_ids",
            "harness_id",
            "engine_meta",
            "completed_at",
            "notice_dismissed_at",
        ):
            assert getattr(duplicate, field) == getattr(original, field)
        assert duplicate.created_at > completed
    parts = list(copied[-1].parts.all())
    assert [part.position for part in parts] == [0, 1]
    assert [part.output for part in parts] == ["part 3", "part 12"]
    assert all(part.state == "running" for part in parts)
    destination.refresh_from_db()
    assert destination.last_message_at == copied[1].created_at
    assert list(
        HarnessMessage.objects.filter(session=source).values_list(
            "position",
            flat=True,
        )
    ) == [4, 19]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "cutoff, expected", [(None, 3), (0, 1), (1, 2), ("unknown", 0)]
)
def test_copy_through_cutoff(
    harness_workspace: Any,
    cutoff: int | str | None,
    expected: int,
) -> None:
    """Copy through the selected message; invalid cutoffs never copy history."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    messages = HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(
                session=source, position=index, role="user", content=str(index)
            )
            for index in range(3)
        ]
    )
    cutoff_id = (
        messages[cutoff].id
        if isinstance(cutoff, int)
        else uuid.uuid4()
        if cutoff
        else None
    )

    if cutoff == "unknown":
        with pytest.raises(ValueError, match="not in session"):
            HarnessMessageRepository.copy_prefix(source.id, destination.id, cutoff_id)
        assert not HarnessMessage.objects.filter(session=destination).exists()
        return

    mapping = HarnessMessageRepository.copy_prefix(source.id, destination.id, cutoff_id)

    assert list(mapping) == [row.id for row in messages[:expected]]
    assert list(
        HarnessMessage.objects.filter(session=destination).values_list(
            "content",
            flat=True,
        )
    ) == [str(index) for index in range(expected)]


@pytest.mark.django_db
def test_copy_rebuilds_displays_and_remaps_references(harness_workspace: Any) -> None:
    """Legacy patch bodies and external child links cannot leak into a fork."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    user = HarnessMessage.objects.create(session=source, role="user", position=0)
    answer = HarnessMessage.objects.create(session=source, role="assistant", position=1)
    originals = []
    for index, (kind, meta, output) in enumerate(
        [
            (
                "patch",
                {"old_content": "secret", "new_content": "secret", "path": "a.py"},
                "@@ -1 +1 @@\n-old\n+new\n",
            ),
            (
                "subtask",
                {"child_session_id": str(uuid.uuid4()), "subtask_id": "task"},
                "",
            ),
            ("compaction", {"tail_start_id": str(user.id)}, "summary"),
            ("compaction", {"tail_start_id": str(uuid.uuid4())}, "external"),
            ("compaction", {"tail_start_id": "invalid"}, "invalid"),
            ("tool", {}, '{"answers": ["choice"]}'),
        ]
    ):
        originals.append(
            HarnessPart.objects.create(
                message=answer,
                position=index * 3,
                type=kind,
                state="completed",
                title=kind,
                call_id=f"call-{index}",
                input={
                    "tool": "question",
                    "arguments": {
                        "questions": [
                            {"question": "Choose", "options": [{"label": "choice"}]},
                        ]
                    },
                },
                output=output,
                meta=meta,
                display={"stale": True},
            )
        )

    mapping = HarnessMessageRepository.copy_prefix(source.id, destination.id, None)

    copied = list(HarnessPart.objects.filter(message_id=mapping[answer.id]))
    assert len(copied) == len(originals)
    assert not {part.id for part in copied}.intersection(part.id for part in originals)
    assert [part.position for part in copied] == list(range(len(originals)))
    assert "old_content" not in copied[0].meta
    assert "new_content" not in copied[0].meta
    assert copied[1].meta["child_session_id"] == ""
    assert copied[2].meta["tail_start_id"] == str(mapping[user.id])
    assert "tail_start_id" not in copied[3].meta
    assert "tail_start_id" not in copied[4].meta
    for original, duplicate in zip(originals, copied, strict=True):
        assert duplicate.input == original.input
        assert duplicate.output == original.output
        assert duplicate.call_id == original.call_id
        assert duplicate.display == build_part_display(
            part_type=duplicate.type,
            title=duplicate.title,
            call_id=duplicate.call_id,
            input_data=duplicate.input,
            output=duplicate.output,
            meta_data=duplicate.meta,
        )
        original.refresh_from_db()
        assert original.display == {"stale": True}


@pytest.mark.django_db
@pytest.mark.parametrize("turns", [1, 10, 50])
def test_copy_query_budget(harness_workspace: Any, turns: int) -> None:
    """Database work grows by bounded batches, without per-row queries."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    messages = HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(
                session=source,
                position=index,
                role="user" if index % 2 == 0 else "assistant",
            )
            for index in range(turns * 2)
        ]
    )
    HarnessPart.objects.bulk_create(
        [
            HarnessPart(message=message, position=index, type="tool", output="x" * 8192)
            for message in messages[1::2]
            for index in range(5)
        ]
    )

    with CaptureQueriesContext(connection) as queries:
        mapping = HarnessMessageRepository.copy_prefix(source.id, destination.id, None)

    assert len(mapping) == turns * 2
    assert len(queries) <= {1: 10, 10: 10, 50: 20}[turns]
    assert sum(query["sql"].startswith("SELECT") for query in queries) <= (
        3 + (turns * 2 + 49) // 50
    )


@pytest.mark.django_db(transaction=True)
def test_concurrent_copies_append_complete_nonoverlapping_prefixes(
    harness_workspace: Any,
) -> None:
    """Destination allocation is serialized; independent connections close cleanly."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    HarnessMessage.objects.bulk_create(
        [
            HarnessMessage(
                session=source, position=index, role="user", content=str(index)
            )
            for index in range(3)
        ]
    )
    start = Barrier(2)

    def copy() -> dict[uuid.UUID, uuid.UUID]:
        close_old_connections()
        try:
            start.wait(timeout=10)
            return HarnessMessageRepository.copy_prefix(source.id, destination.id, None)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(copy) for _ in range(2)]
        mappings = [job.result(timeout=30) for job in jobs]

    assert set(mappings[0].values()).isdisjoint(mappings[1].values())
    copied = list(HarnessMessage.objects.filter(session=destination))
    assert [row.position for row in copied] == list(range(6))
    assert [row.content for row in copied] == ["0", "1", "2"] * 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "postgresql", reason="PostgreSQL row locks")
def test_copy_retries_position_race_with_an_ordinary_append(
    harness_workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FK checks can commit an append while a copy owns the destination lock."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    HarnessMessage.objects.create(session=source, role="user", content="copy")
    copy_read = Event()
    append_committed = Event()
    copy_thread: list[int] = []
    real_position = HarnessMessageRepository._next_message_position

    def position(session_id: uuid.UUID) -> int:
        result = real_position(session_id)
        if (
            session_id == destination.id
            and get_ident() in copy_thread
            and not copy_read.is_set()
        ):
            copy_read.set()
            assert append_committed.wait(timeout=10)
        return result

    monkeypatch.setattr(
        HarnessMessageRepository, "_next_message_position", staticmethod(position)
    )

    def copy() -> dict[uuid.UUID, uuid.UUID]:
        close_old_connections()
        try:
            copy_thread.append(get_ident())
            return HarnessMessageRepository.copy_prefix(source.id, destination.id, None)
        finally:
            connections.close_all()

    def append() -> None:
        close_old_connections()
        try:
            assert copy_read.wait(timeout=10)
            HarnessMessageRepository.create(
                session_id=destination.id,
                role="assistant",
                content="append",
            )
            append_committed.set()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        copy_job = pool.submit(copy)
        append_job = pool.submit(append)
        assert len(copy_job.result(timeout=30)) == 1
        append_job.result(timeout=30)

    copied = list(HarnessMessage.objects.filter(session=destination))
    assert [row.position for row in copied] == [0, 1]
    assert [row.content for row in copied] == ["append", "copy"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("recover", [False, True])
def test_copy_rolls_back_messages_parts_and_activity_on_failure(
    harness_workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
    recover: bool,
) -> None:
    """A failing later part batch cannot leave a partially copied prefix."""
    source, destination = (_session(harness_workspace) for _ in range(2))
    user = HarnessMessage.objects.create(session=source, role="user", position=0)
    HarnessPart.objects.bulk_create(
        [
            HarnessPart(message=user, position=index, type="text", output="copy")
            for index in range(60)
        ]
    )
    before = destination.last_message_at
    manager = HarnessPart.objects
    real_insert = manager.bulk_create
    failures = 0
    batches = []

    def fail_later(rows: list[HarnessPart], **kwargs: Any) -> list[HarnessPart]:
        nonlocal failures
        batches.append(len(rows))
        if len(rows) < 50 and (not recover or failures == 0):
            failures += 1
            raise IntegrityError("injected part batch failure")
        return real_insert(rows, **kwargs)

    monkeypatch.setattr(manager, "bulk_create", fail_later)
    if recover:
        HarnessMessageRepository.copy_prefix(source.id, destination.id, None)
    else:
        with pytest.raises(IntegrityError, match="injected part batch failure"):
            HarnessMessageRepository.copy_prefix(source.id, destination.id, None)

    copied = list(HarnessMessage.objects.filter(session=destination))
    assert len(copied) == int(recover)
    destination.refresh_from_db()
    if recover:
        assert copied[0].parts.count() == 60
        assert destination.last_message_at == copied[0].created_at
        assert failures == 1
    else:
        assert destination.last_message_at == before
        assert failures == 5
    assert batches == [50, 10] * (2 if recover else 5)
    assert HarnessPart.objects.filter(message=user).count() == 60
