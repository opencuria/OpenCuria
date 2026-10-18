"""Live steering: queue semantics, native loop injection, service lifecycle."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.harness_service import (
    FRONTEND_EVENT_PART,
    FRONTEND_EVENT_STEER_QUEUE,
    HarnessService,
)
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import (
    ChatOptions,
    Delta,
    LLMMessage,
    ProviderAdapter,
    ToolSchema,
    Usage,
)
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.steering import (
    MAX_PENDING_STEERS,
    NO_ACTIVE_RUN_CODE,
    SteeringChannel,
)
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import default_tool_registry
from common.exceptions import ConflictError, NotFoundError

# -- channel ----------------------------------------------------------------


def _channel() -> tuple[SteeringChannel, list[list[str]]]:
    batches: list[list[str]] = []

    async def deliver(entries: list[Any]) -> None:
        batches.append([entry.content for entry in entries])

    return SteeringChannel(deliver=deliver), batches


def test_submit_trims_validates_and_orders() -> None:
    channel, _ = _channel()
    first = channel.submit("  first  ", user_id=7)
    channel.submit("second")
    assert first.content == "first"
    assert first.user_id == 7
    assert [entry.content for entry in channel.pending()] == ["first", "second"]
    with pytest.raises(ValueError, match="empty"):
        channel.submit("   ")
    with pytest.raises(ValueError, match="exceeds"):
        channel.submit("x" * 120_000)


def test_submit_rejects_when_closed_or_full() -> None:
    channel, _ = _channel()
    for index in range(MAX_PENDING_STEERS):
        channel.submit(f"m{index}")
    with pytest.raises(ConflictError) as full:
        channel.submit("one too many")
    assert full.value.code == "steer_queue_full"
    assert len(channel.close()) == MAX_PENDING_STEERS
    with pytest.raises(ConflictError) as closed:
        channel.submit("late")
    assert closed.value.code == NO_ACTIVE_RUN_CODE
    assert channel.closed


def test_withdraw_only_removes_pending_entries() -> None:
    channel, _ = _channel()
    entry = channel.submit("oops")
    assert channel.withdraw("unknown") is None
    assert channel.withdraw(entry.id) == entry
    assert channel.withdraw(entry.id) is None
    assert channel.pending() == []


async def test_take_delivers_everything_in_one_batch() -> None:
    channel, batches = _channel()
    channel.submit("a")
    channel.submit("b")
    taken = await channel.take()
    assert [entry.content for entry in taken] == ["a", "b"]
    assert batches == [["a", "b"]]
    assert await channel.take() == []


async def test_take_requeues_entries_when_delivery_fails() -> None:
    async def broken(_entries: list[Any]) -> None:
        raise RuntimeError("db down")

    channel = SteeringChannel(deliver=broken)
    channel.submit("keep me")
    with pytest.raises(RuntimeError):
        await channel.take()
    assert [entry.content for entry in channel.pending()] == ["keep me"]


async def test_next_waits_for_an_entry_then_stops_after_end_feed() -> None:
    channel, batches = _channel()
    waiter = asyncio.create_task(channel.next())
    await asyncio.sleep(0)
    assert not waiter.done()
    channel.submit("hello")
    entry = await asyncio.wait_for(waiter, timeout=1)
    assert entry is not None and entry.content == "hello"
    assert batches == [["hello"]]

    waiter = asyncio.create_task(channel.next())
    await asyncio.sleep(0)
    channel.end_feed()
    assert await asyncio.wait_for(waiter, timeout=1) is None
    # Entries arriving after the feed ended stay pending for the follow-up.
    channel.submit("too late")
    assert await channel.next() is None
    assert await channel.take() == []
    assert [entry.content for entry in channel.pending()] == ["too late"]


# -- native runner ----------------------------------------------------------


class ScriptedProvider(ProviderAdapter):
    """Plays canned steps and runs a hook before each model call."""

    name = "fake"

    def __init__(self, steps: list[list[Delta]], before_call=None) -> None:  # type: ignore[no-untyped-def]
        self._steps = [list(step) for step in steps]
        self._before_call = before_call
        self.seen: list[list[LLMMessage]] = []

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        index = len(self.seen)
        self.seen.append(list(messages))
        if self._before_call is not None:
            await self._before_call(index)
        step = self._steps.pop(0) if self._steps else [Delta(text="done")]
        for delta in step:
            yield delta


def _read_step(call_id: str = "c1") -> list[Delta]:
    return [
        Delta(
            tool_calls=(
                {
                    "index": 0,
                    "id": call_id,
                    "name": "read",
                    "arguments": json.dumps({"path": "a.txt"}),
                },
            ),
            usage=Usage(2, 3, 5),
        )
    ]


def _answer_step(text: str) -> list[Delta]:
    return [Delta(text=text, usage=Usage(1, 1, 2))]


def _runner(provider: ScriptedProvider) -> HarnessRunner:
    async def emit(_event: dict[str, Any]) -> None:
        return None

    return HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        evaluator=PermissionEvaluator(global_rules={"*": "allow"}),
        accessor=FakeAccessor(files={"/workspace/a.txt": b"hi"}),
        emit=emit,
    )


async def test_steer_joins_the_conversation_after_the_tool_results() -> None:
    channel, batches = _channel()

    async def steer_during_first_step(index: int) -> None:
        if index == 0:
            channel.submit("also check b.txt")

    provider = ScriptedProvider(
        [_read_step(), _answer_step("all done")], before_call=steer_during_first_step
    )
    result = await _runner(provider).run(
        "go", "build", "m", "build", RunOptions(steering=channel)
    )
    assert result.output == "all done"
    assert batches == [["also check b.txt"]]
    second_call = provider.seen[1]
    assert [message.role for message in second_call[-3:]] == [
        "assistant",
        "tool",
        "user",
    ]
    assert second_call[-1].content == "also check b.txt"


async def test_steer_during_the_final_answer_keeps_the_turn_going() -> None:
    channel, _ = _channel()

    async def steer_during_answer(index: int) -> None:
        if index == 0:
            channel.submit("one more thing")

    provider = ScriptedProvider(
        [_answer_step("first answer"), _answer_step("second answer")],
        before_call=steer_during_answer,
    )
    result = await _runner(provider).run(
        "go", "build", "m", "build", RunOptions(steering=channel)
    )
    assert result.steps == 2
    assert result.output == "second answer"
    roles = [message.role for message in provider.seen[1][-2:]]
    assert roles == ["assistant", "user"]
    assert provider.seen[1][-1].content == "one more thing"
    assert channel.pending() == []


async def test_without_a_channel_the_loop_is_unchanged() -> None:
    provider = ScriptedProvider([_read_step(), _answer_step("done")])
    result = await _runner(provider).run("go", "build", "m", "build", RunOptions())
    assert result.steps == 2
    assert all(message.role != "user" for message in provider.seen[1][2:])


# -- service ----------------------------------------------------------------


def _service(
    provider: ScriptedProvider,
) -> tuple[HarnessService, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []

    async def emit(event: str, data: dict[str, Any]) -> None:
        events.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=emit,
        provider_factory=lambda _org: provider,
    )
    return service, events


def _new_session(workspace):  # type: ignore[no-untyped-def]
    return HarnessSessionRepository.create(
        workspace_id=workspace.id,
        organization_id=workspace.runner.organization_id,
        title="steer me",
        agent_name="build",
        mode="build",
        model="fake-model",
    )


def _queue_snapshots(events: list[dict[str, Any]]) -> list[list[str]]:
    return [
        [steer["content"] for steer in event["steers"]]
        for event in events
        if event["event"] == FRONTEND_EVENT_STEER_QUEUE
    ]


async def _start(service: HarnessService, session, workspace) -> None:  # type: ignore[no-untyped-def]
    await service.start_run(
        session,
        "start the work",
        organization_id=workspace.runner.organization_id,
        workspace_id=str(workspace.id),
        user_id=1,
    )


@pytest.mark.django_db(transaction=True)
async def test_steer_is_persisted_in_order_and_seen_by_the_model(
    harness_workspace,
) -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    async def hold_first_call(index: int) -> None:
        if index == 0:
            started.set()
            await gate.wait()

    provider = ScriptedProvider(
        [_read_step(), _answer_step("finished")], before_call=hold_first_call
    )
    service, events = _service(provider)
    session = _new_session(harness_workspace)
    await _start(service, session, harness_workspace)
    await asyncio.wait_for(started.wait(), timeout=2)

    entry = await service.steer_run(session, "  prefer tabs  ", user_id=1)
    assert entry.content == "prefer tabs"
    assert [item.content for item in service.list_pending_steers(session.id)] == [
        "prefer tabs"
    ]
    gate.set()
    await service._tasks[str(session.id)]

    # The model saw it after the tool result, before its next answer.
    assert provider.seen[1][-1].role == "user"
    assert provider.seen[1][-1].content == "prefer tabs"

    assistant = HarnessMessageRepository.list_for_session(session.id)[-1]
    parts = HarnessPartRepository.list_for_session(session.id)
    kinds = [part.type for part in parts if part.message_id == assistant.id]
    steer_index = kinds.index("steer")
    assert "tool" in kinds[:steer_index]
    assert "text" in kinds[steer_index + 1 :]
    steer_part = next(part for part in parts if part.type == "steer")
    assert steer_part.output == "prefer tabs"
    assert steer_part.state == "completed"
    assert steer_part.call_id == entry.id
    # The steer is not part of the assistant's own answer.
    assert "prefer tabs" not in (assistant.content or "")

    delivered = [
        event
        for event in events
        if event["event"] == FRONTEND_EVENT_PART
        and event["delta"].get("steer") == "prefer tabs"
    ]
    assert len(delivered) == 1
    assert delivered[0]["delta"]["steer_id"] == entry.id
    assert delivered[0]["part_id"] == str(steer_part.id)
    assert _queue_snapshots(events) == [["prefer tabs"], []]

    # Replay for the next turn keeps the interjection between the answers.
    history = await service._build_history(session)
    assert [message.role for message in history] == [
        "user",
        "assistant",
        "tool",
        "user",
        "assistant",
    ]
    assert history[3].content == "prefer tabs"


@pytest.mark.django_db(transaction=True)
async def test_steer_requires_an_active_run_and_a_root_session(
    harness_workspace,
) -> None:
    service, _ = _service(ScriptedProvider([_answer_step("hi")]))
    session = _new_session(harness_workspace)
    with pytest.raises(ConflictError) as idle:
        await service.steer_run(session, "hello?")
    assert idle.value.code == NO_ACTIVE_RUN_CODE

    child = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="child",
        agent_name="general",
        mode="build",
        model="fake-model",
        parent_id=session.id,
    )
    with pytest.raises(ValueError, match="subagent"):
        await service.steer_run(child, "hello?")


@pytest.mark.django_db(transaction=True)
async def test_withdrawn_steer_never_reaches_the_model(harness_workspace) -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    async def hold_first_call(index: int) -> None:
        if index == 0:
            started.set()
            await gate.wait()

    provider = ScriptedProvider(
        [_read_step(), _answer_step("finished")], before_call=hold_first_call
    )
    service, events = _service(provider)
    session = _new_session(harness_workspace)
    await _start(service, session, harness_workspace)
    await asyncio.wait_for(started.wait(), timeout=2)

    entry = await service.steer_run(session, "never mind", user_id=1)
    withdrawn = await service.withdraw_steer(session, entry.id)
    assert withdrawn.content == "never mind"
    with pytest.raises(NotFoundError):
        await service.withdraw_steer(session, entry.id)
    gate.set()
    await service._tasks[str(session.id)]

    assert all(message.content != "never mind" for message in provider.seen[1])
    assert not [
        part
        for part in HarnessPartRepository.list_for_session(session.id)
        if part.type == "steer"
    ]
    assert _queue_snapshots(events) == [["never mind"], []]


@pytest.mark.django_db(transaction=True)
async def test_steer_that_arrives_too_late_starts_a_follow_up_run(
    harness_workspace,
) -> None:
    session = _new_session(harness_workspace)

    async def end_feed_then_steer(index: int) -> None:
        if index == 0:
            # Simulates a message landing once the engine stopped reading.
            service._steering_channel(session.id).end_feed()
            await service.steer_run(session, "after the fact", user_id=1)

    provider = ScriptedProvider(
        [_answer_step("first"), _answer_step("second")],
        before_call=end_feed_then_steer,
    )
    service, events = _service(provider)
    await _start(service, session, harness_workspace)
    await service._tasks[str(session.id)]

    async def settled() -> bool:
        refreshed = HarnessSessionRepository.get_by_id(session.id)
        users = [
            message
            for message in HarnessMessageRepository.list_for_session(session.id)
            if message.role == "user"
        ]
        return (
            refreshed is not None
            and refreshed.status == "idle"
            and len(users) == 2
            and not service.is_running(session.id)
            and _queue_snapshots(events)[-1] == []
        )

    for _ in range(150):
        if await settled():
            break
        await asyncio.sleep(0.02)
    assert await settled()
    users = [
        message.content
        for message in HarnessMessageRepository.list_for_session(session.id)
        if message.role == "user"
    ]
    assert users == ["start the work", "after the fact"]
    # The follow-up is a regular run: the model saw the message as a new prompt.
    assert provider.seen[1][-1].content == "after the fact"


@pytest.mark.django_db(transaction=True)
async def test_stop_discards_queued_steers_without_a_follow_up(
    harness_workspace,
) -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    async def hold_first_call(index: int) -> None:
        started.set()
        await gate.wait()

    provider = ScriptedProvider([_answer_step("never")], before_call=hold_first_call)
    service, events = _service(provider)
    session = _new_session(harness_workspace)
    await _start(service, session, harness_workspace)
    await asyncio.wait_for(started.wait(), timeout=2)
    await service.steer_run(session, "queued", user_id=1)

    await service.abort_run(session.id)
    await asyncio.sleep(0.1)

    assert _queue_snapshots(events)[-1] == []
    users = [
        message
        for message in HarnessMessageRepository.list_for_session(session.id)
        if message.role == "user"
    ]
    assert [message.content for message in users] == ["start the work"]
    assert not service.is_running(session.id)
    with pytest.raises(ConflictError):
        await service.steer_run(session, "after stop")


@pytest.mark.django_db(transaction=True)
def test_timeline_carries_the_full_steer_text(harness_workspace) -> None:
    from apps.harness.timeline import TIMELINE_TEXT_PART_TYPES, timeline_part_payload

    session = _new_session(harness_workspace)
    assistant = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content=""
    )
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="steer",
        state="completed",
        call_id="steer-1",
        output="use the staging database",
        meta={"steer_id": "steer-1"},
    )
    [part] = HarnessPartRepository.list_timeline_for_session(session.id)
    part.message_position = 0
    payload = timeline_part_payload(
        part,
        text_output=part.timeline_output
        if part.type in TIMELINE_TEXT_PART_TYPES
        else None,
    )
    assert payload["type"] == "steer"
    assert payload["output"] == "use the staging database"
    assert payload["detail_loaded"] is True
    assert payload["display"]["steer_id"] == "steer-1"


@pytest.mark.django_db(transaction=True)
async def test_unconfirmed_cleanup_discards_the_queue_and_tells_clients(
    harness_workspace,
) -> None:
    service, events = _service(ScriptedProvider([_answer_step("unused")]))
    session = _new_session(harness_workspace)
    key = str(session.id)
    channel, _ = _channel()
    channel.submit("will never be delivered")
    service._runs[key] = {
        "steering": channel,
        "workspace_id": str(harness_workspace.id),
    }

    service._drop_run_tracking(key)
    await asyncio.sleep(0.05)

    assert channel.closed and channel.pending() == []
    assert key not in service._runs
    assert _queue_snapshots(events) == [[]]
    # No follow-up run: the cleanup of the previous run is unconfirmed.
    assert not service.is_running(session.id)
