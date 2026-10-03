from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessSession
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.services import ProviderConfigService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService
from common.utils import generate_api_token, hash_token


class TestProvider(ProviderAdapter):
    name = "openrouter"

    async def chat_stream(self, model, messages, tools, opts=None):
        yield Delta(text="scheduled answer", usage=Usage(1, 1, 2))


@pytest.fixture
def scheduled_run_setup(db):
    org = Organization.objects.create(
        name="Scheduled run", slug=f"scheduled-run-{uuid.uuid4().hex}"
    )
    user_model = get_user_model()
    owner = user_model.objects.create_user(
        email=f"scheduled-run-{uuid.uuid4().hex}@example.com", password="secret"
    )
    outsider = user_model.objects.create_user(
        email=f"scheduled-outsider-{uuid.uuid4().hex}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=outsider, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        organization=org,
        api_token_hash=uuid.uuid4().hex,
        status=RunnerStatus.ONLINE,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="Scheduled chat workspace",
        status=WorkspaceStatus.RUNNING,
    )
    task = ScheduledTask.objects.create(
        organization=org,
        owner=owner,
        workspace=workspace,
        name="Integration run",
        prompt="Review this workspace",
        recurrence="daily",
        local_time="09:00",
        timezone_name="UTC",
        model="openrouter/openai/gpt-test",
        reasoning_effort="high",
        next_run_at="2030-01-01T09:00:00Z",
    )
    provider_config = ProviderConfigService()
    provider_config.save_config(
        organization_id=org.id,
        default_model="openrouter/openai/gpt-default",
        default_effort="medium",
    )
    provider_config.save_connection(
        organization_id=org.id,
        provider="openrouter",
        credentials={"api_key": "sk-test"},
        config={"base_url": "https://example.com/v1"},
    )
    return org, owner, outsider, workspace, task


def _client(user, org, permissions):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="scheduled run integration",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))


@pytest.mark.django_db(transaction=True)
def test_run_now_links_provider_backed_chat_and_terminal_outcome(
    scheduled_run_setup, monkeypatch
):
    org, owner, _, workspace, task = scheduled_run_setup
    service = HarnessService(provider_factory=lambda _org: TestProvider())
    monkeypatch.setattr(
        "apps.scheduled_tasks.services.get_harness_service", lambda: service
    )
    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: (
            session.model or "openrouter/openai/gpt-test"
        ),
    )
    service._on_run_task_done = lambda task, key: None
    gate = asyncio.Event()

    class SlowProvider(TestProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            await gate.wait()
            yield Delta(text="scheduled answer", usage=Usage(1, 1, 2))

    service._provider_factory = lambda _org: SlowProvider()

    async def complete_run():
        result = await ScheduledTaskService(harness=service).run_now(task)
        gate.set()
        await service._tasks[str(result.session_id)]
        return result

    run = asyncio.run(complete_run())
    assistant = HarnessMessage.objects.get(id=run.assistant_message_id)
    session = HarnessSession.objects.get(id=run.session_id)
    assert run.status == ScheduledTaskRun.Status.RUNNING
    assert assistant.provider == "openrouter"
    assert assistant.model == "openrouter/openai/gpt-test"
    assert assistant.reasoning_effort == "high"
    assert session.workspace_id == workspace.id
    assert session.organization_id == org.id
    assert session.messages.count() == 2
    assistant.refresh_from_db()
    assert assistant.completed_at is not None
    assert assistant.finish == "stop"
    assert assistant.content == "scheduled answer"

    run.refresh_from_db()
    assert run.session_id == session.id
    assert run.assistant_message_id == assistant.id
    assert run.status == ScheduledTaskRun.Status.SUCCEEDED
    assert run.finished_at == assistant.completed_at
    assert run.error == ""


@pytest.mark.django_db(transaction=True)
def test_rest_run_now_default_model_and_effort_and_unauthorized_actions(
    scheduled_run_setup, monkeypatch
):
    org, owner, outsider, workspace, task = scheduled_run_setup
    provider_config = ProviderConfigService()
    provider_config.save_config(
        organization_id=org.id,
        default_model="openrouter/openai/gpt-default",
        default_effort="medium",
    )
    provider_config.save_connection(
        organization_id=org.id,
        provider="openrouter",
        credentials={"api_key": "sk-test"},
        config={"base_url": "https://example.com/v1"},
    )
    from apps.harness.models import AgentConfig

    AgentConfig.objects.update_or_create(
        organization=org,
        agent="build",
        defaults={"model": "openrouter/openai/gpt-agent", "effort": "low"},
    )
    from apps.harness.harness_service import reset_default_harness_service

    reset_default_harness_service()
    harness = HarnessService(provider_factory=lambda _org: TestProvider())
    monkeypatch.setattr(
        "apps.scheduled_tasks.services.get_harness_service", lambda: harness
    )
    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: (
            session.model
            or HarnessService._agent_defaults(organization_id, "build")["model"]
        ),
    )
    authorized = _client(
        owner,
        org,
        [APIKeyPermission.HARNESS_READ.value, APIKeyPermission.HARNESS_RUN.value],
    )
    create = authorized.post(
        "/api/v1/scheduled-tasks/",
        data=json.dumps(
            {
                "workspace_id": str(workspace.id),
                "name": "Default semantics",
                "prompt": "Summarize",
                "recurrence": "daily",
                "local_time": "09:00",
                "timezone_name": "UTC",
            }
        ),
        content_type="application/json",
    )
    assert create.status_code == 201, create.content
    task_id = create.json()["id"]
    response = authorized.post(f"/api/v1/scheduled-tasks/{task_id}/run/")
    assert response.status_code == 202, response.content
    run = ScheduledTaskRun.objects.get(id=response.json()["id"])
    assistant = HarnessMessage.objects.get(id=run.assistant_message_id)
    asyncio.run(asyncio.sleep(0))
    assistant.refresh_from_db()
    assert assistant.model == "openrouter/openai/gpt-agent"
    assert assistant.reasoning_effort == "low"

    read_only = _client(owner, org, [APIKeyPermission.HARNESS_READ.value])
    assert (
        read_only.patch(
            f"/api/v1/scheduled-tasks/{task_id}/",
            data=json.dumps({"name": "No"}),
            content_type="application/json",
        ).status_code
        == 403
    )
    assert read_only.delete(f"/api/v1/scheduled-tasks/{task_id}/").status_code == 403
    assert read_only.post(f"/api/v1/scheduled-tasks/{task_id}/run/").status_code == 403
    assert read_only.get(f"/api/v1/scheduled-tasks/{task_id}/runs/").status_code == 200

    unauthorized = _client(outsider, org, [APIKeyPermission.HARNESS_RUN.value])
    assert (
        unauthorized.patch(
            f"/api/v1/scheduled-tasks/{task_id}/",
            data=json.dumps({"name": "No"}),
            content_type="application/json",
        ).status_code
        == 404
    )
    assert unauthorized.delete(f"/api/v1/scheduled-tasks/{task_id}/").status_code == 404
    assert (
        unauthorized.post(f"/api/v1/scheduled-tasks/{task_id}/run/").status_code == 404
    )
    assert (
        unauthorized.get(f"/api/v1/scheduled-tasks/{task_id}/runs/").status_code == 403
    )


@pytest.mark.django_db
def test_delete_task_keeps_workspace_session_and_run_history(scheduled_run_setup):
    org, owner, _, workspace, task = scheduled_run_setup
    session = HarnessSession.objects.create(
        workspace=workspace,
        organization_id=org.id,
        mode="build",
        agent_name="build",
    )
    run = ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for="2030-01-01T09:00:00Z",
        session=session,
        status=ScheduledTaskRun.Status.SUCCEEDED,
    )
    ScheduledTaskService().delete(task)
    task.refresh_from_db()
    run.refresh_from_db()
    workspace.refresh_from_db()
    session.refresh_from_db()
    assert task.is_deleted and not task.enabled
    assert run.scheduled_task_id == task.id
    assert run.session_id == session.id
    assert workspace.pk == task.workspace_id
    assert session.pk == run.session_id


@pytest.mark.django_db(transaction=True)
async def test_mcp_run_now_requires_permission_and_owner_scope(
    scheduled_run_setup, monkeypatch
):
    from apps.mcp_app.server import (
        _TOOL_HANDLERS,
        _TOOL_PERMISSIONS,
        _call_delete_scheduled_task,
        _call_list_scheduled_task_runs,
        _call_run_scheduled_task_now,
        _call_update_scheduled_task,
        create_mcp_server,
    )
    from apps.mcp_app.tests.test_scheduled_tasks import FakeAPIKey, parse

    org, owner, outsider, _, task = scheduled_run_setup
    gate = asyncio.Event()

    class SlowProvider(TestProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            await gate.wait()
            yield Delta(text="scheduled answer", usage=Usage(1, 1, 2))

    harness = HarnessService(provider_factory=lambda _org: SlowProvider())
    monkeypatch.setattr(
        "apps.scheduled_tasks.services.get_harness_service", lambda: harness
    )
    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: (
            session.model or "openrouter/openai/gpt-default"
        ),
    )
    read_key = FakeAPIKey(owner, [APIKeyPermission.HARNESS_READ])
    import mcp.types as mcp_types

    list_handler = create_mcp_server(read_key).request_handlers[
        mcp_types.ListToolsRequest
    ]
    listing = await list_handler(mcp_types.ListToolsRequest(method="tools/list"))
    listed = {tool.name for tool in listing.root.tools}
    assert "run_scheduled_task_now" not in listed
    assert "update_scheduled_task" not in listed
    assert _TOOL_PERMISSIONS["run_scheduled_task_now"] is APIKeyPermission.HARNESS_RUN
    assert _TOOL_PERMISSIONS["update_scheduled_task"] is APIKeyPermission.HARNESS_RUN
    assert _TOOL_HANDLERS["run_scheduled_task_now"] is not None

    from mcp.types import CallToolRequest, CallToolRequestParams

    read_server = create_mcp_server(read_key)
    denied = await read_server.request_handlers[CallToolRequest](
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(
                name="run_scheduled_task_now", arguments={"task_id": str(task.id)}
            ),
        )
    )
    assert "Permission denied" in denied.root.content[0].text

    run_key = FakeAPIKey(owner, [APIKeyPermission.HARNESS_RUN])
    outsider_key = FakeAPIKey(outsider, [APIKeyPermission.HARNESS_RUN])
    hidden = parse(
        await _call_run_scheduled_task_now(
            outsider_key, org.id, {"task_id": str(task.id)}
        )
    )
    assert "not found" in hidden["error"].lower()
    hidden_update = parse(
        await _call_update_scheduled_task(
            outsider_key, org.id, {"task_id": str(task.id), "name": "No"}
        )
    )
    hidden_delete = parse(
        await _call_delete_scheduled_task(
            outsider_key, org.id, {"task_id": str(task.id)}
        )
    )
    hidden_history = parse(
        await sync_to_async(_call_list_scheduled_task_runs)(
            outsider_key, org.id, {"task_id": str(task.id)}
        )
    )
    assert "not found" in hidden_update["error"].lower()
    assert "not found" in hidden_delete["error"].lower()
    assert "not found" in hidden_history["error"].lower()
    for tool_name in (
        "update_scheduled_task",
        "delete_scheduled_task",
        "run_scheduled_task_now",
    ):
        denied = await read_server.request_handlers[CallToolRequest](
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(
                    name=tool_name, arguments={"task_id": str(task.id)}
                ),
            )
        )
        assert "Permission denied" in denied.root.content[0].text
    actual = parse(
        await _call_run_scheduled_task_now(run_key, org.id, {"task_id": str(task.id)})
    )
    assert actual["status"] == ScheduledTaskRun.Status.RUNNING
    assert actual["session_id"]
    session_id = actual["session_id"]
    gate.set()
    await harness._tasks[session_id]
    assistant = await sync_to_async(HarnessMessage.objects.get)(
        session_id=session_id, role="assistant"
    )
    assert assistant.finish == "stop"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("outcome", ["error", "abort"])
def test_real_harness_failure_and_abort_settle_before_idle(
    scheduled_run_setup, monkeypatch, outcome
):
    _, _, _, _, task = scheduled_run_setup
    gate = asyncio.Event()
    entered = asyncio.Event()
    idle_statuses = []

    class FailingProvider(TestProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            entered.set()
            await gate.wait()
            raise RuntimeError("scheduled provider failed")
            yield Delta(text="unreachable")

    async def capture_emit(event, payload, **kwargs):
        if payload.get("status") == "idle":
            status = await sync_to_async(
                lambda: ScheduledTaskRun.objects.get(scheduled_task=task).status
            )()
            idle_statuses.append(status)

    harness = HarnessService(
        provider_factory=lambda _org: FailingProvider(), emit=capture_emit
    )
    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: session.model,
    )
    harness._on_run_task_done = lambda task, key: None

    async def complete():
        run = await ScheduledTaskService(harness=harness).run_now(task)
        running = harness._tasks[str(run.session_id)]
        await asyncio.wait_for(entered.wait(), timeout=5)
        if outcome == "abort":
            await harness.abort_run(run.session_id)
        else:
            gate.set()
        await asyncio.gather(running, return_exceptions=True)
        return run

    run = asyncio.run(complete())
    run.refresh_from_db()
    assistant = HarnessMessage.objects.get(id=run.assistant_message_id)
    assert assistant.completed_at is not None
    assert assistant.finish == ("aborted" if outcome == "abort" else "error")
    assert run.status == ScheduledTaskRun.Status.ERROR
    assert run.finished_at == assistant.completed_at
    assert run.error == assistant.error
    expected_error = (
        "aborted by user" if outcome == "abort" else "scheduled provider failed"
    )
    assert expected_error in run.error
    assert idle_statuses == [ScheduledTaskRun.Status.ERROR]


@pytest.mark.django_db
@pytest.mark.parametrize("access", ["other_owner", "no_read"])
def test_unauthorized_history_cannot_repair_completed_run(scheduled_run_setup, access):
    from django.utils import timezone

    org, owner, outsider, workspace, task = scheduled_run_setup
    session = HarnessSession.objects.create(workspace=workspace, organization_id=org.id)
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", completed_at=timezone.now(), finish="stop"
    )
    run = ScheduledTaskRun.objects.create(
        scheduled_task=task,
        scheduled_for=timezone.now(),
        session=session,
        assistant_message=assistant,
        status=ScheduledTaskRun.Status.RUNNING,
    )
    client = _client(
        outsider if access == "other_owner" else owner,
        org,
        [APIKeyPermission.HARNESS_READ.value]
        if access == "other_owner"
        else [APIKeyPermission.HARNESS_RUN.value],
    )
    response = client.get(f"/api/v1/scheduled-tasks/{task.id}/runs/")
    assert response.status_code == (404 if access == "other_owner" else 403)
    run.refresh_from_db()
    assert run.status == ScheduledTaskRun.Status.RUNNING
    assert run.finished_at is None
    assert not run.completion_check_pending


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("failure_phase", ["admission", "finalization"])
def test_projection_failure_preserves_live_run_and_idle_cleanup_then_history_repairs(
    scheduled_run_setup, monkeypatch, failure_phase
):
    _, _, _, _, task = scheduled_run_setup
    gate = asyncio.Event()
    idle_statuses = []
    projection_calls = []
    repairs = []
    callbacks = []
    original_projection = ScheduledTaskService.complete_assistant_run

    def project(service, message_id):
        projection_calls.append(message_id)
        phase = "admission" if len(projection_calls) == 1 else "finalization"
        if phase == failure_phase:
            raise RuntimeError("completion projection unavailable")
        if failure_phase == "admission" and phase == "finalization":
            # The completed assistant is now durable; exercise scoped recovery
            # before the normal finalization projection can settle the ledger.
            repairs.append(service.list_runs(task)[0].status)
        original_projection(service, message_id)

    class SlowProvider(TestProvider):
        async def chat_stream(self, model, messages, tools, opts=None):
            await gate.wait()
            yield Delta(text="scheduled answer", usage=Usage(1, 1, 2))

    async def capture_emit(event, payload):
        if payload.get("status") == "idle":
            idle_statuses.append(
                await sync_to_async(
                    lambda: ScheduledTaskRun.objects.get(scheduled_task=task).status
                )()
            )

    harness = HarnessService(
        provider_factory=lambda _org: SlowProvider(), emit=capture_emit
    )
    original_callback = harness._on_run_task_done

    def run_done(background, key):
        callbacks.append(key)
        original_callback(background, key)

    monkeypatch.setattr(harness, "_on_run_task_done", run_done)
    monkeypatch.setattr(ScheduledTaskService, "complete_assistant_run", project)
    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: session.model,
    )

    async def complete():
        run = await ScheduledTaskService(harness=harness).run_now(task)
        key = str(run.session_id)
        background = harness._tasks[key]
        await sync_to_async(run.refresh_from_db)()
        assert run.status == ScheduledTaskRun.Status.RUNNING
        assert run.error == "" and run.finished_at is None
        assert not background.done()
        gate.set()
        await background
        await asyncio.sleep(0)  # Let the actual completion callback execute.
        assert callbacks == [key]
        assert key not in harness._tasks
        assert key not in harness._runs
        assert key not in harness._event_locks
        assert key not in harness._admissions
        return run

    run = asyncio.run(complete())
    run.refresh_from_db()
    assistant = HarnessMessage.objects.get(id=run.assistant_message_id)
    assert assistant.completed_at is not None and assistant.finish == "stop"
    assert HarnessSession.objects.get(id=run.session_id).status == "idle"
    assert projection_calls == [assistant.id, assistant.id]
    if failure_phase == "finalization":
        assert idle_statuses == [ScheduledTaskRun.Status.RUNNING]
        assert run.status == ScheduledTaskRun.Status.RUNNING
        assert run.error == "" and run.finished_at is None
    else:
        assert repairs == [ScheduledTaskRun.Status.SUCCEEDED]
        assert idle_statuses == [ScheduledTaskRun.Status.SUCCEEDED]
    repaired = ScheduledTaskService().list_runs(task)[0]
    assert repaired.status == ScheduledTaskRun.Status.SUCCEEDED
    assert repaired.finished_at == assistant.completed_at
    assert repaired.error == ""
