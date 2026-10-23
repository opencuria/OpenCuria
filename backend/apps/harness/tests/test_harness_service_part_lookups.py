"""Characterization tests for the run-persistence reads of ``HarnessService``.

They pin the message/part lookups the service performs while persisting runner
events (tool-call matching, stream-slot closing, token baselines, socket part
positions), independent of how the queries are implemented.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.harness.harness_service import FRONTEND_EVENT_PART, HarnessService
from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)


def _session(harness_workspace) -> HarnessSession:  # type: ignore[no-untyped-def]
    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="lookups",
        agent_name="build",
        mode="build",
        model="fake-model",
    )


def _assistant(session: HarnessSession, **fields: Any) -> HarnessMessage:
    return HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="", **fields
    )


def _service(events: list[dict[str, Any]] | None = None) -> HarnessService:
    async def _emit(event: str, data: dict[str, Any]) -> None:
        if events is not None:
            events.append({"event": event, **data})

    return HarnessService(emit=_emit)


@pytest.mark.django_db
def test_last_assistant_step_tokens_reads_prior_turn(harness_workspace) -> None:
    """The newest step-finish of the previous assistant turn is the baseline."""
    session = _session(harness_workspace)
    assert HarnessService._last_assistant_step_tokens(session.id, uuid.uuid4()) == (
        0,
        0,
        0,
    )

    previous = _assistant(session)
    HarnessMessageRepository.add_usage(
        previous, prompt_tokens=7, completion_tokens=8, total_tokens=15, cost=0.0
    )
    current = _assistant(session)
    assert HarnessService._last_assistant_step_tokens(session.id, current.id) == (
        7,
        8,
        15,
    )

    for prompt in (1, 40):
        HarnessPartRepository.create(
            message_id=previous.id,
            type="step-finish",
            state="completed",
            meta={
                "tokens": {
                    "prompt_tokens": prompt,
                    "completion_tokens": 2,
                    "total_tokens": prompt + 2,
                }
            },
        )
    assert HarnessService._last_assistant_step_tokens(session.id, current.id) == (
        40,
        2,
        42,
    )
    # The excluded (current) turn never serves as its own baseline.
    assert HarnessService._last_assistant_step_tokens(session.id, previous.id) == (
        0,
        0,
        0,
    )


@pytest.mark.django_db(transaction=True)
async def test_collect_suffix_child_ids_dedupes_subtask_children(
    harness_workspace,
) -> None:
    """Only valid, unique subtask child session ids of the suffix are returned."""
    session = _session(harness_workspace)
    first = _assistant(session)
    second = _assistant(session)
    outside = _assistant(session)
    child_a, child_b = uuid.uuid4(), uuid.uuid4()
    for message, meta in (
        (first, {"child_session_id": str(child_a)}),
        (first, {"child_session_id": str(child_a)}),
        (second, {"child_session_id": "not-a-uuid"}),
        (second, {"child_session_id": str(child_b)}),
        (outside, {"child_session_id": str(uuid.uuid4())}),
    ):
        HarnessPartRepository.create(message_id=message.id, type="subtask", meta=meta)
    HarnessPartRepository.create(
        message_id=first.id, type="tool", meta={"child_session_id": str(uuid.uuid4())}
    )

    child_ids = await _service()._collect_suffix_child_ids([first.id, second.id])

    assert child_ids == [child_a, child_b]


@pytest.mark.django_db(transaction=True)
async def test_tool_part_lookup_prefers_run_context_then_call_id(
    harness_workspace,
) -> None:
    """Run-context ids win; otherwise the call id is matched in the message."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    indexed = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", call_id="other"
    )
    by_call = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", call_id="call-1"
    )
    service = _service()

    found = await service._lookup_tool_part(
        {"tool_parts": {"call-1": str(indexed.id)}}, assistant, "call-1"
    )
    assert found is not None and found.id == indexed.id
    found = await service._lookup_tool_part({}, assistant, "call-1")
    assert found is not None and found.id == by_call.id
    assert await service._lookup_tool_part({}, assistant, "missing") is None


@pytest.mark.django_db(transaction=True)
async def test_rebind_provisional_tool_part_picks_oldest_pending_match(
    harness_workspace,
) -> None:
    """A real call id adopts the oldest pending part of the same step + tool."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="running",
        input={"tool": "bash"},
        meta={"step": 1},
    )
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="pending",
        input={"tool": "read"},
        meta={"step": 1},
    )
    oldest = HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="pending",
        input={"tool": "bash"},
        meta={"step": 1},
    )
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="pending",
        input={"tool": "bash"},
        meta={"step": 1},
    )
    service = _service()

    match = await service._rebind_provisional_tool_part(
        {}, assistant, step=1, tool="bash", call_id="real"
    )
    assert match is not None and match.id == oldest.id
    assert (
        await service._rebind_provisional_tool_part(
            {}, assistant, step=2, tool="bash", call_id="real"
        )
        is None
    )


@pytest.mark.django_db(transaction=True)
async def test_finish_tool_part_adopts_queued_part_call_id(
    harness_workspace,
) -> None:
    """A completion carrying ``queued_id`` rebinds the queued row's call id."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    queued = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", state="pending", call_id="tmp-1"
    )
    service = _service()
    service._runs[str(session.id)] = {"tool_parts": {}}

    part = await service._finish_tool_part(
        assistant,
        {"call_id": "real-1", "queued_id": "tmp-1", "output": "ok"},
        state="completed",
    )

    queued.refresh_from_db()
    assert part.id == queued.id
    assert queued.call_id == "real-1"
    assert queued.state == "completed"
    assert queued.output == "ok"
    assert service._runs[str(session.id)]["tool_parts"]["real-1"] == str(queued.id)
    assert HarnessPart.objects.filter(message_id=assistant.id).count() == 1


@pytest.mark.django_db(transaction=True)
async def test_settle_open_stream_parts_only_closes_text_and_reasoning(
    harness_workspace,
) -> None:
    """Leftover streams complete; open tools and finished parts are untouched."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    text = HarnessPartRepository.create(
        message_id=assistant.id, type="text", state="running"
    )
    reasoning = HarnessPartRepository.create(
        message_id=assistant.id, type="reasoning", state="pending"
    )
    tool = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", state="running"
    )
    errored = HarnessPartRepository.create(
        message_id=assistant.id, type="text", state="error"
    )

    await _service()._settle_open_stream_parts(assistant)

    states = {
        part.id: part.state
        for part in HarnessPart.objects.filter(message_id=assistant.id)
    }
    assert states == {
        text.id: "completed",
        reasoning.id: "completed",
        tool.id: "running",
        errored.id: "error",
    }


@pytest.mark.django_db(transaction=True)
async def test_emit_frontend_adds_cached_part_position(harness_workspace) -> None:
    """Part deltas carry the persisted part position, cached per run."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    HarnessPartRepository.create(message_id=assistant.id, type="text")
    part = HarnessPartRepository.create(message_id=assistant.id, type="text")
    events: list[dict[str, Any]] = []
    service = _service(events)
    service._runs[str(session.id)] = {}
    payload = {"session_id": str(session.id), "part_id": str(part.id)}

    await service._emit_frontend(FRONTEND_EVENT_PART, dict(payload), "ws")
    HarnessPart.objects.filter(id=part.id).delete()
    await service._emit_frontend(FRONTEND_EVENT_PART, dict(payload), "ws")
    await service._emit_frontend(
        FRONTEND_EVENT_PART,
        {"session_id": str(session.id), "part_id": str(uuid.uuid4())},
        "ws",
    )

    assert [event.get("part_position") for event in events] == [1, 1, None]
