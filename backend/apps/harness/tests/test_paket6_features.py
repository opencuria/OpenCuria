"""Paket 6: question tool, patch parts, compaction, MCP mark-read."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.compaction import (
    CHECKPOINT_PREFIX,
    ModelLimits,
    apply_compaction,
    is_overflow,
    select,
)
from apps.harness.harness_service import HarnessService
from apps.harness.models import QuestionRequest
from apps.harness.providers.base import Delta, LLMMessage, ProviderAdapter, ToolSchema, Usage
from apps.harness.repositories import QuestionRequestRepository
from apps.harness.runner import HarnessRunner, RunOptions
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tools import default_tool_registry
from apps.harness.tools.files import EditTool, WriteTool
from apps.harness.tools.base import ToolContext


class ScriptProvider(ProviderAdapter):
    """Provider that plays back scripted steps."""

    name = "script"

    def __init__(self, steps: list[list[Delta]]) -> None:
        self._steps = [list(step) for step in steps]
        self.calls = 0

    async def chat_stream(  # type: ignore[no-untyped-def]
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts=None,
    ) -> AsyncIterator[Delta]:
        self.calls += 1
        if not self._steps:
            yield Delta(text="done", usage=Usage(0, 0, 0))
            return
        step = self._steps.pop(0)
        for delta in step:
            yield delta


def _tool_step(tool: str, args: dict[str, Any], *, call_id: str = "call-1"):
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
            usage=Usage(1, 1, 2),
        )
    ]


@pytest.mark.asyncio
async def test_question_tool_pauses_until_resolve() -> None:
    """Question tool waits on on_question and resumes with answers."""
    gate = asyncio.Event()
    answers_box: list[list[Any]] = []

    async def on_question(**kwargs: Any) -> list[Any]:
        answers_box.append(list(kwargs.get("questions") or []))
        await gate.wait()
        return ["blue"]

    provider = ScriptProvider(
        [
            _tool_step(
                "question",
                {
                    "questions": [
                        {
                            "question": "Pick a color",
                            "options": [{"label": "blue"}],
                        }
                    ]
                },
            ),
            [Delta(text="thanks", usage=Usage(1, 1, 2))],
        ]
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        emit=emit,
    )
    task = asyncio.create_task(
        runner.run(
            "go",
            "build",
            "model",
            "build",
            RunOptions(on_question=on_question, auto_approve=True),
        )
    )
    await asyncio.sleep(0.05)
    assert not task.done()
    gate.set()
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result.output == "thanks"
    assert answers_box
    assert events[-1]["type"] != "tool_error"


@pytest.mark.asyncio
async def test_question_timeout_is_tool_error() -> None:
    """Question tool fails when on_question never resolves."""

    async def on_question(**kwargs: Any) -> list[Any]:
        await asyncio.sleep(1.0)
        return []

    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    provider = ScriptProvider([_tool_step("question", {"questions": [{"question": "Hi?"}]})])
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        emit=emit,
    )
    await runner.run(
        "go",
        "build",
        "model",
        "build",
        RunOptions(on_question=on_question, question_timeout=0.05, auto_approve=True),
    )
    errors = [event for event in events if event.get("type") == "tool_error"]
    assert errors
    assert "timed out" in str(errors[0].get("error", "")).lower()


@pytest.mark.asyncio
async def test_write_tool_emits_patch_metadata() -> None:
    """Write tool returns unified diff metadata."""
    accessor = FakeAccessor(files={"/workspace/a.txt": b"old\n"})
    tool = WriteTool()
    ctx = ToolContext(session_id="s", workspace_id="w", accessor=accessor)
    result = await tool.execute({"path": "a.txt", "content": "new\n"}, ctx)
    assert "unified_diff" in result.metadata
    assert "-old" in result.metadata["unified_diff"]
    assert "+new" in result.metadata["unified_diff"]


@pytest.mark.asyncio
async def test_edit_tool_emits_patch_metadata() -> None:
    """Edit tool returns unified diff metadata."""
    accessor = FakeAccessor(files={"/workspace/a.txt": b"foo bar"})
    tool = EditTool()
    ctx = ToolContext(session_id="s", workspace_id="w", accessor=accessor)
    result = await tool.execute(
        {"path": "a.txt", "old_string": "foo", "new_string": "baz"},
        ctx,
    )
    assert result.metadata.get("path")
    assert "foo" in result.metadata.get("old_content", "")
    assert "baz" in result.metadata.get("new_content", "")


@pytest.mark.asyncio
async def test_runner_emits_patch_event_after_write() -> None:
    """Successful write/edit emits a patch event with diff payload."""
    provider = ScriptProvider(
        [
            _tool_step("write", {"path": "a.txt", "content": "hello"}),
            [Delta(text="done", usage=Usage(1, 1, 2))],
        ]
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        accessor=FakeAccessor(files={}),
        emit=emit,
    )
    await runner.run("go", "build", "model", "build", RunOptions(auto_approve=True))
    patch_events = [event for event in events if event.get("type") == "patch"]
    assert patch_events
    assert patch_events[0].get("unified_diff")


def test_is_overflow_helper() -> None:
    """Overflow uses model limits, not message size estimates."""
    limits = ModelLimits(context_length=1_000, max_output_tokens=100)
    assert is_overflow(
        prompt_tokens=500,
        completion_tokens=500,
        limits=limits,
    )
    assert not is_overflow(
        prompt_tokens=400,
        completion_tokens=400,
        limits=limits,
    )


def test_apply_compaction_keeps_current_user_prompt() -> None:
    """Compaction preserves the latest real user message in the tail."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="a" * 8_000, message_id="old-user"),
        LLMMessage(role="assistant", content="old answer"),
        LLMMessage(role="user", content="current prompt", message_id="current-user"),
    ]
    compacted, tail_start_id = apply_compaction(
        messages,
        "## Objective\n- continue work",
        limits,
    )
    assert compacted[0].role == "system"
    assert any(
        message.role == "user"
        and isinstance(message.content, str)
        and message.content.startswith(CHECKPOINT_PREFIX)
        for message in compacted
    )
    assert compacted[-1].content == "current prompt"
    assert compacted[-1].message_id == "current-user"
    assert tail_start_id == "current-user"


def test_select_splits_head_and_tail() -> None:
    """Older turns become head; recent tail including current user is kept."""
    limits = ModelLimits(context_length=10_000, max_output_tokens=2_000)
    messages = [
        LLMMessage(role="user", content="a" * 8_000, message_id="old-user"),
        LLMMessage(role="assistant", content="old answer"),
        LLMMessage(role="user", content="current", message_id="current-user"),
    ]
    selection = select(messages, limits)
    assert selection.head
    assert selection.tail
    assert selection.tail[-1].content == "current"
    assert selection.tail_start_id == "current-user"


@pytest.mark.asyncio
async def test_compaction_runs_before_provider_when_overflow() -> None:
    """Prior-step overflow triggers compaction on the session model."""
    models_used: list[str] = []
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    class TrackingProvider(ScriptProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            models_used.append(model)
            async for delta in super().chat_stream(model, messages, tools, opts):
                yield delta

    provider = TrackingProvider(
        [
            [Delta(text="## Objective\n- summary", usage=Usage(1, 1, 2))],
            [Delta(text="final", usage=Usage(1, 1, 2))],
        ]
    )
    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        emit=emit,
    )
    result = await runner.run(
        "follow-up",
        "build",
        "session-model",
        "build",
        RunOptions(
            auto_approve=True,
            context_length=1_000,
            max_output_tokens=100,
            last_step_prompt_tokens=500,
            last_step_completion_tokens=500,
            current_user_message_id="current-user",
            history=[
                LLMMessage(role="user", content="prior"),
                LLMMessage(role="assistant", content="prior answer"),
            ],
        ),
    )
    assert result.output == "final"
    assert provider.calls >= 2
    assert "session-model" in models_used
    assert "small-model" not in models_used
    compaction_events = [event for event in events if event.get("type") == "compaction"]
    assert compaction_events
    assert compaction_events[0].get("overflow") is False


@pytest.mark.asyncio
async def test_compaction_child_emit_does_not_leak_to_parent() -> None:
    """Compaction summary text must not stream into parent assistant deltas."""
    provider = ScriptProvider(
        [
            [Delta(text="## Objective\n- leaked summary", usage=Usage(1, 1, 2))],
            [Delta(text="final", usage=Usage(1, 1, 2))],
        ]
    )
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    runner = HarnessRunner(
        provider=provider,
        tools=default_tool_registry(),
        emit=emit,
    )
    result = await runner.run(
        "follow-up",
        "build",
        "session-model",
        "build",
        RunOptions(
            auto_approve=True,
            context_length=1_000,
            max_output_tokens=100,
            last_step_prompt_tokens=500,
            last_step_completion_tokens=500,
            current_user_message_id="current-user",
            history=[
                LLMMessage(role="user", content="prior"),
                LLMMessage(role="assistant", content="prior answer"),
            ],
        ),
    )
    assert result.output == "final"
    text_deltas = [
        event
        for event in events
        if event.get("type") == "part_updated"
        and str((event.get("delta") or {}).get("text", ""))
    ]
    assert not any("leaked summary" in str(event.get("delta", {}).get("text", "")) for event in text_deltas)
    compaction_events = [event for event in events if event.get("type") == "compaction"]
    assert compaction_events
    assert "leaked summary" in compaction_events[0].get("summary", "")


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_resolve_question_via_service(db) -> None:
    """HarnessService.resolve_question stores answers and completes future."""
    from django.contrib.auth import get_user_model

    from apps.organizations.models import Membership, MembershipRole, Organization
    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user_model = get_user_model()
    org = Organization.objects.create(name="Q Org", slug=f"q-org-{uuid.uuid4().hex[:8]}")
    user = user_model.objects.create_user(
        email=f"q-user-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.MEMBER)
    runner = Runner.objects.create(
        name="q-runner",
        api_token_hash=hash_token(f"q-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"q-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Q WS",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    service = HarnessService()
    session = service.create_session(
        workspace_id=workspace.id,
        organization_id=org.id,
        prompt="hi",
    )
    request = QuestionRequestRepository.create(
        organization_id=org.id,
        session_id=session.id,
        workspace_id=workspace.id,
        questions=[{"question": "Color?"}],
    )
    future: asyncio.Future[list[Any]] = asyncio.get_running_loop().create_future()
    service._pending_questions[str(request.id)] = future
    outcome = await service.resolve_question(
        session=session,
        question_id=request.id,
        answers=["blue"],
    )
    assert outcome["status"] == "answered"
    assert future.done()
    assert future.result() == ["blue"]
    stored = QuestionRequest.objects.get(id=request.id)
    assert stored.answers == ["blue"]


@pytest.mark.django_db
def test_question_request_model_persists() -> None:
    """QuestionRequest rows store questions and answers."""
    org_id = uuid.uuid4()
    session_id = uuid.uuid4()
    request = QuestionRequestRepository.create(
        organization_id=org_id,
        session_id=session_id,
        questions=[{"question": "Name?"}],
    )
    QuestionRequestRepository.resolve(request, answers=["Ada"], status="answered")
    fresh = QuestionRequestRepository.get_by_id(request.id)
    assert fresh is not None
    assert fresh.answers == ["Ada"]


@pytest.mark.django_db
def test_list_pending_questions_for_session() -> None:
    """Pending questions are listed per session and skip resolved rows."""
    org_id = uuid.uuid4()
    session_id = uuid.uuid4()
    pending = QuestionRequestRepository.create(
        organization_id=org_id,
        session_id=session_id,
        questions=[{"question": "Color?"}],
    )
    answered = QuestionRequestRepository.create(
        organization_id=org_id,
        session_id=session_id,
        questions=[{"question": "Name?"}],
    )
    QuestionRequestRepository.resolve(answered, answers=["Ada"], status="answered")
    QuestionRequestRepository.create(
        organization_id=org_id,
        session_id=uuid.uuid4(),
        questions=[{"question": "Other session"}],
    )
    rows = QuestionRequestRepository.list_pending_for_session(session_id)
    assert [row.id for row in rows] == [pending.id]
