"""Tests for fork_session and edit_user_message (service level).

Covers the M6 fork/edit flows with the FakeProvider pattern from
``test_harness_service.py``: no network, deterministic scripted steps.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async

from apps.harness.harness_service import HarnessService, _forked_title
from apps.harness.models import Todo
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.models import PermissionRequest
from apps.harness.permissions.service import (
    PermissionRequestRepository,
    PermissionService,
)
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
    QuestionRequestRepository,
    TodoRepository,
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
        """Yield the next canned step; then a text-only done reply."""
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0)
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
    fake = provider or FakeProvider([_text_step("hello")] * 10)

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


async def _db_create_session(harness_workspace):  # type: ignore[no-untyped-def]
    """Create a session row from async test context."""
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="svc",
        agent_name="build",
        mode="build",
        model="fake-model",
    )


async def _run_two_turns(service, session, org_id, ws_id) -> None:  # type: ignore[no-untyped-def]
    """Start two sequential runs (each awaited) to build history."""
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(ws_id),
    )
    await service._tasks[str(session.id)]
    await service.start_run(
        session,
        "second",
        organization_id=org_id,
        workspace_id=str(ws_id),
    )
    await service._tasks[str(session.id)]


# -- fork title helper -----------------------------------------------------


def test_forked_title_increments() -> None:
    """Fork titles increment the ``(fork #N)`` suffix like OpenCode."""
    assert _forked_title("Chat") == "Chat (fork #1)"
    assert _forked_title("Chat (fork #1)") == "Chat (fork #2)"
    assert _forked_title("Chat (fork #9)") == "Chat (fork #10)"


# -- fork ------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_fork_copies_prefix_exclusive_with_new_ids(harness_workspace) -> None:
    """Fork at a user message excludes the cutoff and later turns."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)

    stored = HarnessMessageRepository.list_for_session(session.id)
    assert [m.role for m in stored] == ["user", "assistant", "user", "assistant"]
    cutoff = next(m for m in stored if m.content == "second")

    forked = await service.fork_session(session.id, cutoff.id)

    assert forked.id != session.id
    assert forked.parent_id is None
    assert forked.title == "svc (fork #1)"
    forked_msgs = HarnessMessageRepository.list_for_session(forked.id)
    assert [m.role for m in forked_msgs] == ["user", "assistant"]
    assert [m.content for m in forked_msgs] == ["first", "hello"]
    # New UUIDs, no shared rows with the source session.
    assert {m.id for m in forked_msgs}.isdisjoint({m.id for m in stored})
    # The source session still has the full history.
    assert len(HarnessMessageRepository.list_for_session(session.id)) == 4


@pytest.mark.django_db(transaction=True)
async def test_fork_copies_parts_and_clears_child_session_id(
    harness_workspace,
) -> None:
    """Forked messages carry parts, but subtask child links are cleared."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    stored = HarnessMessageRepository.list_for_session(session.id)
    assistant = next(m for m in stored if m.role == "assistant")
    child_id = uuid.uuid4()
    await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="subtask",
        state="completed",
        title="Explore",
        meta={"subtask_id": "sub-1", "child_session_id": str(child_id)},
    )

    # Anchor cutoff: a fresh user message after the first turn.
    anchor = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="anchor"
    )
    forked = await service.fork_session(session.id, anchor.id)
    forked_parts = HarnessPartRepository.list_for_session(forked.id)
    assert any(p.type == "text" for p in forked_parts)
    subtask = [p for p in forked_parts if p.type == "subtask"]
    assert len(subtask) == 1
    assert subtask[0].meta.get("subtask_id") == "sub-1"
    assert subtask[0].meta.get("child_session_id") == ""
    # New part rows (no shared IDs with the source session).
    src_parts = HarnessPartRepository.list_for_session(session.id)
    assert {p.id for p in forked_parts}.isdisjoint({p.id for p in src_parts})


@pytest.mark.django_db(transaction=True)
async def test_fork_full_copy_without_message_id(harness_workspace) -> None:
    """Omitting message_id copies the whole history."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)

    forked = await service.fork_session(session.id)
    assert len(HarnessMessageRepository.list_for_session(forked.id)) == 4


@pytest.mark.django_db(transaction=True)
async def test_fork_title_increment_and_fresh_usage(harness_workspace) -> None:
    """Fork titles increment; cost/tokens start fresh."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    await sync_to_async(HarnessSessionRepository.add_usage)(
        session,
        cost=1.25,
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
    )
    first = await service.fork_session(session.id)
    first.refresh_from_db()
    assert first.title == "svc (fork #1)"
    assert first.cost == 0.0
    assert dict(first.tokens or {}) == {}

    session.refresh_from_db()
    second = await service.fork_session(session.id)
    assert second.title == "svc (fork #1)"
    nested = await service.fork_session(first.id)
    assert nested.title == "svc (fork #2)"


@pytest.mark.django_db(transaction=True)
async def test_fork_does_not_copy_todos_and_gates(harness_workspace) -> None:
    """Todos and session gates are scoped per session; forks start empty."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await sync_to_async(Todo.objects.create)(
        session=session, content="ship it", status="pending", order=0
    )
    permission = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="reboot",
        title="$ reboot",
    )
    question = await sync_to_async(QuestionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        questions=[{"question": "Continue?"}],
    )

    forked = await service.fork_session(session.id)
    assert TodoRepository.list_for_session(forked.id) == []
    assert len(TodoRepository.list_for_session(session.id)) == 1
    pending = await sync_to_async(PermissionRequestRepository.list_pending_for_session)(
        forked.id
    )
    assert pending == []
    pending_q = await sync_to_async(QuestionRequestRepository.list_pending_for_session)(
        forked.id
    )
    assert pending_q == []
    # Source gates are untouched (fork rejects nothing, deletes nothing).
    assert permission.id is not None and question.id is not None
    assert (
        await sync_to_async(PermissionRequest.objects.get)(id=permission.id)
    ).status == "pending"


@pytest.mark.django_db(transaction=True)
async def test_fork_remaps_compaction_tail_start_id(harness_workspace) -> None:
    """Compaction ``tail_start_id`` points at the copied message in forks."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)
    stored = HarnessMessageRepository.list_for_session(session.id)
    tail_msg = stored[0]
    assistant = stored[1]
    await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="compaction",
        state="completed",
        output="summary",
        meta={"tail_start_id": str(tail_msg.id)},
    )
    anchor = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="anchor"
    )

    forked = await service.fork_session(session.id, anchor.id)
    forked_parts = [
        p
        for p in HarnessPartRepository.list_for_session(forked.id)
        if p.type == "compaction"
    ]
    assert len(forked_parts) == 1
    tail_raw = str(forked_parts[0].meta.get("tail_start_id", ""))
    mapped = uuid.UUID(tail_raw)
    forked_ids = {m.id for m in HarnessMessageRepository.list_for_session(forked.id)}
    assert mapped in forked_ids
    assert mapped != tail_msg.id


@pytest.mark.django_db(transaction=True)
async def test_fork_drops_compaction_tail_start_id_outside_prefix(
    harness_workspace,
) -> None:
    """``tail_start_id`` outside the copied prefix is dropped, not dangling."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    stored = HarnessMessageRepository.list_for_session(session.id)
    anchor = stored[0]
    assistant = stored[1]
    # Point the checkpoint at a message that will NOT be copied.
    outside = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="outside"
    )
    await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="compaction",
        state="completed",
        output="summary",
        meta={"tail_start_id": str(outside.id)},
    )

    forked = await service.fork_session(session.id, anchor.id)
    # Prefix before the very first message is empty.
    assert HarnessMessageRepository.list_for_session(forked.id) == []
    assert HarnessPartRepository.list_for_session(forked.id) == []

    full = await service.fork_session(session.id)
    compactions = [
        p
        for p in HarnessPartRepository.list_for_session(full.id)
        if p.type == "compaction"
    ]
    assert len(compactions) == 1
    # Outside message was copied in the full fork, so the id is remapped.
    assert str(compactions[0].meta.get("tail_start_id", "") or "").strip() != ""
    assert uuid.UUID(str(compactions[0].meta["tail_start_id"])) != outside.id


@pytest.mark.django_db(transaction=True)
async def test_fork_works_while_source_is_busy(harness_workspace) -> None:
    """Forking does not raise ConflictError while the source runs."""
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
    await service.start_run(
        session,
        "slow run",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert service.is_running(session.id)
    try:
        forked = await service.fork_session(session.id)
    finally:
        gate.set()
        await service._tasks[str(session.id)]
    assert forked.id != session.id


@pytest.mark.django_db(transaction=True)
async def test_fork_child_session_raises(harness_workspace) -> None:
    """Forking a subagent child session is rejected like prompting it."""
    service, _, _ = _service()
    parent = await _db_create_session(harness_workspace)
    child = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="child",
        agent_name="general",
        mode="build",
        model="fake-model",
        parent_id=parent.id,
    )
    with pytest.raises(ValueError, match="subagent"):
        await service.fork_session(child.id)


@pytest.mark.django_db(transaction=True)
async def test_fork_unknown_message_id_raises(harness_workspace) -> None:
    """Forking at a message from another session raises ValueError."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    other = await _db_create_session(harness_workspace)
    orphan = await sync_to_async(HarnessMessageRepository.create)(
        session_id=other.id, role="user", content="elsewhere"
    )
    with pytest.raises(ValueError, match="not in session"):
        await service.fork_session(session.id, orphan.id)


# -- edit ------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
async def test_edit_truncates_suffix_and_reruns(harness_workspace) -> None:
    """Editing the first prompt drops later turns and starts a new run."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)

    stored = HarnessMessageRepository.list_for_session(session.id)
    first_user = next(m for m in stored if m.content == "first")
    await service.edit_user_message(
        session.id,
        message_id=first_user.id,
        prompt="first edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    current = HarnessMessageRepository.list_for_session(session.id)
    assert [m.content for m in current if m.role == "user"] == ["first edited"]
    assert current[-1].role == "assistant"
    assert current[-1].content == "hello"


@pytest.mark.django_db(transaction=True)
async def test_edit_busy_raises_conflict(harness_workspace) -> None:
    """Editing while a run is active raises ConflictError (REST 409)."""
    gate = asyncio.Event()
    started = asyncio.Event()

    class SlowProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            started.set()
            await gate.wait()
            yield Delta(text="slow", usage=Usage(1, 1, 2))

    service, _, _ = _service(provider=SlowProvider([_text_step("slow")]))
    session = await _db_create_session(harness_workspace)
    await service.start_run(
        session,
        "slow run",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    user_msg = next(
        m
        for m in HarnessMessageRepository.list_for_session(session.id)
        if m.role == "user"
    )
    try:
        with pytest.raises(ConflictError, match="active run"):
            await service.edit_user_message(
                session.id,
                message_id=user_msg.id,
                prompt="edited",
                organization_id=harness_workspace.runner.organization_id,
                workspace_id=str(harness_workspace.id),
            )
    finally:
        gate.set()
        await service._tasks[str(session.id)]


@pytest.mark.django_db(transaction=True)
async def test_edit_assistant_message_raises(harness_workspace) -> None:
    """Only user messages can be edited."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    assistant = next(
        m
        for m in HarnessMessageRepository.list_for_session(session.id)
        if m.role == "assistant"
    )
    with pytest.raises(ValueError, match="Only user messages"):
        await service.edit_user_message(
            session.id,
            message_id=assistant.id,
            prompt="nope",
            organization_id=org_id,
            workspace_id=str(harness_workspace.id),
        )


@pytest.mark.django_db(transaction=True)
async def test_edit_empty_prompt_raises(harness_workspace) -> None:
    """Blank edit prompts are rejected before any truncation."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    user_msg = next(
        m
        for m in HarnessMessageRepository.list_for_session(session.id)
        if m.role == "user"
    )
    with pytest.raises(ValueError, match="must not be empty"):
        await service.edit_user_message(
            session.id,
            message_id=user_msg.id,
            prompt="   ",
            organization_id=org_id,
            workspace_id=str(harness_workspace.id),
        )
    # Nothing was truncated.
    assert len(HarnessMessageRepository.list_for_session(session.id)) == 2


@pytest.mark.django_db(transaction=True)
async def test_edit_unknown_message_raises_not_found(harness_workspace) -> None:
    """Editing a message id outside the session raises NotFoundError."""
    from common.exceptions import NotFoundError

    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    with pytest.raises(NotFoundError):
        await service.edit_user_message(
            session.id,
            message_id=uuid.uuid4(),
            prompt="edited",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )


@pytest.mark.django_db(transaction=True)
async def test_edit_child_session_raises(harness_workspace) -> None:
    """Editing inside a subagent child session is rejected."""
    service, _, _ = _service()
    parent = await _db_create_session(harness_workspace)
    child = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="child",
        agent_name="general",
        mode="build",
        model="fake-model",
        parent_id=parent.id,
    )
    child_msg = await sync_to_async(HarnessMessageRepository.create)(
        session_id=child.id, role="user", content="child prompt"
    )
    with pytest.raises(ValueError, match="subagent"):
        await service.edit_user_message(
            child.id,
            message_id=child_msg.id,
            prompt="edited",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )


@pytest.mark.django_db(transaction=True)
async def test_edit_rejects_suffix_permission_and_question(harness_workspace) -> None:
    """Pending gates in the dropped suffix end up rejected."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)

    stored = HarnessMessageRepository.list_for_session(session.id)
    first_user = next(m for m in stored if m.content == "first")
    second_user = next(m for m in stored if m.content == "second")
    keeper = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="keep",
        title="$ keep",
        message_id=first_user.id,
    )
    suffix_perm = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="drop",
        title="$ drop",
        message_id=second_user.id,
    )
    unscoped_perm = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="unscoped",
        title="$ unscoped",
    )
    suffix_question = await sync_to_async(QuestionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        questions=[{"question": "Suffix?"}],
        message_id=second_user.id,
    )
    prefix_question = await sync_to_async(QuestionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        questions=[{"question": "Prefix?"}],
        message_id=first_user.id,
    )
    unscoped_question = await sync_to_async(QuestionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        questions=[{"question": "Any?"}],
    )

    await service.edit_user_message(
        session.id,
        message_id=second_user.id,
        prompt="second edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    # The edit target is part of the suffix: both prefix + unscoped gates
    # linked to it (or unscoped) are rejected.
    assert (
        await sync_to_async(PermissionRequest.objects.get)(id=keeper.id)
    ).status == "pending"
    assert (
        await sync_to_async(PermissionRequest.objects.get)(id=suffix_perm.id)
    ).status == "rejected"
    assert (
        await sync_to_async(PermissionRequest.objects.get)(id=unscoped_perm.id)
    ).status == "rejected"
    from apps.harness.models import QuestionRequest

    assert (
        await sync_to_async(QuestionRequest.objects.get)(id=suffix_question.id)
    ).status == "rejected"
    assert (
        await sync_to_async(QuestionRequest.objects.get)(id=prefix_question.id)
    ).status == "pending"
    assert (
        await sync_to_async(QuestionRequest.objects.get)(id=unscoped_question.id)
    ).status == "rejected"


@pytest.mark.django_db(transaction=True)
async def test_edit_rejects_only_suffix_gates_when_editing_first(
    harness_workspace,
) -> None:
    """Editing the first message rejects every gate (whole suffix)."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)

    stored = HarnessMessageRepository.list_for_session(session.id)
    first_user = next(m for m in stored if m.content == "first")
    second_user = next(m for m in stored if m.content == "second")
    first_perm = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="first",
        title="$ first",
        message_id=first_user.id,
    )
    second_perm = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="second",
        title="$ second",
        message_id=second_user.id,
    )
    await service.edit_user_message(
        session.id,
        message_id=first_user.id,
        prompt="first edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    assert (
        await sync_to_async(PermissionRequest.objects.get)(id=first_perm.id)
    ).status == "rejected"
    assert (
        await sync_to_async(PermissionRequest.objects.get)(id=second_perm.id)
    ).status == "rejected"


@pytest.mark.django_db(transaction=True)
async def test_edit_aborts_and_deletes_suffix_child_session(
    harness_workspace,
) -> None:
    """Subagent children referenced by the suffix are aborted, no orphans."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    stored = HarnessMessageRepository.list_for_session(session.id)
    first_user = next(m for m in stored if m.role == "user")
    assistant = next(m for m in stored if m.role == "assistant")

    child = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="child",
        agent_name="explore",
        mode="build",
        model="fake-model",
        parent_id=session.id,
    )
    await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="subtask",
        state="completed",
        title="Explore",
        meta={"subtask_id": "sub-1", "child_session_id": str(child.id)},
    )
    assert await sync_to_async(HarnessSessionRepository.get_by_id)(child.id) is not None

    await service.edit_user_message(
        session.id,
        message_id=first_user.id,
        prompt="first edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    assert await sync_to_async(HarnessSessionRepository.get_by_id)(child.id) is None
    remaining = HarnessPartRepository.list_for_session(session.id)
    assert all(
        str((p.meta or {}).get("child_session_id", "") or "") != str(child.id)
        for p in remaining
    )


@pytest.mark.django_db(transaction=True)
async def test_edit_keeps_prefix_child_session(harness_workspace) -> None:
    """Children referenced only before the edit target survive the edit."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)
    stored = HarnessMessageRepository.list_for_session(session.id)
    first_assistant = next(m for m in stored if m.role == "assistant")
    second_user = next(m for m in stored if m.content == "second")

    child = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="child",
        agent_name="explore",
        mode="build",
        model="fake-model",
        parent_id=session.id,
    )
    await sync_to_async(HarnessPartRepository.create)(
        message_id=first_assistant.id,
        type="subtask",
        state="completed",
        title="Explore",
        meta={"subtask_id": "sub-1", "child_session_id": str(child.id)},
    )
    await service.edit_user_message(
        session.id,
        message_id=second_user.id,
        prompt="second edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    assert await sync_to_async(HarnessSessionRepository.get_by_id)(child.id) is not None


@pytest.mark.django_db(transaction=True)
async def test_edit_restores_original_title(harness_workspace) -> None:
    """The rerun must not regenerate the title of an existing session."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    renamed = await sync_to_async(HarnessSessionRepository.set_title)(
        session, "My custom title"
    )
    user_msg = next(
        m
        for m in HarnessMessageRepository.list_for_session(renamed.id)
        if m.role == "user"
    )
    await service.edit_user_message(
        renamed.id,
        message_id=user_msg.id,
        prompt="first edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(renamed.id)]
    renamed.refresh_from_db()
    assert renamed.title == "My custom title"


@pytest.mark.django_db(transaction=True)
async def test_edit_empty_overrides_are_noop(harness_workspace) -> None:
    """Empty mode/model/effort strings leave the session config untouched."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await sync_to_async(HarnessSessionRepository.set_model)(session, "keep-model")
    session = await sync_to_async(HarnessSessionRepository.set_reasoning_effort)(
        session, "high"
    )
    session.refresh_from_db()
    user_msg = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="first"
    )
    await service.edit_user_message(
        session.id,
        message_id=user_msg.id,
        prompt="first edited",
        mode="",
        model="   ",
        reasoning_effort="",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    assert session.mode == "build"
    assert session.agent_name == "build"
    assert session.model == "keep-model"
    assert session.reasoning_effort == "high"


@pytest.mark.django_db(transaction=True)
async def test_edit_applies_mode_model_effort(harness_workspace) -> None:
    """Explicit overrides are persisted before the rerun starts."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    user_msg = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="first"
    )
    await service.edit_user_message(
        session.id,
        message_id=user_msg.id,
        prompt="first edited",
        mode="plan",
        model="new-model",
        reasoning_effort="low",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    assert session.mode == "plan"
    assert session.agent_name == "plan"
    assert session.model == "new-model"
    assert session.reasoning_effort == "low"
    assistant = HarnessMessageRepository.list_for_session(session.id)[-1]
    assert assistant.model == "new-model"
    assert assistant.reasoning_effort == "low"


@pytest.mark.django_db(transaction=True)
async def test_edit_history_contains_edited_prompt(harness_workspace) -> None:
    """The rerun history is built from the truncated prefix + edit prompt."""
    seen: list[list[str]] = []

    class RecordingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            seen.append([str(m.content) for m in messages])
            yield Delta(text="ok", usage=Usage(1, 1, 2))

    recording = RecordingProvider([_text_step("ok")])
    service, _, _ = _service(provider=recording)
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    await _run_two_turns(service, session, org_id, harness_workspace.id)
    # Two runs recorded; the edit rerun is the third provider call.
    stored = HarnessMessageRepository.list_for_session(session.id)
    first_user = next(m for m in stored if m.content == "first")
    await service.edit_user_message(
        session.id,
        message_id=first_user.id,
        prompt="first edited",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    assert len(seen) == 3
    assert "second" not in " ".join(seen[2])
    assert "first edited" in " ".join(seen[2])


@pytest.mark.django_db(transaction=True)
async def test_edit_does_not_regenerate_title_for_first_prompt(
    harness_workspace,
) -> None:
    """Editing the only/first prompt keeps the pre-edit title (no regen)."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    assert session.title == "svc"
    org_id = harness_workspace.runner.organization_id
    await service.start_run(
        session,
        "first",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    user_msg = next(
        m
        for m in HarnessMessageRepository.list_for_session(session.id)
        if m.role == "user"
    )
    await service.edit_user_message(
        session.id,
        message_id=user_msg.id,
        prompt="brand new first prompt",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    # FakeProvider runs never spawn the title agent (no small_model config),
    # but the edit path must actively restore the title either way.
    assert session.title == "svc"


@pytest.mark.django_db(transaction=True)
async def test_repository_copy_prefix_returns_id_map(harness_workspace) -> None:
    """copy_prefix maps every source id to a fresh destination id."""
    session = await _db_create_session(harness_workspace)
    org_id = harness_workspace.runner.organization_id
    other = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="dst",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    first = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="one"
    )
    second = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="two"
    )
    id_map = HarnessMessageRepository.copy_prefix(session.id, other.id, second.id)
    assert set(id_map) == {first.id}
    assert id_map[first.id] != first.id
    dst_msgs = HarnessMessageRepository.list_for_session(other.id)
    assert [m.id for m in dst_msgs] == [id_map[first.id]]

    full_map = HarnessMessageRepository.copy_prefix(session.id, other.id, None)
    assert set(full_map) == {first.id, second.id}


@pytest.mark.django_db(transaction=True)
async def test_repository_delete_from_returns_deleted_ids(harness_workspace) -> None:
    """delete_from removes the suffix inclusive and reports the ids."""
    session = await _db_create_session(harness_workspace)
    first = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="one"
    )
    second = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="two"
    )
    deleted = HarnessMessageRepository.delete_from(session.id, second.id)
    assert deleted == [second.id]
    assert [m.id for m in HarnessMessageRepository.list_for_session(session.id)] == [
        first.id
    ]
    with pytest.raises(ValueError, match="not in session"):
        HarnessMessageRepository.delete_from(session.id, uuid.uuid4())


@pytest.mark.django_db(transaction=True)
async def test_fork_preserves_session_config(harness_workspace) -> None:
    """Forks inherit mode/agent/model/effort/skill ids of the source."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    await sync_to_async(HarnessSessionRepository.set_mode)(session, "plan")
    await sync_to_async(HarnessSessionRepository.set_model)(session, "m-1")
    await sync_to_async(HarnessSessionRepository.set_reasoning_effort)(session, "high")
    skill_id = str(uuid.uuid4())
    await sync_to_async(HarnessSessionRepository.set_skill_ids)(session, [skill_id])
    session.refresh_from_db()

    forked = await service.fork_session(session.id)
    assert forked.mode == "plan"
    assert forked.agent_name == "plan"
    assert forked.model == "m-1"
    assert forked.reasoning_effort == "high"
    assert list(forked.skill_ids or []) == [skill_id]


@pytest.mark.django_db(transaction=True)
async def test_compaction_tail_start_id_drop_for_missing_message(
    harness_workspace,
) -> None:
    """An invalid tail_start_id is dropped instead of copied verbatim."""
    session = await _db_create_session(harness_workspace)
    other = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="dst",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    user_msg = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="one"
    )
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant", content="hello"
    )
    await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="compaction",
        state="completed",
        output="summary",
        meta={"tail_start_id": "not-a-uuid"},
    )
    HarnessMessageRepository.copy_prefix(session.id, other.id, None)
    parts = HarnessPartRepository.list_for_session(other.id)
    compactions = [p for p in parts if p.type == "compaction"]
    assert len(compactions) == 1
    assert "tail_start_id" not in (compactions[0].meta or {})
    assert user_msg.id is not None
