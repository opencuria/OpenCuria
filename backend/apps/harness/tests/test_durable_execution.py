"""Run versioning, deferred gates and fail-closed execution integration tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from asgiref.sync import sync_to_async

from apps.harness.gateway import HarnessWorkerService
from apps.harness.models import HarnessRun, HarnessSession
from apps.harness.tests.test_harness_service import FakeProvider, _text_step, _tool_step
from apps.harness.worker_protocol import HarnessDeployment, HarnessWorkerRepository


@pytest.mark.django_db(transaction=True)
async def test_native_worker_run_finishes_durable_attempt(harness_workspace):
    """Real native loop finalizes all four durable facts, without any LLM call."""
    provider = FakeProvider([_text_step("durable answer")])
    deployment = HarnessDeployment("worker", "native-v1", "native-worker")
    await sync_to_async(HarnessWorkerRepository.register)(
        deployment.worker_id, deployment.version, pid=1
    )
    service = HarnessWorkerService(
        deployment=deployment,
        gateway=False,
        provider_factory=lambda _: provider,
        emit=AsyncMock(),
    )
    session = await sync_to_async(HarnessSession.objects.create)(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        model="fake/model",
    )
    assistant = await service.start_run(session, "prompt")
    await service._tasks[str(session.id)]
    await sync_to_async(assistant.refresh_from_db)()
    await sync_to_async(session.refresh_from_db)()
    run = await sync_to_async(HarnessRun.objects.get)(assistant_message=assistant)
    assert run.status == "completed"
    assert run.owner_worker_id == "native-worker"
    assert run.deployment_version == "native-v1"
    assert assistant.completed_at is not None and assistant.content == "durable answer"
    assert session.status == "idle"


@pytest.mark.django_db(transaction=True)
async def test_native_gate_answer_from_another_service_resumes_owner(harness_workspace):
    """A replacement gateway persists an answer; the owning worker polls it."""
    provider = FakeProvider(
        [
            _tool_step("question", {"questions": [{"question": "Continue?"}]}),
            _text_step("continued on original version"),
        ]
    )
    deployment = HarnessDeployment("worker", "gate-v1", "gate-worker")
    await sync_to_async(HarnessWorkerRepository.register)(
        deployment.worker_id, deployment.version, pid=2
    )
    worker = HarnessWorkerService(
        deployment=deployment,
        gateway=False,
        provider_factory=lambda _: provider,
        emit=AsyncMock(),
    )
    session = await sync_to_async(HarnessSession.objects.create)(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        model="fake/model",
    )
    assistant = await worker.start_run(session, "ask")
    task = worker._tasks[str(session.id)]
    from apps.harness.models import QuestionRequest

    for _ in range(100):
        request = await sync_to_async(
            lambda: QuestionRequest.objects.filter(session_id=session.id).first()
        )()
        if request:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("Question was not persisted")
    await sync_to_async(HarnessWorkerRepository.set_draining)("gate-worker")
    gateway = HarnessWorkerService(
        deployment=HarnessDeployment("gateway", "gate-v2", ""),
        gateway=True,
        emit=AsyncMock(),
    )
    await gateway.resolve_question(
        session=session, question_id=request.id, answers=["yes"]
    )
    from apps.harness.management.commands.run_harness_worker import Command

    await Command._handle_gate_resolutions(worker, {"unused": task})
    await asyncio.wait_for(task, 5)
    run = await sync_to_async(HarnessRun.objects.get)(assistant_message=assistant)
    assert run.status == "completed"
    assert run.deployment_version == "gate-v1"


@pytest.mark.django_db(transaction=True)
async def test_critical_tool_commit_failure_does_not_execute(harness_workspace):
    """No tool effect may happen after failure to persist its durable start."""
    provider = FakeProvider([_tool_step("bash", {"command": "echo unsafe"})])
    from apps.harness.tests.conftest import FakeAccessor

    accessor = FakeAccessor()
    service = HarnessWorkerService(
        deployment=HarnessDeployment("worker", "critical-v1", "critical-worker"),
        gateway=False,
        provider_factory=lambda _: provider,
        emit=AsyncMock(),
        accessor_factory=AsyncMock(return_value=accessor),
    )
    await sync_to_async(HarnessWorkerRepository.register)(
        "critical-worker", "critical-v1", pid=3
    )
    session = await sync_to_async(HarnessSession.objects.create)(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        model="fake/model",
    )
    original = service._persist_runner_event

    async def fail_start(session, assistant, event):
        if event["type"] == "tool_started":
            raise OSError("database unavailable")
        await original(session, assistant, event)

    service._persist_runner_event = fail_start
    assistant = await service.start_run(session, "must not execute")
    await service._tasks[str(session.id)]
    assert accessor.exec_calls == []
    run = await sync_to_async(HarnessRun.objects.get)(assistant_message=assistant)
    assert run.status == "closing"
    await sync_to_async(session.refresh_from_db)()
    assert session.status == "busy"


@pytest.mark.django_db(transaction=True)
async def test_uncertain_runner_effect_blocks_next_tool(harness_workspace):
    """A deadline must not be converted into an ordinary retryable tool result."""
    from apps.harness.runtime_authority import mark_current_run_uncertain
    from apps.harness.tests.conftest import FakeAccessor

    class UncertainAccessor(FakeAccessor):
        async def exec_wait(self, *args, **kwargs):
            mark_current_run_uncertain()
            raise TimeoutError("effect outcome unknown")

    provider = FakeProvider(
        [
            _tool_step("bash", {"command": "echo first"}),
            _tool_step("bash", {"command": "echo must-not-run"}, call_id="second"),
        ]
    )
    accessor = UncertainAccessor()
    service = HarnessWorkerService(
        deployment=HarnessDeployment("worker", "uncertain-v1", "uncertain-worker"),
        gateway=False,
        provider_factory=lambda _: provider,
        emit=AsyncMock(),
        accessor_factory=AsyncMock(return_value=accessor),
    )
    await sync_to_async(HarnessWorkerRepository.register)(
        "uncertain-worker", "uncertain-v1", pid=4
    )
    session = await sync_to_async(HarnessSession.objects.create)(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        model="fake/model",
    )
    assistant = await service.start_run(session, "unknown effect")
    await service._tasks[str(session.id)]
    run = await sync_to_async(HarnessRun.objects.get)(assistant_message=assistant)
    assert run.status == "closing"
    assert len(provider._steps) == 1  # second provider/tool step was never reached


@pytest.mark.django_db(transaction=True)
async def test_managed_external_engine_uses_its_original_owner(harness_workspace):
    """Managed wrapper delegates Claude without native provider arguments."""
    import uuid
    from apps.harness.tests.test_claude_harness_service import _service, _new_session

    legacy, registry, connections, _events = _service(connection_id=uuid.uuid4())
    session = await _new_session(legacy, harness_workspace)
    deployment = HarnessDeployment("worker", "claude-v1", "claude-worker")
    await sync_to_async(HarnessWorkerRepository.register)(
        deployment.worker_id, deployment.version, pid=9
    )
    worker = HarnessWorkerService(
        deployment=deployment,
        gateway=False,
        engine_registry=registry,
        engine_connections=connections,
        accessor_factory=legacy._accessor_factory,
        emit=AsyncMock(),
    )
    assistant = await worker.start_run(
        session, "managed external run", user_id=harness_workspace.created_by_id
    )
    await worker._tasks[str(session.id)]
    await sync_to_async(assistant.refresh_from_db)()
    run = await sync_to_async(HarnessRun.objects.get)(assistant_message=assistant)
    assert run.deployment_version == "claude-v1"
    assert run.owner_worker_id == "claude-worker"
    assert run.status == "completed"
    assert assistant.finish == "stop"


@pytest.mark.django_db(transaction=True)
async def test_subagent_started_after_drain_inherits_worker_and_release(
    harness_workspace,
):
    """Local child admission does not depend on a new ready root worker."""
    provider = FakeProvider(
        [
            _tool_step(
                "task",
                {
                    "description": "verify child",
                    "prompt": "child prompt",
                    "subagent_type": "general",
                },
            ),
            _text_step("child result"),
            _text_step("root result"),
        ]
    )
    deployment = HarnessDeployment("worker", "tree-v1", "tree-worker")
    await sync_to_async(HarnessWorkerRepository.register)(
        "tree-worker", "tree-v1", pid=10
    )
    worker = HarnessWorkerService(
        deployment=deployment,
        gateway=False,
        provider_factory=lambda _: provider,
        emit=AsyncMock(),
    )
    session = await sync_to_async(HarnessSession.objects.create)(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        model="fake/model",
    )
    original = worker._persist_runner_event

    async def drain_at_task_start(session, assistant, event):
        if event["type"] == "tool_started" and event.get("tool") == "task":
            await sync_to_async(HarnessWorkerRepository.set_draining)("tree-worker")
        await original(session, assistant, event)

    worker._persist_runner_event = drain_at_task_start
    assistant = await worker.start_run(session, "delegate child")
    await asyncio.wait_for(worker._tasks[str(session.id)], 8)
    rows = await sync_to_async(
        lambda: list(HarnessRun.objects.filter(deployment_version="tree-v1"))
    )()
    assert len(rows) == 2
    root = next(row for row in rows if row.root_run_id is None)
    child = next(row for row in rows if row.root_run_id is not None)
    assert child.root_run_id == root.id
    assert {row.owner_worker_id for row in rows} == {"tree-worker"}
    assert {row.status for row in rows} == {"completed"}
