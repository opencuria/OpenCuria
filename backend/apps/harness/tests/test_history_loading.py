"""Provider-history ordering, compaction isolation and bounded database work."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.db import connection
from django.db.models.signals import post_init
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.harness.compaction import CHECKPOINT_PREFIX
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.repositories import HarnessSessionRepository


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
@pytest.mark.parametrize("turns", [1, 10, 50])
@pytest.mark.parametrize("compacted", [False, True])
def test_history_checkpoint_lookup_never_loads_discarded_parts(
    harness_workspace: Any, turns: int, compacted: bool
) -> None:
    """Checkpoint discovery costs one read, independent of full history size."""
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
            session=session, position=index * 2 + 1, role="assistant"
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
            if index >= tail_index:
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

    def record_loaded(sender: Any, instance: HarnessPart, **kwargs: Any) -> None:
        loaded.append(instance)

    post_init.connect(record_loaded, sender=HarnessPart)
    try:
        with CaptureQueriesContext(connection) as queries:
            history = async_to_sync(HarnessService()._build_history)(session)
    finally:
        post_init.disconnect(record_loaded, sender=HarnessPart)

    assert len(queries) == 2 + turns - tail_index
    assert {part.id for part in loaded} == retained_ids
    assert len(loaded) == len(retained_ids) + int(compacted)
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
def test_history_without_assistants_only_reads_messages(
    harness_workspace: Any, has_user: bool
) -> None:
    """Empty/new conversations do not query parts."""
    session = _session(harness_workspace)
    if has_user:
        _message(session, 0, role="user", content="hello")
    with CaptureQueriesContext(connection) as queries:
        history = async_to_sync(HarnessService()._build_history)(session)
    assert len(queries) == 1
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
    history = async_to_sync(HarnessService()._build_history)(session)
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
    history = async_to_sync(HarnessService()._build_history)(
        session, **{exclude_keyword: excluded}
    )
    assert [row.content for row in history] == [
        f"{CHECKPOINT_PREFIX}\nold summary",
        "first retained",
        None,
        "next retained",
    ]


@pytest.mark.django_db
@pytest.mark.parametrize("tail", ["malformed", "missing", "excluded"])
def test_history_checkpoint_missing_tail_falls_back_to_assistant(
    harness_workspace: Any, tail: str
) -> None:
    """Malformed, absent and excluded tails retain the checkpoint's assistant."""
    session = _session(harness_workspace)
    user = _message(session, 0, role="user", content="omit this user")
    assistant = _message(session, 1, role="assistant", content="summary")
    tail_id = {
        "malformed": "not-a-uuid",
        "missing": str(uuid.uuid4()),
        "excluded": str(user.id),
    }[tail]
    _part(
        assistant,
        0,
        type="compaction",
        state="completed",
        output="summary",
        meta={"tail_start_id": tail_id},
    )
    history = async_to_sync(HarnessService()._build_history)(
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
    _part(second, 7, type="agent", output="omit agent plan")
    _part(second, 8, type="subtask", output="omit subtask")
    HarnessMessage.objects.filter(id=first.id).update(
        created_at=timezone.now() + timedelta(days=1)
    )
    history = async_to_sync(HarnessService()._build_history)(session)
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
