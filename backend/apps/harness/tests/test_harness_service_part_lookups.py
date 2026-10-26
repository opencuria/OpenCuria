"""Characterization tests for the run-persistence reads of ``HarnessService``.

They pin the message/part lookups the service performs around a run (token
baselines, edit-suffix children, socket part positions), independent of how
the queries are implemented. Event persistence reads live in
``test_run_timeline``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from apps.harness.run_timeline import FRONTEND_EVENT_PART


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
