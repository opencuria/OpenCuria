"""Tests for M6 HarnessService: runs, double-run guard, abort."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.core.exceptions import SynchronousOnlyOperation

from apps.harness.harness_service import (
    FRONTEND_EVENT_PART,
    FRONTEND_EVENT_PERMISSION,
    FRONTEND_EVENT_QUESTION,
    FRONTEND_EVENT_STATUS,
    HarnessService,
)
from apps.harness.models import HarnessSession, QuestionRequest, Todo
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
    ProviderTimeoutError,
    ToolSchema,
    Usage,
)
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
    QuestionRequestRepository,
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


def _tool_step(
    tool: str,
    args: dict[str, Any],
    *,
    call_id: str = "call-1",
) -> list[Delta]:
    return [
        Delta(
            tool_calls=(
                {
                    "index": 0,
                    "id": call_id,
                    "name": tool,
                    "arguments": json.dumps(args),
                },
            ),
            usage=Usage(2, 3, 5),
        )
    ]


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
    assert assistant.tokens.get("total") == 2
    assert assistant.tokens.get("prompt") == 1
    assert assistant.tokens.get("completion") == 1

    parts = HarnessPartRepository.list_for_session(session.id)
    assert any(p.type == "text" and "hello" in (p.output or "") for p in parts)
    assert any(p.type == "step-finish" for p in parts)

    session.refresh_from_db()
    assert session.status == "idle"
    assert session.tokens.get("total") == 2
    assert session.tokens.get("total") == assistant.tokens.get("total")

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
    """Abort cancels the task and keeps streamed reasoning content."""
    gate = asyncio.Event()
    streamed = asyncio.Event()

    class HangingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            yield Delta(reasoning="considering the layout")
            streamed.set()
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
    await asyncio.wait_for(streamed.wait(), timeout=2)
    await _wait_for_part(session.id, part_type="reasoning")
    assert service.is_running(session.id)
    aborted = await service.abort_run(session.id)
    assert aborted.status == "idle"
    assert not service.is_running(session.id)

    stored = HarnessMessageRepository.list_for_session(session.id)
    assistant = [m for m in stored if m.role == "assistant"][0]
    assert assistant.finish == "aborted"
    assert assistant.error == "aborted by user"
    parts = HarnessPartRepository.list_for_session(session.id)
    reasoning = [p for p in parts if p.type == "reasoning"]
    assert reasoning
    assert reasoning[0].state == "completed"
    assert reasoning[0].output == "considering the layout"
    assert [e["event"] for e in events][-1] == "harness.session_status"


@pytest.mark.django_db(transaction=True)
async def test_abort_rejects_pending_permission_and_question(
    harness_workspace,
) -> None:
    """Abort marks leftover user gates rejected and notifies the frontend."""
    service, _, events = _service()
    session = await _db_create_session(harness_workspace)
    permission = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=harness_workspace.runner.organization_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="bash",
        pattern="reboot",
        title="$ reboot",
    )
    question = await sync_to_async(QuestionRequestRepository.create)(
        organization_id=harness_workspace.runner.organization_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        questions=[{"question": "Continue?"}],
    )
    await service.abort_run(session.id)

    stored_permission = await sync_to_async(PermissionRequest.objects.get)(
        id=permission.id
    )
    stored_question = await sync_to_async(QuestionRequest.objects.get)(id=question.id)
    assert stored_permission.status == "rejected"
    assert stored_question.status == "rejected"
    assert any(
        event["event"] == FRONTEND_EVENT_PERMISSION
        and event.get("request_id") == str(permission.id)
        and event.get("decision") == "reject"
        for event in events
    )
    assert any(
        event["event"] == FRONTEND_EVENT_QUESTION
        and event.get("request_id") == str(question.id)
        and event.get("status") == "rejected"
        for event in events
    )


@pytest.mark.django_db(transaction=True)
async def test_start_run_settles_open_text_and_reasoning_parts(
    harness_workspace,
) -> None:
    """A finished run persists leftover text/reasoning parts as completed."""
    provider = FakeProvider(
        [
            [
                Delta(reasoning="planning the change"),
                Delta(text="hello", usage=Usage(1, 1, 2)),
            ]
        ]
    )
    service, _, _events = _service(provider=provider)
    session = await _db_create_session(harness_workspace)
    await service.start_run(
        session,
        "do the thing",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    parts = HarnessPartRepository.list_for_session(session.id)
    reasoning = [p for p in parts if p.type == "reasoning"]
    text = [p for p in parts if p.type == "text"]
    assert reasoning
    assert reasoning[0].state == "completed"
    assert reasoning[0].output == "planning the change"
    assert text
    assert text[0].state == "completed"
    assert "hello" in (text[0].output or "")


@pytest.mark.django_db(transaction=True)
async def test_run_error_preserves_stream_content_and_fails_tools(
    harness_workspace,
) -> None:
    """A provider crash completes thoughts and fails leftover tools."""

    class FailingProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            yield Delta(reasoning="almost there")
            raise RuntimeError("provider exploded")

    service, _, _events = _service(provider=FailingProvider([]))
    session = await _db_create_session(harness_workspace)
    await service.start_run(
        session,
        "will fail",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    stored = HarnessMessageRepository.list_for_session(session.id)
    assistant = [m for m in stored if m.role == "assistant"][0]
    assert assistant.finish == "error"
    assert "provider exploded" in (assistant.error or "")
    parts = HarnessPartRepository.list_for_session(session.id)
    reasoning = [p for p in parts if p.type == "reasoning"]
    assert reasoning
    assert reasoning[0].state == "completed"
    assert reasoning[0].output == "almost there"


@pytest.mark.django_db(transaction=True)
async def test_fail_open_parts_preserves_partial_tool_output(
    harness_workspace,
) -> None:
    """Abort keeps partial tool output instead of overwriting it."""
    service, _, _events = _service()
    session = await _db_create_session(harness_workspace)
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id,
        role="assistant",
        content="",
    )
    reasoning = await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="reasoning",
        state="running",
    )
    await sync_to_async(HarnessPartRepository.append_output)(
        reasoning, "already thought this"
    )
    tool_partial = await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="tool",
        state="running",
        title="bash",
    )
    await sync_to_async(HarnessPartRepository.append_output)(tool_partial, "stdout…")
    tool_empty = await sync_to_async(HarnessPartRepository.create)(
        message_id=assistant.id,
        type="tool",
        state="running",
        title="read",
    )

    await service._fail_open_parts(assistant, state="error", output="aborted")

    reasoning.refresh_from_db()
    tool_partial.refresh_from_db()
    tool_empty.refresh_from_db()
    assert reasoning.state == "completed"
    assert reasoning.output == "already thought this"
    assert tool_partial.state == "error"
    assert tool_partial.output == "stdout…"
    assert tool_empty.state == "error"
    assert tool_empty.output == "aborted"


@pytest.mark.django_db(transaction=True)
async def test_abort_run_does_not_call_orm_from_async_context(
    harness_workspace, monkeypatch
) -> None:
    """Idle abort must wrap session lookup so Daphne does not 500."""
    service, _, _ = _service()
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="idle abort",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        aborted = await service.abort_run(session.id)
    except SynchronousOnlyOperation:
        pytest.fail("abort_run called Django ORM from an async context")
    assert aborted.status == "idle"


@pytest.mark.asyncio
async def test_spawn_background_detaches_asgiref_executor() -> None:
    """Background tasks must not inherit the HTTP request executor."""
    from asgiref.sync import AsyncToSync

    seen: dict[str, Any] = {}

    async def probe() -> None:
        seen["executor"] = getattr(AsyncToSync.executors, "current", None)

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
    )
    sentinel = object()
    AsyncToSync.executors.current = sentinel
    try:
        await service._spawn_background(probe())
    finally:
        if getattr(AsyncToSync.executors, "current", None) is sentinel:
            del AsyncToSync.executors.current

    assert "executor" in seen
    assert seen["executor"] is not sentinel


@pytest.mark.django_db(transaction=True)
async def test_failed_run_persists_error_without_task_exception(
    harness_workspace, monkeypatch
) -> None:
    """Provider failures persist error finish and do not leak task exceptions."""
    monkeypatch.setattr("apps.harness.runner.retry_delay", lambda _attempt: 0)

    class TimeoutProvider(FakeProvider):
        async def chat_stream(  # type: ignore[no-untyped-def]
            self,
            model: str,
            messages: list[LLMMessage],
            tools: list[ToolSchema],
            opts: ChatOptions | None = None,
        ) -> AsyncIterator[Delta]:
            raise ProviderTimeoutError("SSE read timed out", provider="fake")
            yield Delta(text="unused")  # pragma: no cover

    service, _, _ = _service(provider=TimeoutProvider([]))
    session = await _db_create_session(harness_workspace)
    assistant = await service.start_run(
        session,
        "will timeout",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    task = service._tasks.get(str(session.id))
    if task is not None:
        await task
        assert task.exception() is None
    assistant.refresh_from_db()
    assert assistant.finish == "error"
    assert "timed out" in (assistant.error or "")
    session.refresh_from_db()
    assert session.status == "idle"


@pytest.mark.django_db(transaction=True)
async def test_execute_run_loads_org_provider_without_sync_orm(
    harness_workspace, monkeypatch
) -> None:
    """Production path (no provider_factory) must not hit ORM from asyncio."""
    from apps.harness.providers.models_catalog import ProviderModel
    from apps.harness.providers.openrouter import OpenRouterAdapter
    from apps.harness.services import ProviderConfigService

    org_id = harness_workspace.runner.organization_id
    ProviderConfigService().save_config(
        organization_id=org_id,
        api_key="sk-test",
        base_url="https://example.com/v1",
        default_model="org-default-model",
        small_model="",
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="orm-safe run",
        agent_name="build",
        mode="build",
        model="fake-model",
    )

    async def _scripted_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        yield Delta(text="hello", usage=Usage(1, 1, 2))

    monkeypatch.setattr(OpenRouterAdapter, "chat_stream", _scripted_stream)

    def _fake_list(self, organization_id):  # type: ignore[no-untyped-def]
        assert organization_id == org_id
        return [
            ProviderModel(
                id="fake-model",
                name="Fake",
                reasoning_efforts=("high",),
                default_effort="high",
                supports_tools=True,
                context_length=128_000,
                max_output_tokens=8_192,
            )
        ]

    monkeypatch.setattr(ProviderConfigService, "list_models", _fake_list)

    collected: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        collected.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
    )
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        await service.start_run(
            session,
            "say hello",
            organization_id=org_id,
            workspace_id=str(harness_workspace.id),
        )
        await service._tasks[str(session.id)]
    except SynchronousOnlyOperation:
        pytest.fail("_execute_run called Django ORM from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

    stored = HarnessMessageRepository.list_for_session(session.id)
    assistant = [m for m in stored if m.role == "assistant"][0]
    assert assistant.finish == "stop"
    assert assistant.content == "hello"


@pytest.mark.django_db(transaction=True)
async def test_execute_run_passes_catalog_limits_and_last_step_tokens(
    harness_workspace, monkeypatch
) -> None:
    """_execute_run always resolves model limits and prior step tokens."""
    from apps.harness.providers.models_catalog import ProviderModel
    from apps.harness.providers.openrouter import OpenRouterAdapter
    from apps.harness.runner import HarnessRunner, RunOptions, RunResult
    from apps.harness.services import ProviderConfigService

    org_id = harness_workspace.runner.organization_id
    ProviderConfigService().save_config(
        organization_id=org_id,
        api_key="sk-test",
        base_url="https://example.com/v1",
        default_model="org-default-model",
        small_model="",
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="limits run",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    prev_assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id,
        role="assistant",
        content="prior answer",
    )
    await sync_to_async(HarnessPartRepository.create)(
        message_id=prev_assistant.id,
        type="step-finish",
        state="completed",
        meta={
            "tokens": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }
        },
    )

    def _fake_list(self, organization_id):  # type: ignore[no-untyped-def]
        return [
            ProviderModel(
                id="fake-model",
                name="Fake",
                reasoning_efforts=("high",),
                default_effort="high",
                supports_tools=True,
                context_length=128_000,
                max_output_tokens=8_192,
            )
        ]

    monkeypatch.setattr(ProviderConfigService, "list_models", _fake_list)

    async def _scripted_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        yield Delta(text="hello", usage=Usage(1, 1, 2))

    monkeypatch.setattr(OpenRouterAdapter, "chat_stream", _scripted_stream)

    captured: list[RunOptions] = []

    def _runner_factory(*, provider, tools, accessor, emit):  # type: ignore[no-untyped-def]
        runner = HarnessRunner(
            provider=provider,
            tools=tools,
            accessor=accessor,
            emit=emit,
        )
        original_run = runner.run

        async def _capturing_run(prompt, agent, model, mode, opts):  # type: ignore[no-untyped-def]
            captured.append(opts)
            return RunResult(
                output="ok",
                steps=1,
                usage=Usage(),
                cost=0.0,
                finish_reason="stop",
            )

        runner.run = _capturing_run  # type: ignore[method-assign]
        return runner

    service = HarnessService(runner_factory=_runner_factory)
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    await service.start_run(
        session,
        "follow up",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    assert captured
    opts = captured[0]
    assert opts.context_length == 128_000
    assert opts.max_output_tokens == 8_192
    assert opts.last_step_prompt_tokens == 100
    assert opts.last_step_completion_tokens == 50
    assert opts.last_step_total_tokens == 150


@pytest.mark.django_db(transaction=True)
async def test_execute_run_todowrite_without_sync_orm(
    harness_workspace, monkeypatch
) -> None:
    """todowrite through the Django repo must not hit ORM from asyncio."""
    org_id = harness_workspace.runner.organization_id
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="todo-orm-safe run",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    todos = [
        {"content": "first", "status": "in_progress"},
        {"content": "second", "status": "pending"},
    ]
    provider = FakeProvider(
        [
            _tool_step("todowrite", {"todos": todos}),
            _text_step("noted"),
        ]
    )
    collected: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        collected.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
        provider_factory=lambda _org: provider,
    )
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        await service.start_run(
            session,
            "track work",
            organization_id=org_id,
            workspace_id=str(harness_workspace.id),
        )
        await service._tasks[str(session.id)]
    except SynchronousOnlyOperation:
        pytest.fail("todowrite called Django ORM from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

    assert Todo.objects.filter(session_id=session.id).count() == 2
    parts = HarnessPartRepository.list_for_session(session.id)
    tool_parts = [part for part in parts if part.type == "tool"]
    assert len(tool_parts) == 1
    assert tool_parts[0].state == "completed"
    updated = [e for e in collected if e["event"] == "harness.todo_updated"]
    assert updated
    assert [item["content"] for item in updated[0]["todos"]] == ["first", "second"]


@pytest.mark.django_db(transaction=True)
async def test_execute_run_with_skills_without_sync_orm(
    harness_workspace, monkeypatch
) -> None:
    """start_run must wrap skill-body ORM so Daphne does not 500."""
    from apps.skills.models import Skill

    org_id = harness_workspace.runner.organization_id
    user_id = harness_workspace.created_by_id
    skill = Skill.objects.create(
        name="Hints",
        body="Always use type hints.",
        organization_id=org_id,
        created_by_id=user_id,
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="skill-orm-safe run",
        agent_name="build",
        mode="build",
        model="fake-model",
        skill_ids=[str(skill.id)],
    )
    captured: list[list[str]] = []
    original = HarnessService._execute_run

    async def _capture(self, **kwargs):  # type: ignore[no-untyped-def]
        key = str(kwargs["session"].id)
        captured.append(list(self._runs.get(key, {}).get("skill_bodies") or []))
        await original(self, **kwargs)

    monkeypatch.setattr(HarnessService, "_execute_run", _capture)
    service, _fake, _events = _service()
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        await service.start_run(
            session,
            "say hello",
            organization_id=org_id,
            workspace_id=str(harness_workspace.id),
            user_id=user_id,
        )
        await service._tasks[str(session.id)]
    except SynchronousOnlyOperation:
        pytest.fail("start_run resolved skill bodies from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

    stored = HarnessMessageRepository.list_for_session(session.id)
    assistant = [m for m in stored if m.role == "assistant"][0]
    assert assistant.finish == "stop"
    assert assistant.content == "hello"
    assert captured
    assert "Always use type hints." in captured[0][0]


@pytest.mark.django_db(transaction=True)
async def test_generate_title_loads_org_provider_without_sync_orm(
    harness_workspace, monkeypatch
) -> None:
    """Title agent without provider_factory must not hit ORM from asyncio."""
    from apps.harness.harness_service import _title_from_prompt
    from apps.harness.providers.openrouter import OpenRouterAdapter
    from apps.harness.services import ProviderConfigService

    org_id = harness_workspace.runner.organization_id
    prompt = "Please help me build a feature"
    ProviderConfigService().save_config(
        organization_id=org_id,
        api_key="sk-test",
        base_url="https://example.com/v1",
        default_model="big-model",
        small_model="small-model",
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title=_title_from_prompt(prompt),
        agent_name="build",
        mode="build",
        model="big-model",
    )

    async def _scripted_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        yield Delta(text="Short Generated Title Here", usage=Usage(1, 1, 2))

    monkeypatch.setattr(OpenRouterAdapter, "chat_stream", _scripted_stream)

    async def _drop_emit(event: str, data: dict[str, Any]) -> None:
        return None

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
    )
    monkeypatch.delenv("DJANGO_ALLOW_ASYNC_UNSAFE", raising=False)
    try:
        await service._generate_title(
            session_id=session.id,
            prompt=prompt,
            organization_id=org_id,
        )
    except SynchronousOnlyOperation:
        pytest.fail("_generate_title called Django ORM from an async context")
    finally:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

    refreshed = HarnessSessionRepository.get_by_id(session.id)
    assert refreshed is not None
    assert refreshed.title == "Short Generated Title Here"


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


@pytest.mark.django_db(transaction=True)
async def test_build_history_includes_tool_calls(harness_workspace) -> None:
    """_build_history replays assistant tool_calls and tool results from parts."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    HarnessMessageRepository.create(
        session_id=session.id, role="user", content="read a file"
    )
    assistant = HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="I read it.",
    )
    part = HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="completed",
        call_id="call-42",
        title="read a.txt",
        input={"tool": "read", "arguments": '{"path":"a.txt"}'},
    )
    HarnessPartRepository.mark_state(part, "completed", output="file contents")
    history = await service._build_history(session)
    roles = [m.role for m in history]
    assert roles == ["user", "assistant", "tool"]
    assistant_msg = history[1]
    assert assistant_msg.tool_calls
    assert assistant_msg.tool_calls[0]["id"] == "call-42"
    assert assistant_msg.tool_calls[0]["name"] == "read"
    assert history[2].tool_call_id == "call-42"
    assert history[2].content == "file contents"


@pytest.mark.django_db(transaction=True)
def test_ensure_user_promptable_rejects_child_session(harness_workspace) -> None:
    """Users cannot prompt subagent child sessions; root sessions stay open."""
    service, _, _ = _service()
    parent = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="parent",
    )
    child = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="child",
        parent_id=parent.id,
    )
    service.ensure_user_promptable(parent)
    with pytest.raises(ValueError, match="subagent"):
        service.ensure_user_promptable(child)


@pytest.mark.django_db(transaction=True)
async def test_start_run_on_child_session_still_works(harness_workspace) -> None:
    """Internal subagent launch may still call start_run on a child session."""
    os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
    service, _, _ = _service()
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="parent",
        agent_name="build",
        mode="build",
        model="fake-model",
    )
    child = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="child",
        agent_name="general",
        mode="build",
        model="fake-model",
        parent_id=parent.id,
    )
    assistant = await service.start_run(
        child,
        "look around",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(child.id)]
    assistant.refresh_from_db()
    assert assistant.content == "hello"


@pytest.mark.django_db(transaction=True)
def test_create_session_plan_mode_aligns_agent_name(harness_workspace) -> None:
    """create_session with mode=plan sets agent_name=plan (ignores default build)."""
    service, _, _ = _service()
    session = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="plan it",
        mode="plan",
        agent_name="build",
    )
    assert session.mode == "plan"
    assert session.agent_name == "plan"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("subagent", ("general", "explore", "computeruse"))
def test_create_session_child_honors_subagent_agent_name(
    harness_workspace, subagent: str
) -> None:
    """Child sessions persist subagent agent_name while keeping parent mode."""
    service, _, _ = _service()
    parent = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="parent",
        mode="build",
    )
    child = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="child task",
        parent_id=parent.id,
        agent_name=subagent,
        mode="build",
    )
    assert child.mode == "build"
    assert child.agent_name == subagent


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("hidden_agent", ("title", "compaction"))
def test_create_session_child_rejects_hidden_agent(
    harness_workspace, hidden_agent: str
) -> None:
    """Hidden agents cannot be spawned as subagent child sessions."""
    service, _, _ = _service()
    parent = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="parent",
    )
    with pytest.raises(ValueError, match="subagent child"):
        service.create_session(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
            prompt="child",
            parent_id=parent.id,
            agent_name=hidden_agent,
        )


@pytest.mark.django_db(transaction=True)
def test_create_session_stores_reasoning_effort(harness_workspace) -> None:
    """create_session persists a valid reasoning_effort token."""
    service, _, _ = _service()
    session = service.create_session(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="think hard",
        mode="build",
        reasoning_effort="high",
    )
    assert session.reasoning_effort == "high"
    updated = service.set_reasoning_effort(session.id, "low")
    assert updated.reasoning_effort == "low"


@pytest.mark.django_db(transaction=True)
async def test_start_run_snapshots_reasoning_effort_on_assistant(
    harness_workspace,
) -> None:
    """Assistant shells store the session effort used for that turn."""
    service, _, _ = _service()
    session = await sync_to_async(service.create_session)(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        prompt="think hard",
        mode="build",
        model="fake-model",
        reasoning_effort="high",
    )
    assistant = await service.start_run(
        session,
        "think hard",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    assert assistant.model == "fake-model"
    assert assistant.reasoning_effort == "high"


@pytest.mark.django_db(transaction=True)
async def test_start_run_snapshots_org_default_when_session_model_empty(
    harness_workspace, monkeypatch
) -> None:
    """Auto (empty session.model) snapshots org default onto the assistant."""
    from apps.harness.providers.models_catalog import ProviderModel
    from apps.harness.providers.openrouter import OpenRouterAdapter
    from apps.harness.services import ProviderConfigService

    org_id = harness_workspace.runner.organization_id
    ProviderConfigService().save_config(
        organization_id=org_id,
        api_key="sk-test",
        base_url="https://example.com/v1",
        default_model="org-default-model",
        small_model="",
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="auto model",
        agent_name="build",
        mode="build",
        model="",
        reasoning_effort="medium",
    )

    async def _scripted_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        yield Delta(text="hello", usage=Usage(1, 1, 2))

    monkeypatch.setattr(OpenRouterAdapter, "chat_stream", _scripted_stream)

    def _fake_list(self, organization_id):  # type: ignore[no-untyped-def]
        return [
            ProviderModel(
                id="org-default-model",
                name="Org Default",
                reasoning_efforts=("medium",),
                default_effort="medium",
                supports_tools=True,
                context_length=128_000,
                max_output_tokens=8_192,
            )
        ]

    monkeypatch.setattr(ProviderConfigService, "list_models", _fake_list)

    collected: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        collected.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
    )
    assistant = await service.start_run(
        session,
        "say hello",
        organization_id=org_id,
        workspace_id=str(harness_workspace.id),
    )
    assert assistant.model == "org-default-model"
    assert assistant.reasoning_effort == "medium"
    refreshed = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert refreshed is not None
    assert refreshed.model == ""
    busy = next(
        item
        for item in collected
        if item.get("event") == FRONTEND_EVENT_STATUS and item.get("status") == "busy"
    )
    assert busy["model"] == "org-default-model"
    assert busy["reasoning_effort"] == "medium"
    await service._tasks[str(session.id)]
    refreshed = await sync_to_async(HarnessSessionRepository.get_by_id)(session.id)
    assert refreshed is not None
    assert refreshed.model == ""


@pytest.mark.django_db(transaction=True)
async def test_set_mode_aligns_agent_name(harness_workspace) -> None:
    """set_mode(plan) also sets agent_name to plan."""
    service, _, _ = _service()
    session = await _db_create_session(harness_workspace)
    assert session.agent_name == "build"
    updated = await sync_to_async(service.set_mode)(session.id, "plan")
    assert updated.mode == "plan"
    assert updated.agent_name == "plan"


@pytest.mark.django_db(transaction=True)
async def test_missing_provider_config_raises(harness_workspace) -> None:
    """start_run without provider config raises NotFoundError before task."""
    collected: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        collected.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
    )
    session = await _db_create_session(harness_workspace)
    session.model = ""
    await sync_to_async(HarnessSessionRepository.set_model)(session, "")
    from common.exceptions import NotFoundError

    with pytest.raises(NotFoundError):
        await service.start_run(
            session,
            "needs config",
            organization_id=harness_workspace.runner.organization_id,
            workspace_id=str(harness_workspace.id),
        )


async def _wait_for_part(
    session_id: uuid.UUID, *, part_type: str, timeout: float = 2.0
):
    """Poll until a part of *part_type* exists for *session_id*."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        parts = await sync_to_async(HarnessPartRepository.list_for_session)(
            session_id
        )
        match = [part for part in parts if part.type == part_type]
        if match:
            return match[0]
        await asyncio.sleep(0.02)
    raise AssertionError(f"no {part_type} part within {timeout}s")


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


@pytest.mark.django_db(transaction=True)
async def test_start_run_uses_accessor_factory_for_list_tool(
    harness_workspace,
) -> None:
    """Workspace tools run through the injected accessor, not MissingAccessor."""
    from apps.harness.tests.conftest import FakeAccessor

    fake_accessor = FakeAccessor(files={"/workspace/readme.txt": b"hi"})
    wired: list[str] = []

    async def factory(workspace_id: str):
        wired.append(workspace_id)
        return fake_accessor

    provider = FakeProvider(
        [
            _tool_step("list", {"path": "/workspace"}),
            _text_step("listed"),
        ]
    )
    collected: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        collected.append({"event": event, **data})

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_emit,
        provider_factory=lambda _org: provider,
        accessor_factory=factory,
    )
    session = await _db_create_session(harness_workspace)
    await service.start_run(
        session,
        "list files",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]

    assert wired == [str(harness_workspace.id)]
    parts = HarnessPartRepository.list_for_session(session.id)
    tool_parts = [part for part in parts if part.type == "tool"]
    assert len(tool_parts) == 1
    assert tool_parts[0].state == "completed"
    assert "readme.txt" in (tool_parts[0].output or "")


@pytest.mark.django_db(transaction=True)
async def test_start_run_without_accessor_factory_errors_list_tool(
    harness_workspace,
) -> None:
    """Unwired production-style service fails workspace tools with a clear error."""
    provider = FakeProvider(
        [
            _tool_step("list", {"path": "/workspace"}),
            _text_step("handled"),
        ]
    )
    service, _, _ = _service(provider=provider)
    session = await _db_create_session(harness_workspace)
    await service.start_run(
        session,
        "list files",
        organization_id=harness_workspace.runner.organization_id,
        workspace_id=str(harness_workspace.id),
    )
    await service._tasks[str(session.id)]
    parts = HarnessPartRepository.list_for_session(session.id)
    tool_parts = [part for part in parts if part.type == "tool"]
    assert tool_parts and tool_parts[0].state == "error"
    assert "No workspace accessor configured" in (tool_parts[0].output or "")


@pytest.mark.django_db(transaction=True)
async def test_parallel_tool_events_persist_distinct_parts_by_call_id(
    harness_workspace,
) -> None:
    """Concurrent tool_started/completed events keep separate parts per call_id."""
    service, _, events = _service()
    session = await _db_create_session(harness_workspace)
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant"
    )
    service._runs[str(session.id)] = {
        "session_id": str(session.id),
        "message_id": str(assistant.id),
        "tool_parts": {},
        "step_parts": {},
        "subtask_parts": {},
    }

    async def start(call_id: str, title: str) -> None:
        await service._on_runner_event(
            session,
            assistant,
            {
                "type": "tool_started",
                "step": 1,
                "call_id": call_id,
                "tool": "read",
                "title": title,
                "arguments": "{}",
            },
        )

    await asyncio.gather(start("c1", "read a"), start("c2", "read b"))
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "tool_completed",
            "step": 1,
            "call_id": "c2",
            "tool": "read",
            "output": "b-content",
        },
    )
    await service._on_runner_event(
        session,
        assistant,
        {
            "type": "tool_completed",
            "step": 1,
            "call_id": "c1",
            "tool": "read",
            "output": "a-content",
        },
    )
    parts = [
        part
        for part in HarnessPartRepository.list_for_session(session.id)
        if part.type == "tool"
    ]
    by_call = {part.call_id: part for part in parts}
    assert set(by_call) == {"c1", "c2"}
    assert by_call["c1"].state == "completed"
    assert by_call["c1"].output == "a-content"
    assert by_call["c2"].state == "completed"
    assert by_call["c2"].output == "b-content"
    started = [
        item
        for item in events
        if item.get("event") == FRONTEND_EVENT_PART
        and "tool_started" in (item.get("delta") or {})
    ]
    completed = [
        item
        for item in events
        if item.get("event") == FRONTEND_EVENT_PART
        and "tool_completed" in (item.get("delta") or {})
    ]
    assert {item["delta"]["call_id"] for item in started} == {"c1", "c2"}
    assert {item["part_id"] for item in started} == {
        str(by_call["c1"].id),
        str(by_call["c2"].id),
    }
    assert {item["delta"]["call_id"] for item in completed} == {"c1", "c2"}
    assert all(item.get("part_id") for item in completed)


@pytest.mark.django_db(transaction=True)
async def test_always_allow_resolves_sibling_pending_asks(
    harness_workspace,
) -> None:
    """Always-allow auto-approves remaining pending asks for the same tool."""
    emitted: list[dict[str, Any]] = []

    async def _emit(event: str, data: dict[str, Any]) -> None:
        emitted.append({"event": event, **data})

    service = HarnessService(emit=_emit)
    session = await _db_create_session(harness_workspace)
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant"
    )
    first = asyncio.create_task(
        service._on_permission(
            session,
            assistant,
            tool="bash",
            action="git status",
            title="$ git status",
            call_id="c1",
        )
    )
    second = asyncio.create_task(
        service._on_permission(
            session,
            assistant,
            tool="bash",
            action="git diff",
            title="$ git diff",
            call_id="c2",
        )
    )
    request_ids: list[str] = []
    for _ in range(50):
        request_ids = [
            str(item["request_id"])
            for item in emitted
            if item.get("event") == FRONTEND_EVENT_PERMISSION
        ]
        if len(request_ids) >= 2:
            break
        await asyncio.sleep(0.02)
    assert len(request_ids) >= 2
    await service.resolve_permission(
        session=session,
        request_id=uuid.UUID(request_ids[0]),
        response="always",
    )
    decisions = await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
    assert set(decisions) == {"once"}
    resolved_events = [
        item
        for item in emitted
        if item.get("event") == FRONTEND_EVENT_PERMISSION and item.get("decision")
    ]
    assert len(resolved_events) == 2
    assert {item["request_id"] for item in resolved_events} == set(request_ids[:2])


@pytest.mark.django_db(transaction=True)
async def test_abort_run_cancels_child_session_task(
    harness_workspace,
) -> None:
    """Aborting a parent also cancels in-flight child session tasks."""
    service, _, _ = _service()
    parent = await _db_create_session(harness_workspace)
    child = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=parent.workspace_id,
        organization_id=parent.organization_id,
        title="child",
        parent_id=parent.id,
        agent_name="explore",
        mode="build",
        model="fake-model",
    )
    hang = asyncio.Event()

    async def never() -> None:
        await hang.wait()

    child_task = asyncio.create_task(never())
    service._tasks[str(child.id)] = child_task
    await service.abort_run(parent.id)
    assert child_task.done()


@pytest.mark.django_db(transaction=True)
async def test_abort_busy_computeruse_leaves_parent_running(
    harness_workspace,
) -> None:
    """Take-control aborts computer-use children without aborting the parent."""
    service, _, _ = _service()
    parent = await _db_create_session(harness_workspace)
    computeruse = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=parent.workspace_id,
        organization_id=parent.organization_id,
        title="cu",
        parent_id=parent.id,
        agent_name="computeruse",
        mode="build",
        model="fake-model",
    )
    explore = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=parent.workspace_id,
        organization_id=parent.organization_id,
        title="explore",
        parent_id=parent.id,
        agent_name="explore",
        mode="build",
        model="fake-model",
    )
    hang = asyncio.Event()

    async def never() -> None:
        await hang.wait()

    parent_task = asyncio.create_task(never())
    cu_task = asyncio.create_task(never())
    explore_task = asyncio.create_task(never())
    service._tasks[str(parent.id)] = parent_task
    service._tasks[str(computeruse.id)] = cu_task
    service._tasks[str(explore.id)] = explore_task
    await sync_to_async(HarnessSessionRepository.mark_status)(parent, "busy")
    await sync_to_async(HarnessSessionRepository.mark_status)(computeruse, "busy")
    await sync_to_async(HarnessSessionRepository.mark_status)(explore, "busy")

    aborted = await service.abort_busy_computeruse_for_workspace(parent.workspace_id)

    assert [session.id for session in aborted] == [computeruse.id]
    assert cu_task.done()
    assert not parent_task.done()
    assert not explore_task.done()
    hang.set()
    parent_task.cancel()
    explore_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await parent_task
    with pytest.raises(asyncio.CancelledError):
        await explore_task
