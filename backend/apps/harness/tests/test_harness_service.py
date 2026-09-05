"""Tests for M6 HarnessService: runs, double-run guard, abort."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
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
from common.exceptions import ConflictError


class FakeProvider(ProviderAdapter):
    """Scripted provider with canned steps (no network)."""

    name = "fake"

    def __init__(self, steps: list[list[Delta]]) -> None:
        self._steps = [list(step) for step in steps]

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Yield the next canned step; repeat the last when exhausted."""
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        for delta in step:
            yield delta


def _text_step(text: str) -> list[Delta]:
    return [Delta(text=text, usage=Usage(1, 1, 2))]


def _service(
    *,
    provider: FakeProvider | None = None,
    events: list[dict[str, Any]] | None = None,
) -> tuple[HarnessService, FakeProvider, list[dict[str, Any]]]:
    collected: list[dict[str, Any]] = events if events is not None else []
    fake = provider or FakeProvider([_text_step("hello")])

    async def _emit(event: str, data: dict[str, Any]) -> None:
        collected.append({"event": event, **data})

    return (
        HarnessService(
            permissions=PermissionService(
                evaluator=PermissionEvaluator(global_rules={"*": "allow"})
            ),
            emit=_emit,
            provider_factory=lambda _org: fake,
        ),
        fake,
        collected,
    )


def _session(harness_workspace) -> HarnessSession:  # type: ignore[no-untyped-def]
    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="run me",
        agent_name="build",
        mode="build",
        model="fake-model",
    )


@pytest.mark.django_db(transaction=True)
async def test_start_run_persists_messages_parts_and_history(
    harness_workspace,
) -> None:
    """Run start persists user+assistant messages and streams parts to DB."""
    service, _fake, events = _service()
    session = await _db_create_session(harness_workspace)
    history_before = HarnessMessageRepository.list_for_session(session.id)
    assert history_before == []

    assistant = await service.start_run(
        session,
        "do the thing",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    stored = HarnessMessageRepository.list_for_session(session.id)
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[0].content == "do the thing"
    assistant.refresh_from_db()
    assert assistant.content == "hello"
    assert assistant.finish == "stop"

    parts = HarnessPartRepository.list_for_session(session.id)
    assert any(p.type == "text" and "hello" in (p.output or "") for p in parts)
    assert any(p.type == "step-finish" for p in parts)

    session.refresh_from_db()
    assert session.status == "idle"
    assert session.tokens.get("total") == 2

    emitted = [e["event"] for e in events]
    assert "harness.part_updated" in emitted
    assert "harness.session_status" in emitted
    # Last status event marks the session idle again.
    assert events[-1]["event"] == "harness.session_status"
    assert events[-1]["status"] == "idle"


@pytest.mark.django_db(transaction=True)
async def test_double_run_rejected_with_conflict(harness_workspace) -> None:
    """A second start while a run is active raises ConflictError (409)."""
    gate = asyncio.Event()
    started = asyncio.Event()

    class SlowProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            started.set()
            await gate.wait()
            yield Delta(text="slow", usage=Usage(1, 1, 2))

    slow = SlowProvider([_text_step("slow")])
    service, _, _ = _service(provider=slow)
    session = await _db_create_session(harness_workspace)
    assistant_first = await service.start_run(
        session,
        "slow run",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await started.wait()
    with pytest.raises(ConflictError, match="active run"):
        await service.start_run(
            session,
            "second run",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )
    gate.set()
    await service._tasks[str(session.id)]
    assert assistant_first.id is not None


@pytest.mark.django_db(transaction=True)
async def test_abort_marks_message_and_parts(harness_workspace) -> None:
    """Abort cancels the task and marks message/parts as aborted."""
    gate = asyncio.Event()

    class HangingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            await gate.wait()
            yield Delta(text="never", usage=Usage(1, 1, 2))

    hanging = HangingProvider([])
    service, _, events = _service(provider=hanging)
    session = await _db_create_session(harness_workspace)
    await service.start_run(
        session,
        "hanging run",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await asyncio.sleep(0.05)
    assert service.is_running(session.id)
    aborted = await service.abort_run(session.id)
    assert aborted.status == "idle"
    assert not service.is_running(session.id)

    stored = HarnessMessageRepository.list_for_session(session.id)
    assistant = [m for m in stored if m.role == "assistant"][0]
    assert assistant.finish == "aborted"
    assert [e["event"] for e in events][-1] == "harness.session_status"


@pytest.mark.django_db(transaction=True)
async def test_history_built_from_db_not_params(harness_workspace) -> None:
    """Follow-up runs include earlier persisted turns as history."""
    seen: list[dict[str, Any]] = []

    class RecordingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            seen.append(
                {
                    "roles": [m.role for m in messages],
                    "texts": [m.content for m in messages],
                }
            )
            yield Delta(text="ok", usage=Usage(1, 1, 2))

    recording = RecordingProvider([_text_step("ok")])
    service, _, _ = _service(provider=recording)
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    await service.start_run(
        session,
        "second",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    assert len(seen) == 2
    # Second run history contains the first persisted user turn.
    assert "first" in json.dumps(seen[1]["texts"])


async def _db_create_session(harness_workspace):  # type: ignore[no-untyped-def]
    """Create a session row from async test context."""
    import os

    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="svc",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
