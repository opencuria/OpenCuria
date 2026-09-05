"""Paket 6: question tool, patch parts, compaction, MCP mark-read."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.compaction import (
    COMPACTION_TOKEN_THRESHOLD,
    apply_compaction_summary,
    build_compaction_prompt,
    should_compact,
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
        step = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
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


def test_compaction_threshold_helper() -> None:
    """Compaction triggers when estimated tokens exceed threshold."""
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="x" * 400_000),
    ]
    assert should_compact(messages, threshold=1000)
    assert not should_compact(messages, threshold=COMPACTION_TOKEN_THRESHOLD * 4)


def test_apply_compaction_summary_replaces_history() -> None:
    """Compaction summary becomes a single user note."""
    messages = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="old"),
        LLMMessage(role="assistant", content="old answer"),
    ]
    compacted = apply_compaction_summary(messages, "summary text")
    assert compacted[0].role == "system"
    assert len(compacted) == 2
    assert "summary text" in (compacted[1].content or "")


@pytest.mark.asyncio
async def test_compaction_runs_before_provider_when_threshold_low() -> None:
    """Oversized history triggers compaction agent using small_model."""
    big = "word " * 50_000
    provider = ScriptProvider(
        [
            [Delta(text="summary only", usage=Usage(1, 1, 2))],
            [Delta(text="final", usage=Usage(1, 1, 2))],
        ]
    )
    runner = HarnessRunner(provider=provider, tools=default_tool_registry())
    result = await runner.run(
        "follow-up",
        "build",
        "big-model",
        "build",
        RunOptions(
            auto_approve=True,
            small_model="small-model",
            compaction_threshold=100,
            history=[
                LLMMessage(role="user", content=big),
                LLMMessage(role="assistant", content=big),
            ],
        ),
    )
    assert result.output == "final"
    assert provider.calls >= 2


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
