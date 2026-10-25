"""Streaming durability, event ordering and bounded database work per delta."""

from __future__ import annotations

import asyncio
import threading
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext

from apps.harness.harness_service import FRONTEND_EVENT_PART, HarnessService
from apps.harness.models import HarnessMessage, HarnessPart
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)


@pytest.fixture(autouse=True)
async def close_worker_connections(transactional_db: None) -> AsyncIterator[None]:
    """Close the async tests' thread-local connections before database teardown."""
    yield
    await sync_to_async(connections.close_all)()


def _stream(workspace: Any) -> tuple[Any, HarnessMessage, HarnessPart, HarnessService]:
    session = HarnessSessionRepository.create(
        workspace_id=workspace.id, organization_id=workspace.runner.organization_id
    )
    message = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="existing "
    )
    part = HarnessPartRepository.create(
        message_id=message.id,
        type="text",
        state="running",
        output=message.content,
        meta={"step": 3},
    )
    service = HarnessService()
    service._runs[str(session.id)] = {"text_part_id": str(part.id)}
    return session, message, part, service


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("chunks", [1, 50])
def test_text_stream_uses_two_writes_and_one_commit_per_delta(
    harness_workspace: Any, chunks: int
) -> None:
    """Text output changes never require reading its growing output or display."""
    session, message, part, service = _stream(harness_workspace)
    display = dict(part.display)
    emitted = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        emitted.append(data)

    service._emit = emit
    service._runs[str(session.id)]["part_positions"] = {str(part.id): part.position}

    async def stream() -> None:
        for _ in range(chunks):
            await service._on_runner_event(
                session, message, {"type": "part_updated", "delta": {"text": "new "}}
            )

    with CaptureQueriesContext(connection) as queries:
        async_to_sync(stream)()
    statements = Counter(query["sql"].split()[0].upper() for query in queries)
    assert statements["SELECT"] == 0
    assert statements["UPDATE"] == 2 * chunks
    assert statements["COMMIT"] == chunks
    updates = [query["sql"] for query in queries if query["sql"].startswith("UPDATE")]
    assert all('"harness_message"' in sql for sql in updates[::2])
    assert all('"harness_part"' in sql for sql in updates[1::2])
    message.refresh_from_db()
    part.refresh_from_db()
    assert message.content == part.output == "existing " + "new " * chunks
    assert part.display == display
    assert len(emitted) == chunks
    assert all(data["part_id"] == str(part.id) for data in emitted)


@pytest.mark.django_db(transaction=True)
async def test_stream_is_committed_before_emit_and_preserves_kind_boundaries(
    harness_workspace: Any,
) -> None:
    """Every forwarded delta is already durable; reasoning stays out of content."""
    session, message, part, service = await sync_to_async(_stream)(harness_workspace)
    observed = []

    def snapshot(part_id: str) -> tuple[str, str, bool]:
        return (
            HarnessMessage.objects.get(id=message.id).content,
            HarnessPart.objects.get(id=part_id).output,
            connection.in_atomic_block,
        )

    async def emit(event: str, data: dict[str, Any]) -> None:
        assert event == FRONTEND_EVENT_PART
        observed.append(await sync_to_async(snapshot)(data["part_id"]))

    service._emit = emit
    for delta in ({"text": "one"}, {"reasoning": "think\n"}, {"text": "two"}):
        await service._on_runner_event(
            session, message, {"type": "part_updated", "delta": delta, "step": 3}
        )
    assert observed == [
        ("existing one", "existing one", False),
        ("existing one", "think\n", False),
        ("existing onetwo", "two", False),
    ]
    parts = await sync_to_async(HarnessPartRepository.list_for_message)(message.id)
    assert [(row.type, row.state) for row in parts] == [
        ("text", "completed"),
        ("reasoning", "completed"),
        ("text", "running"),
    ]
    assert parts[0].id == part.id
    assert parts[1].display["summary"] == "think"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failed_table", ["harness_message", "harness_part"])
def test_text_stream_failure_rolls_back_both_writes(
    harness_workspace: Any, failed_table: str
) -> None:
    """A failed message write leaves neither a partial delta nor an emitted event."""
    session, message, part, service = _stream(harness_workspace)
    emitted = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        emitted.append(data)

    def fail_write(
        execute: Any, sql: str, params: Any, many: bool, context: Any
    ) -> Any:
        result = execute(sql, params, many, context)
        if sql.startswith("UPDATE") and f'"{failed_table}"' in sql:
            raise RuntimeError("synthetic write failure")
        return result

    service._emit = emit
    with connection.execute_wrapper(fail_write):
        with pytest.raises(RuntimeError, match="synthetic write failure"):
            async_to_sync(service._on_runner_event)(
                session, message, {"type": "part_updated", "delta": {"text": "lost"}}
            )
    assert message.content == "existing "
    message.refresh_from_db()
    part.refresh_from_db()
    assert message.content == part.output == "existing "
    assert not emitted


@pytest.mark.django_db(transaction=True)
async def test_cancel_during_text_write_keeps_part_and_message_consistent(
    harness_workspace: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation cannot land between independently committed text writes."""
    session, message, part, service = await sync_to_async(_stream)(harness_workspace)
    entered, release = threading.Event(), threading.Event()
    original_save = HarnessMessage.save
    emitted = []

    def paused_save(self: HarnessMessage, *args: Any, **kwargs: Any) -> None:
        entered.set()
        assert release.wait(timeout=5)
        original_save(self, *args, **kwargs)

    async def emit(event: str, data: dict[str, Any]) -> None:
        emitted.append(data)

    service._emit = emit
    monkeypatch.setattr(HarnessMessage, "save", paused_save)
    task = asyncio.create_task(
        service._on_runner_event(
            session, message, {"type": "part_updated", "delta": {"text": "tail"}}
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
    finally:
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
    # Queue a read on the same thread-sensitive worker, after its write completes.
    await sync_to_async(message.refresh_from_db)()
    await sync_to_async(part.refresh_from_db)()
    assert message.content == part.output == "existing tail"
    assert not emitted


@pytest.mark.django_db(transaction=True)
async def test_concurrent_text_events_remain_ordered(harness_workspace: Any) -> None:
    """The existing session event lock still serializes concurrent producers."""
    session, message, part, service = await sync_to_async(_stream)(harness_workspace)
    emitted = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        emitted.append(data["delta"]["text"])

    service._emit = emit
    deltas = [f"{index}," for index in range(30)]
    await asyncio.gather(
        *[
            service._on_runner_event(
                session, message, {"type": "part_updated", "delta": {"text": delta}}
            )
            for delta in deltas
        ]
    )
    await sync_to_async(message.refresh_from_db)()
    await sync_to_async(part.refresh_from_db)()
    assert message.content == part.output == "existing " + "".join(deltas)
    assert emitted == deltas
