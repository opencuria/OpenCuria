"""Provider-history ordering, compaction isolation and bounded database work."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from math import ceil
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.db.models.signals import post_init
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.harness.compaction import CHECKPOINT_PREFIX
from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.provider_history_service import (
    HISTORY_PART_BATCH_SIZE,
    ProviderHistoryService,
)
from apps.harness.repositories import HarnessPartRepository, HarnessSessionRepository


def _session(workspace: Any) -> HarnessSession:
    return HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
    )


def _message(session: HarnessSession, position: int, **fields: Any) -> HarnessMessage:
    return HarnessMessage.objects.create(session=session, position=position, **fields)


def _part(message: HarnessMessage, position: int, **fields: Any) -> HarnessPart:
    return HarnessPart.objects.create(message=message, position=position, **fields)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "meta",
    ["invalid", [], {}, {"attachments": "invalid"}, {"attachments": [None, "bad"]}],
)
def test_history_ignores_malformed_tool_attachment_metadata(
    harness_workspace: Any, meta: Any
) -> None:
    """Legacy malformed metadata must not prevent plain-text tool replay."""
    session = _session(harness_workspace)
    assistant = _message(session, 0, role="assistant")
    _part(
        assistant,
        0,
        type="tool",
        state="completed",
        call_id="read",
        output="plain result",
        meta=meta,
    )
    history = async_to_sync(ProviderHistoryService().build)(session)
    assert history[-1].role == "tool"
    assert history[-1].content == "plain result"


@pytest.mark.django_db
def test_history_restores_pdf_attachment_among_invalid_entries(
    harness_workspace: Any,
) -> None:
    """PDF bytes and filenames survive persisted tool replay without extra keys."""
    session = _session(harness_workspace)
    assistant = _message(session, 0, role="assistant")
    pdf = {
        "type": "file",
        "mime": "application/pdf",
        "url": "data:application/pdf;base64,AAAA",
        "filename": "report.pdf",
    }
    _part(
        assistant,
        0,
        type="tool",
        state="completed",
        call_id="pdf",
        output="PDF result",
        meta={"attachments": [None, "invalid", {**pdf, "extra": "discard"}]},
    )
    history = async_to_sync(ProviderHistoryService().build)(session)
    assert history[-1].content == [{"type": "text", "text": "PDF result"}, pdf]


@pytest.mark.django_db
@pytest.mark.parametrize("turns", [1, 8, 9, 50])
@pytest.mark.parametrize("compacted", [False, True])
def test_history_batches_only_load_retained_replay_parts(
    harness_workspace: Any, turns: int, compacted: bool
) -> None:
    """Batch reads skip unused payloads and stay within a fixed query budget."""
    session = _session(harness_workspace)
    messages: list[HarnessMessage] = []
    parts: list[HarnessPart] = []
    retained_ids: set[uuid.UUID] = set()
    tail_index = max(0, turns - 5) if compacted else 0
    for index in range(turns):
        user = HarnessMessage(
            session=session, position=index * 2, role="user", content=f"user {index}"
        )
        assistant = HarnessMessage(
            session=session,
            position=index * 2 + 1,
            role="assistant",
            content="a" * 16384,
            engine_meta={"unused": "m" * 32768},
        )
        messages.extend([user, assistant])
        for position, kind in enumerate(("text", "tool", "reasoning", "agent")):
            part = HarnessPart(
                message=assistant,
                position=position,
                type=kind,
                state="completed",
                call_id=f"call-{index}" if kind == "tool" else "",
                output=f"{kind} {index}" if kind == "text" else "x" * 32768,
                input={"tool": "read", "arguments": {}} if kind == "tool" else {},
                display={"unused": "y" * 10000},
            )
            parts.append(part)
            if index >= tail_index and kind in ("text", "tool"):
                retained_ids.add(part.id)
        if compacted and index == tail_index:
            checkpoint = HarnessPart(
                message=assistant,
                position=4,
                type="compaction",
                state="completed",
                output="checkpoint",
                meta={"tail_start_id": str(user.id)},
            )
            parts.append(checkpoint)
            retained_ids.add(checkpoint.id)
    HarnessMessage.objects.bulk_create(messages)
    HarnessPart.objects.bulk_create(parts)
    loaded: list[HarnessPart] = []
    loaded_messages: list[HarnessMessage] = []

    def record_loaded(sender: Any, instance: HarnessPart, **kwargs: Any) -> None:
        loaded.append(instance)

    def record_message(sender: Any, instance: HarnessMessage, **kwargs: Any) -> None:
        loaded_messages.append(instance)

    post_init.connect(record_loaded, sender=HarnessPart)
    post_init.connect(record_message, sender=HarnessMessage)
    try:
        with CaptureQueriesContext(connection) as queries:
            history = async_to_sync(ProviderHistoryService().build)(session)
    finally:
        post_init.disconnect(record_loaded, sender=HarnessPart)
        post_init.disconnect(record_message, sender=HarnessMessage)

    query_budget = 3 if compacted else {1: 3, 8: 3, 9: 4, 50: 9}[turns]
    assert len(queries) <= query_budget
    assert {message.id for message in loaded_messages} == {
        message.id for message in messages[tail_index * 2 :]
    }
    assert all("engine_meta" in row.get_deferred_fields() for row in loaded_messages)
    assert len(loaded_messages) == (turns - tail_index) * 2
    assert {part.id for part in loaded} == retained_ids
    assert len(loaded) == len(retained_ids)
    assert all("display" in part.get_deferred_fields() for part in loaded)
    if compacted:
        checkpoint_row = loaded[0]
        assert checkpoint_row.id == checkpoint.id
        assert checkpoint_row.get_deferred_fields() >= {"input", "display", "title"}
        assert checkpoint_row.output == "checkpoint"
    assert len(history) == (turns - tail_index) * 3 + int(compacted)
    assert [row.content for row in history if row.role == "user"] == (
        [f"{CHECKPOINT_PREFIX}\ncheckpoint"] if compacted else []
    ) + [f"user {index}" for index in range(tail_index, turns)]


@pytest.mark.django_db
@pytest.mark.parametrize("has_user", [False, True])
def test_history_without_assistants_skips_replay_parts(
    harness_workspace: Any, has_user: bool
) -> None:
    """Empty/new conversations only check for a checkpoint and read messages."""
    session = _session(harness_workspace)
    if has_user:
        _message(session, 0, role="user", content="hello")
    with CaptureQueriesContext(connection) as queries:
        history = async_to_sync(ProviderHistoryService().build)(session)
    assert len(queries) == 2
    assert [row.content for row in history] == (["hello"] if has_user else [])


@pytest.mark.django_db
@pytest.mark.parametrize("summary", ["new checkpoint", "  "])
def test_history_selects_last_completed_checkpoint_in_timeline_order(
    harness_workspace: Any, summary: str
) -> None:
    """Message/part positions, not insertion timestamps, choose the checkpoint."""
    session = _session(harness_workspace)
    old = _message(session, 0, role="assistant", content="old")
    tail = _message(session, 1, role="user", content="retained user")
    latest = _message(session, 2, role="assistant", content="retained answer")
    _part(
        latest,
        8,
        type="compaction",
        state="completed",
        output=summary,
        meta={"tail_start_id": str(tail.id)},
    )
    _part(latest, 7, type="compaction", state="completed", output="superseded")
    _part(old, 99, type="compaction", state="completed", output="old checkpoint")
    _part(latest, 9, type="compaction", state="pending", output="not completed")
    _part(latest, 10, type="compaction", state="error", output="failed")
    _part(tail, 0, type="compaction", state="completed", output="not assistant")
    foreign = _message(_session(harness_workspace), 0, role="assistant")
    _part(foreign, 0, type="compaction", state="completed", output="other session")
    HarnessMessage.objects.filter(id=old.id).update(
        created_at=timezone.now() + timedelta(days=1)
    )
    history = async_to_sync(ProviderHistoryService().build)(session)
    expected = ["retained user", "retained answer"]
    if summary.strip():
        expected.insert(0, f"{CHECKPOINT_PREFIX}\n{summary}")
    assert [row.content for row in history] == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    "exclude_keyword",
    ["exclude_message_id", "exclude_user_message_id", "exclude_message_ids"],
)
def test_history_excludes_messages_before_checkpoint_selection(
    harness_workspace: Any, exclude_keyword: str
) -> None:
    """An excluded checkpoint cannot drop earlier messages from provider replay."""
    session = _session(harness_workspace)
    first = _message(session, 0, role="user", content="first retained")
    old = _message(session, 1, role="assistant", content="old summary")
    _part(
        old,
        0,
        type="compaction",
        state="completed",
        output="old summary",
        meta={"tail_start_id": str(first.id)},
    )
    _message(session, 2, role="user", content="next retained")
    latest = _message(session, 3, role="assistant", content="excluded")
    _part(latest, 0, type="compaction", state="completed", output="excluded summary")
    excluded = [latest.id] if exclude_keyword == "exclude_message_ids" else latest.id
    history = async_to_sync(ProviderHistoryService().build)(
        session, **{exclude_keyword: excluded}
    )
    assert [row.content for row in history] == [
        f"{CHECKPOINT_PREFIX}\nold summary",
        "first retained",
        None,
        "next retained",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("tail", ["malformed", "missing", "excluded", "foreign"])
def test_history_checkpoint_missing_tail_falls_back_to_assistant(
    harness_workspace: Any, tail: str
) -> None:
    """Invalid, absent, excluded and foreign tails retain the checkpoint assistant."""
    session = _session(harness_workspace)
    user = _message(session, 0, role="user", content="omit this user")
    assistant = _message(session, 1, role="assistant", content="summary")
    foreign = _message(_session(harness_workspace), 0, role="user", content="private")
    tail_id = {
        "malformed": "not-a-uuid",
        "missing": str(uuid.uuid4()),
        "excluded": str(user.id),
        "foreign": str(foreign.id),
    }[tail]
    _part(
        assistant,
        0,
        type="compaction",
        state="completed",
        output="summary",
        meta={"tail_start_id": tail_id},
    )
    history = async_to_sync(ProviderHistoryService().build)(
        session, exclude_user_message_id=user.id if tail == "excluded" else None
    )
    assert [row.content for row in history] == [f"{CHECKPOINT_PREFIX}\nsummary", None]


@pytest.mark.django_db
def test_history_replay_keeps_text_tools_and_steers_in_position_order(
    harness_workspace: Any,
) -> None:
    """Checkpoint discovery must not change tool, steer or text replay."""
    session = _session(harness_workspace)
    first = _message(session, 0, role="assistant", content="legacy answer")
    second = _message(session, 1, role="assistant", content="flattened text")
    # Insert rows out of position order; unused parts must not affect replay.
    _part(second, 9, type="text", output="after")
    _part(second, 4, type="steer", output="user correction")
    _part(
        second,
        5,
        type="tool",
        state="error",
        call_id="failed",
        output="failure",
        input={"tool": "bash", "arguments": {"command": "false"}},
    )
    _part(
        second,
        2,
        type="tool",
        state="completed",
        call_id="read",
        output="body",
        input={"tool": "read", "arguments": '{"path":"a.txt"}'},
    )
    _part(second, 0, type="reasoning", output="not provider history")
    _part(second, 1, type="text", output="before")
    _part(second, 3, type="tool", state="running", call_id="running", output="omit")
    _part(second, 6, type="tool", state="completed", output="missing call id")
    _part(second, 7, type="subtask", output="omit subtask")
    HarnessMessage.objects.filter(id=first.id).update(
        created_at=timezone.now() + timedelta(days=1)
    )
    history = async_to_sync(ProviderHistoryService().build)(session)
    assert [(row.role, row.content) for row in history] == [
        ("assistant", "legacy answer"),
        ("assistant", "before"),
        ("tool", "body"),
        ("user", "user correction"),
        ("assistant", None),
        ("tool", "failure"),
        ("assistant", "after"),
    ]
    assert [row.tool_call_id for row in history if row.role == "tool"] == [
        "read",
        "failed",
    ]
    assert history[4].tool_calls == [
        {"id": "failed", "name": "bash", "arguments": '{"command": "false"}'}
    ]


@pytest.mark.django_db
def test_history_tail_at_position_zero_and_position_gaps(
    harness_workspace: Any,
) -> None:
    """An eligible tail may precede its checkpoint, including at position zero."""
    session = _session(harness_workspace)
    user = _message(session, 0, role="user", content="keep first")
    assistant = _message(session, 9, role="assistant", content="checkpoint")
    _part(
        assistant,
        0,
        type="compaction",
        state="completed",
        output="checkpoint",
        meta={"tail_start_id": str(user.id)},
    )
    _message(session, 20, role="user", content="keep last")
    history = async_to_sync(ProviderHistoryService().build)(session)
    assert [row.content for row in history] == [
        f"{CHECKPOINT_PREFIX}\ncheckpoint",
        "keep first",
        None,
        "keep last",
    ]


@pytest.mark.django_db
def test_history_range_failure_aborts_before_replay(
    harness_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Database failures cannot silently change the replay boundary."""
    from django.db import DatabaseError

    session = _session(harness_workspace)
    assistant = _message(session, 0, role="assistant", content="checkpoint")
    _part(
        assistant,
        0,
        type="compaction",
        state="completed",
        output="checkpoint",
        meta={"tail_start_id": str(assistant.id)},
    )
    service = ProviderHistoryService()

    def fail_range(*args: Any, **kwargs: Any) -> list[HarnessMessage]:
        raise DatabaseError("synthetic range failure")

    monkeypatch.setattr(service.messages, "list_for_history", fail_range)
    loaded: list[HarnessMessage] = []

    def record_loaded(sender: Any, instance: HarnessMessage, **kwargs: Any) -> None:
        loaded.append(instance)

    post_init.connect(record_loaded, sender=HarnessMessage)
    try:
        with pytest.raises(DatabaseError, match="synthetic range failure"):
            async_to_sync(service.build)(session)
    finally:
        post_init.disconnect(record_loaded, sender=HarnessMessage)
    assert loaded == []


@pytest.mark.django_db
@pytest.mark.parametrize("turns", [8, 9, 17])
def test_history_batches_include_assistants_without_parts(
    harness_workspace: Any, turns: int
) -> None:
    """Empty groups must not skip later assistants or trigger extra batch reads."""
    session = _session(harness_workspace)
    for index in range(turns):
        _message(session, index, role="assistant", content=f"legacy {index}")
    # The last assistant owns replayable parts after batches of empty groups.
    last = _message(session, turns, role="assistant", content="flattened")
    _part(last, 1, type="text", output="last answer")
    with CaptureQueriesContext(connection) as queries:
        history = async_to_sync(ProviderHistoryService().build)(session)
    assert len(queries) == 2 + ceil((turns + 1) / HISTORY_PART_BATCH_SIZE)
    assert [row.content for row in history] == [
        *(f"legacy {index}" for index in range(turns)),
        "last answer",
    ]


@pytest.mark.django_db
def test_history_batch_cannot_load_excluded_or_foreign_messages(
    harness_workspace: Any,
) -> None:
    """Batch inputs come only from the retained, session-scoped message list."""
    session = _session(harness_workspace)
    retained = _message(session, 0, role="assistant")
    excluded = _message(session, 1, role="assistant")
    foreign = _message(_session(harness_workspace), 0, role="assistant")
    for message in (retained, excluded, foreign):
        _part(message, 0, type="text", output=str(message.id))
    loaded: list[uuid.UUID] = []

    def record_loaded(sender: Any, instance: HarnessPart, **kwargs: Any) -> None:
        loaded.append(instance.message_id)

    post_init.connect(record_loaded, sender=HarnessPart)
    try:
        history = async_to_sync(ProviderHistoryService().build)(
            session, exclude_message_id=excluded.id
        )
    finally:
        post_init.disconnect(record_loaded, sender=HarnessPart)
    assert loaded == [retained.id]
    assert [row.content for row in history] == [str(retained.id)]


@pytest.mark.django_db
def test_history_cancellation_stops_replay_between_assistants(
    harness_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Batching must preserve cancellation points without preloading all parts."""
    session = _session(harness_workspace)
    for index in range(HISTORY_PART_BATCH_SIZE * 2 + 1):
        _message(session, index, role="assistant", content="answer")
    service = ProviderHistoryService()
    replayed: list[uuid.UUID] = []

    def cancel_after_first(message: HarnessMessage, *args: Any, **kwargs: Any) -> str:
        replayed.append(message.id)
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return message.content

    monkeypatch.setattr(service, "_assistant_history_content", cancel_after_first)

    async def cancel_history() -> None:
        with pytest.raises(asyncio.CancelledError):
            await service.build(session)

    with CaptureQueriesContext(connection) as queries:
        async_to_sync(cancel_history)()
    assert len(replayed) == 1
    assert len(queries) == 3  # Messages, checkpoint, first part batch only.


@pytest.mark.django_db
def test_history_propagates_later_batch_read_failure(
    harness_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed batch must abort history construction, never return partial replay."""
    from django.db import DatabaseError

    session = _session(harness_workspace)
    for index in range(HISTORY_PART_BATCH_SIZE + 1):
        _message(session, index, role="assistant", content="answer")
    original = HarnessPartRepository.history_parts_by_message
    batches: list[list[uuid.UUID]] = []

    def fail_second_batch(
        message_ids: list[uuid.UUID],
    ) -> dict[uuid.UUID, list[HarnessPart]]:
        batches.append(message_ids)
        if len(batches) == 2:
            raise DatabaseError("synthetic batch read failure")
        return original(message_ids)

    monkeypatch.setattr(
        HarnessPartRepository, "history_parts_by_message", fail_second_batch
    )
    with pytest.raises(DatabaseError, match="synthetic batch read failure"):
        async_to_sync(ProviderHistoryService().build)(session)
    assert [len(batch) for batch in batches] == [HISTORY_PART_BATCH_SIZE, 1]
