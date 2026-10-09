"""Integration tests for HarnessService's Claude engine orchestration seam."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest

from apps.harness.engines.connections import AuthConfiguration
from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService
from apps.harness.harness_service import (
    FRONTEND_EVENT_PART,
    HarnessService,
)
from apps.harness.models import (
    HarnessConnection,
    HarnessMessage,
    HarnessPart,
    HarnessSession,
)
from apps.harness.providers.base import Usage
from apps.harness.repositories import HarnessMessageRepository
from apps.harness.tests.conftest import FakeAccessor
from common.exceptions import AuthenticationError, ConflictError


class FakeConnectionService:
    """Return opaque test auth material without consulting real credentials."""

    def __init__(self, connection_id: uuid.UUID) -> None:
        self.connection_id = connection_id
        self.resolutions: list[tuple[Any, Any, Any]] = []

    def resolve(self, organization_id, user_id, connection_id=None):  # type: ignore[no-untyped-def]
        self.resolutions.append((organization_id, user_id, connection_id))
        return AuthConfiguration(
            auth_type="api_token",
            token="do-not-persist-this-token",
            connection_id=self.connection_id,
        )


@pytest.mark.django_db(transaction=True)
async def test_closing_run_blocks_mutations_until_runner_release(harness_workspace):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="cleanup_pending"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "leave recovery required",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "closing"

    service._accessor_factory = None
    messages_before = HarnessMessageRepository.list_for_session(session.id)
    user_message = messages_before[0]
    with pytest.raises(ConflictError):
        await service.edit_user_message(
            session.id,
            message_id=user_message.id,
            prompt="must not destructively edit",
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id,
        )
    with pytest.raises(ConflictError):
        await service.delete_session(session.id)
    with pytest.raises(ConflictError):
        await service.abort_run(session.id)
    assert HarnessRunRepository.get_open_for_session(session.id) is not None
    assert HarnessMessageRepository.list_for_session(session.id) == messages_before
    assert HarnessSession.objects.filter(id=session.id).exists()

    # Recovery must positively confirm runner release before delete can cascade.
    recovery_accessor = FakeAccessor(
        str(session.workspace_id),
        desktop_result={"ok": True, "lease_state": "released"},
    )
    service._accessor_factory = lambda _workspace_id: recovery_accessor
    await service.delete_session(session.id)
    assert any(
        action == "release"
        for action, _args, _timeout in recovery_accessor.desktop_calls
    )
    assert not HarnessSession.objects.filter(id=session.id).exists()
    assert not HarnessRunRepository.model.objects.filter(id=attempt.id).exists()


@pytest.mark.django_db(transaction=True)
async def test_claude_async_run_works_without_async_unsafe_escape(
    harness_workspace, monkeypatch
):
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    service, _registry, _connections, _events = _service(connection_id=uuid.uuid4())
    session = await asyncio.to_thread(_new_session_sync, service, harness_workspace)
    assistant = await service.start_run(
        session,
        "no sync ORM on the event loop",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    assert assistant.finish == "stop"
    assert assistant.content == "Claude: done"
    fresh = await asyncio.to_thread(HarnessSession.objects.get, id=session.id)
    assert fresh.status == "idle"


def _new_session_sync(service, harness_workspace):
    """Create fixture-backed auth/session rows from an ORM worker thread."""
    connection_id = service._engine_connections.connection_id
    HarnessConnection.objects.create(
        id=connection_id,
        organization_id=harness_workspace.runner.organization_id,
        user_id=harness_workspace.created_by_id,
        auth_type="api_token",
        label="Claude test",
    )
    return service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="Summarize the repository",
        mode="build",
        harness_id="claude",
        user_id=harness_workspace.created_by_id,
    )


class FakeClaudeEngine:
    """Small async engine that exercises runtime callbacks, never Claude SDK."""

    def __init__(self, *, behavior: str, kwargs: dict[str, Any]) -> None:
        self.behavior = behavior
        self.kwargs = kwargs

    async def run(self, prompt, agent, model, mode, opts):  # type: ignore[no-untyped-def]
        lease = {"lease_id": str(uuid.uuid4()), "epoch": "epoch-test"}
        external_id = str(uuid.uuid4())
        try:
            await self.kwargs["on_owner"](lease, "reserved")
            await self.kwargs["on_binding"](
                external_id,
                {
                    "harness_id": "claude",
                    "cli_version": "2.1.292",
                    "sdk_version": "0.2.164",
                    "model": model,
                    "mode": mode,
                },
            )
            await self.kwargs["emit"](
                {"type": "part_updated", "step": 1, "delta": {"text": "Claude: "}}
            )
            if self.behavior in {"cleanup_pending", "closing_error"}:
                await self.kwargs["on_owner"](lease, "closing")
                raise RuntimeError("Claude failed while closing")
            if self.behavior == "subagent":
                await self.kwargs["emit"](
                    {
                        "type": "subtask_started",
                        "subtask_id": "sdk-task-1",
                        "parent_tool_use_id": "tool-call-1",
                        "agent": "explorer",
                        "description": "Inspect the code",
                        "model": model,
                    }
                )
                await self.kwargs["on_child_event"](
                    "tool-call-1",
                    {"type": "part_updated", "step": 1, "delta": {"text": "found it"}},
                )
                await self.kwargs["emit"](
                    {
                        "type": "subtask_finished",
                        "subtask_id": "sdk-task-1",
                        "parent_tool_use_id": "tool-call-1",
                        "agent": "explorer",
                        "status": "completed",
                        "summary": "found it",
                    }
                )
            if self.behavior == "error":
                raise RuntimeError("ANTHROPIC_API_KEY=do-not-persist-this-token")
            if self.behavior == "cancel":
                await asyncio.Event().wait()
            await self.kwargs["emit"](
                {"type": "part_updated", "step": 1, "delta": {"text": "done"}}
            )
            from apps.harness.runner import RunResult

            return RunResult(
                output="Claude: done",
                steps=1,
                usage=Usage(3, 2, 5, 0.01),
                cost=0.01,
                finish_reason="stop",
                metadata={
                    "harness_id": "claude",
                    "external_session_id": external_id,
                    "cli_version": "2.1.292",
                    "sdk_version": "0.2.164",
                    "model": model,
                    "reasoning_effort": "high",
                },
            )
        finally:
            if self.behavior not in {"cleanup_pending", "closing_error"}:
                await self.kwargs["on_owner"](lease, "released")


class FakeEngineRegistry:
    def __init__(self, behavior: str = "success") -> None:
        self.behavior = behavior
        self.created: list[tuple[str, dict[str, Any]]] = []

    def create(self, harness_id: str, **kwargs: Any) -> FakeClaudeEngine:
        self.created.append((harness_id, kwargs))
        return FakeClaudeEngine(behavior=self.behavior, kwargs=kwargs)


def _service(*, connection_id: uuid.UUID, behavior: str = "success"):
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(name: str, data: dict[str, Any]) -> None:
        emitted.append((name, dict(data)))

    registry = FakeEngineRegistry(behavior)
    connections = FakeConnectionService(connection_id)
    service = HarnessService(
        emit=emit,
        accessor_factory=lambda workspace_id: _fake_accessor(workspace_id),
        engine_registry=registry,
        engine_connections=connections,
        run_service=HarnessRunService(),
    )
    return service, registry, connections, emitted


async def _fake_accessor(workspace_id: str) -> FakeAccessor:
    return FakeAccessor(workspace_id)


async def _new_session(service, harness_workspace, *, harness_id="claude"):
    connection_id = service._engine_connections.connection_id
    HarnessConnection.objects.create(
        id=connection_id,
        organization_id=harness_workspace.runner.organization_id,
        user_id=harness_workspace.created_by_id,
        auth_type="api_token",
        label="Claude test",
    )
    return service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="Summarize the repository",
        mode="build",
        harness_id=harness_id,
        user_id=harness_workspace.created_by_id,
    )


@pytest.mark.django_db(transaction=True)
async def test_claude_run_persists_engine_binding_and_secret_free_result(
    harness_workspace,
):
    connection_id = uuid.uuid4()
    service, registry, connections, events = _service(connection_id=connection_id)
    session = await _new_session(service, harness_workspace)

    assert session.harness_id == "claude"
    assert session.connection_id == connection_id
    assert session.created_by_id == harness_workspace.created_by_id
    assert session.model == "sonnet"
    assert session.reasoning_effort == "high"
    assert connections.resolutions[0][1] == harness_workspace.created_by_id

    assistant = await service.start_run(
        session,
        "Summarize the repository",
        organization_id=session.organization_id,
        workspace_id=str(session.workspace_id),
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    await asyncio.to_thread(assistant.refresh_from_db)
    await asyncio.to_thread(session.refresh_from_db)
    assert assistant.content == "Claude: done"
    assert assistant.harness_id == "claude"
    assert assistant.engine_meta["harness_id"] == "claude"
    assert "do-not-persist-this-token" not in str(assistant.engine_meta)
    assert session.external_session_id == assistant.engine_meta["external_session_id"]
    assert session.status == "idle"
    attempt = HarnessRunRepository.get_open_for_session(session.id)
    assert attempt is None
    assert registry.created[0][0] == "claude"
    assert registry.created[0][1]["auth_env"] == {
        "ANTHROPIC_API_KEY": "do-not-persist-this-token"
    }
    assert any(name == FRONTEND_EVENT_PART for name, _ in events)
    assert all("do-not-persist-this-token" not in str(data) for _, data in events)


@pytest.mark.django_db(transaction=True)
async def test_claude_fork_and_edit_never_resume_stale_cli_suffix(
    harness_workspace,
):
    service, registry, _connections, _events = _service(connection_id=uuid.uuid4())
    session = await _new_session(service, harness_workspace)
    first_assistant = await service.start_run(
        session,
        "first prompt",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    first_assistant.refresh_from_db()
    user_message = HarnessMessageRepository.list_for_session(session.id)[0]
    original_external_id = session.external_session_id
    assert original_external_id

    forked = await service.fork_session(session.id)
    forked.refresh_from_db()
    assert forked.harness_id == "claude"
    assert forked.connection_id == session.connection_id
    assert forked.created_by_id == session.created_by_id
    assert forked.external_session_id == ""
    assert forked.engine_state == {}
    copied = HarnessMessageRepository.list_for_session(forked.id)
    assert copied[-1].engine_meta == first_assistant.engine_meta
    assert copied[-1].harness_id == "claude"

    await service.edit_user_message(
        session.id,
        message_id=user_message.id,
        prompt="edited prompt",
        organization_id=session.organization_id,
        workspace_id=str(session.workspace_id),
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    session.refresh_from_db()
    assert session.external_session_id != original_external_id
    assert registry.created[-1][1]["external_session_id"] == ""
    assert (
        HarnessMessageRepository.list_for_session(session.id)[0].content
        == "edited prompt"
    )


@pytest.mark.django_db(transaction=True)
async def test_claude_closing_callbacks_still_finalize_success_and_error(
    harness_workspace,
):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="closing_result"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "closing result",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]
    assistant.refresh_from_db()
    session.refresh_from_db()
    row = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert row.status == "completed"
    assert assistant.finish == "stop"
    assert assistant.content == "Claude: done"
    assert session.status == "idle"

    from django.contrib.auth import get_user_model

    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user = get_user_model().objects.create_user(
        email=f"closing-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    runner = Runner.objects.create(
        name="closing-runner",
        api_token_hash=hash_token(uuid.uuid4().hex),
        status=RunnerStatus.ONLINE,
        sid=f"closing-{uuid.uuid4().hex[:8]}",
        organization_id=harness_workspace.runner.organization_id,
        available_runtimes=["docker"],
    )
    second_workspace = Workspace.objects.create(
        runner=runner,
        name="Closing Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    failed_service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="closing_error"
    )
    failed_session = await _new_session(failed_service, second_workspace)
    failed_assistant = await failed_service.start_run(
        failed_session,
        "closing error",
        organization_id=failed_session.organization_id,
        user_id=second_workspace.created_by_id,
    )
    await failed_service._tasks[str(failed_session.id)]
    failed_assistant.refresh_from_db()
    failed_session.refresh_from_db()
    failed_row = HarnessRunRepository.model.objects.get(
        assistant_message=failed_assistant
    )
    assert failed_row.status == "closing"
    assert failed_assistant.finish == "error"
    assert failed_assistant.error == "Claude run failed."
    assert failed_session.status == "busy"


@pytest.mark.django_db(transaction=True)
async def test_claude_unconfirmed_cleanup_retains_run_admission(harness_workspace):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="cleanup_pending"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "cleanup must remain fenced",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    row = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assistant.refresh_from_db()
    session.refresh_from_db()
    assert row.status == "closing"
    assert assistant.finish == "error"
    assert assistant.error == "Claude run failed."
    assert session.status == "busy"
    assert not service.is_running(session.id)


@pytest.mark.django_db(transaction=True)
async def test_claude_session_auth_is_owner_bound(harness_workspace):
    connection_id = uuid.uuid4()
    service, _registry, _connections, _events = _service(connection_id=connection_id)
    session = await _new_session(service, harness_workspace)

    with pytest.raises(AuthenticationError):
        await service.start_run(
            session,
            "not the owner",
            organization_id=session.organization_id,
            user_id=harness_workspace.created_by_id + 1000,
        )
    assert HarnessMessageRepository.list_for_session(session.id) == []


@pytest.mark.django_db(transaction=True)
async def test_claude_engine_error_is_generic_in_message_and_terminal_attempt(
    harness_workspace,
    caplog,
):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="error"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "fail safely",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert assistant.error == "Claude run failed."
    assert "do-not-persist-this-token" not in assistant.error
    assert "do-not-persist-this-token" not in caplog.text
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "error"
    assert attempt.error == "Engine run failed."


@pytest.mark.django_db(transaction=True)
async def test_claude_abort_finishes_attempt_and_child_projection(harness_workspace):
    service, _registry, _connections, events = _service(
        connection_id=uuid.uuid4(), behavior="subagent"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "delegate work",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    await service._tasks[str(session.id)]

    child = HarnessSession.objects.get(parent_id=session.id)
    child_assistant = HarnessMessage.objects.filter(
        session=child, role="assistant"
    ).get()
    assert child.harness_id == "claude"
    assert child.agent_name == "explore"
    assert child.status == "idle"
    assert child_assistant.content == "found it"
    assert HarnessPart.objects.filter(
        message=assistant, type="subtask", meta__child_session_id=str(child.id)
    ).exists()
    assert any(name == "harness.subtask_started" for name, _ in events)
    assert any(name == "harness.subtask_finished" for name, _ in events)


@pytest.mark.django_db(transaction=True)
async def test_queued_tool_card_is_idempotent_and_enriched_by_later_payload(
    harness_workspace,
):
    service, _registry, _connections, events = _service(connection_id=uuid.uuid4())
    session = await _new_session(service, harness_workspace)
    await asyncio.to_thread(service.sessions.mark_status, session, "busy")
    assistant = await asyncio.to_thread(
        service.messages.create,
        session_id=session.id,
        role="assistant",
        content="",
        harness_id="claude",
    )
    service._runs[str(session.id)] = {
        "session_id": str(session.id),
        "message_id": str(assistant.id),
        "tool_parts": {},
        "step_parts": {},
        "subtask_parts": {},
        "skill_bodies": [],
    }

    early = {
        "type": "tool_queued",
        "step": 1,
        "call_id": "call-queued-1",
        "tool": "read",
        "title": "Read file",
        "arguments": "",
    }
    await service._on_runner_event(session, assistant, early)
    first = await asyncio.to_thread(
        lambda: HarnessPart.objects.get(
            message_id=assistant.id, call_id="call-queued-1"
        )
    )
    await service._on_runner_event(session, assistant, early)
    await service._on_runner_event(
        session,
        assistant,
        {
            **early,
            "title": "Read /workspace/README.md",
            "arguments": json.dumps({"path": "/workspace/README.md"}),
        },
    )
    rows = await asyncio.to_thread(
        lambda: list(
            HarnessPart.objects.filter(
                message_id=assistant.id, type="tool", call_id="call-queued-1"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert rows[0].state == "pending"
    assert rows[0].title == "Read /workspace/README.md"
    assert rows[0].input == {
        "tool": "read",
        "arguments": json.dumps({"path": "/workspace/README.md"}),
    }

    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "tool_started",
            "step": 1,
            "call_id": "call-queued-1",
            "tool": "read",
            "title": "Read /workspace/README.md",
            "arguments": json.dumps({"path": "/workspace/README.md"}),
        },
    )
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "tool_completed",
            "step": 1,
            "call_id": "call-queued-1",
            "tool": "read",
            "output": "read complete",
        },
    )
    await service._on_runner_event(
        session,
        assistant,
        {
            **early,
            "title": "late queued notification",
            "arguments": '{"path":"stale"}',
        },
    )
    rows = await asyncio.to_thread(
        lambda: list(
            HarnessPart.objects.filter(
                message_id=assistant.id, type="tool", call_id="call-queued-1"
            )
        )
    )
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert rows[0].state == "completed"
    assert rows[0].input["arguments"] == json.dumps({"path": "/workspace/README.md"})
    assert rows[0].output == "read complete"
    correlated_events = [
        data
        for name, data in events
        if name == FRONTEND_EVENT_PART
        and (
            data.get("delta", {}).get("call_id") == "call-queued-1"
            or data.get("part_id") == str(first.id)
        )
        and data.get("delta", {}).get("tool_started") == "read"
    ]
    assert correlated_events
    assert {data.get("part_id") for data in correlated_events} == {str(first.id)}


@pytest.mark.django_db(transaction=True)
async def test_claude_cancellation_sets_interrupted_attempt(harness_workspace):
    service, _registry, _connections, _events = _service(
        connection_id=uuid.uuid4(), behavior="cancel"
    )
    session = await _new_session(service, harness_workspace)
    assistant = await service.start_run(
        session,
        "wait for cancellation",
        organization_id=session.organization_id,
        user_id=harness_workspace.created_by_id,
    )
    task = service._tasks[str(session.id)]
    await asyncio.sleep(0.05)
    await service.abort_run(session.id)
    if not task.done():
        await task

    assistant.refresh_from_db()
    assert assistant.finish == "aborted"
    attempt = HarnessRunRepository.model.objects.get(assistant_message=assistant)
    assert attempt.status == "interrupted"
    session.refresh_from_db()
    assert session.status == "idle"
