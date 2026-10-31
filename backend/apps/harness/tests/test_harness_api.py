"""Tests for M6 harness REST contracts: auth, scope, key permissions."""

from __future__ import annotations

import json
import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

import apps.harness.api as harness_api
from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession, Todo
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.repositories import PermissionRequestRepository
from apps.harness.permissions.service import PermissionService
from apps.harness.providers.base import Delta, ProviderAdapter, Usage
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    QuestionRequestRepository,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def harness_setup(db):
    """Org + users + runner + workspaces for harness API tests."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Harness API {uuid.uuid4().hex[:6]}",
        slug=f"harness-api-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"h-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"h-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="harness-api-runner",
        api_token_hash=hash_token(f"harness-api-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"harness-api-{uuid.uuid4().hex[:8]}",
        organization=org,
    )
    owned = Workspace.objects.create(
        runner=runner,
        name="Owned",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    foreign = Workspace.objects.create(
        runner=runner,
        name="Foreign",
        status=WorkspaceStatus.RUNNING,
        created_by=stranger,
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "runner": runner,
        "owned": owned,
        "foreign": foreign,
    }


class FakeProvider(ProviderAdapter):
    """Immediate text answer, no network."""

    name = "fake"

    async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
        """Yield one text delta with usage."""
        yield Delta(text="api-answer", usage=Usage(1, 1, 2))


def _client(*, user, org, permissions: list[str]) -> Client:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name=f"harness-{uuid.uuid4().hex[:6]}",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(org.id),
    )


@pytest.fixture
def fake_harness_service(monkeypatch):
    """Route API calls through a service with a fake provider."""
    fake = FakeProvider()
    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: fake,
    )
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    # Keep the runners owner-scoping real (no monkeypatch of _get_service).
    return service


async def _drop_emit(event: str, data: dict) -> None:
    """Drop frontend emits in API tests."""
    return None


READ = [APIKeyPermission.HARNESS_READ.value]
RUN = [APIKeyPermission.HARNESS_RUN.value]
PERMS = [APIKeyPermission.HARNESS_PERMISSIONS.value]


@pytest.mark.django_db(transaction=True)
def test_create_session_returns_busy_without_waiting(harness_setup, monkeypatch):
    """POST sessions returns immediately with busy status (fire-and-forget)."""
    import asyncio as _asyncio

    class SlowProvider(FakeProvider):
        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            await _asyncio.sleep(2)
            yield Delta(text="slow", usage=Usage(1, 1, 2))

    service = HarnessService(
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
        emit=_drop_emit,
        provider_factory=lambda _org: SlowProvider(),
    )
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "agent_name": "build", "mode": "build"}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["status"] == "busy"


@pytest.mark.django_db(transaction=True)
def test_create_session_starts_run(harness_setup, fake_harness_service):
    """POST sessions creates a session and dispatches a background run."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "agent_name": "build", "mode": "build"}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["status"] in ("busy", "idle")
    assert HarnessSession.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_create_session_plan_mode_sets_agent_name(harness_setup, fake_harness_service):
    """POST create with mode=plan (no agent_name) aligns agent_name and returns busy."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "plan it", "mode": "plan"}),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["mode"] == "plan"
    assert body["agent_name"] == "plan"
    assert body["status"] == "busy"
    row = HarnessSession.objects.get(id=body["id"])
    assert row.agent_name == "plan"


@pytest.mark.django_db(transaction=True)
def test_create_session_persists_reasoning_effort(harness_setup, fake_harness_service):
    """POST create stores reasoning_effort on the session."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps(
            {"prompt": "hello", "mode": "build", "reasoning_effort": "high"}
        ),
        content_type="application/json",
    )
    assert response.status_code == 201, response.content[:500]
    body = response.json()
    assert body["reasoning_effort"] == "high"
    row = HarnessSession.objects.get(id=body["id"])
    assert row.reasoning_effort == "high"


@pytest.mark.django_db(transaction=True)
def test_timeline_includes_message_reasoning_effort(
    harness_setup, fake_harness_service
):
    """GET timeline returns the effort snapshotted on the assistant message."""
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + READ,
    )
    created = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps(
            {
                "prompt": "hello",
                "mode": "build",
                "model": "fake-model",
                "reasoning_effort": "high",
            }
        ),
        content_type="application/json",
    )
    assert created.status_code == 201, created.content[:500]
    session_id = created.json()["id"]
    timeline = client.get(f"/api/v1/harness/sessions/{session_id}/timeline")
    assert timeline.status_code == 200, timeline.content[:500]
    messages = timeline.json()["messages"]
    assistant = next(message for message in messages if message["role"] == "assistant")
    assert assistant["model"] == "fake-model"
    assert assistant["reasoning_effort"] == "high"


@pytest.mark.django_db(transaction=True)
def test_create_session_invalid_reasoning_effort_is_400(
    harness_setup, fake_harness_service
):
    """Unknown reasoning_effort is a validation error."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "reasoning_effort": "turbo"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_create_session_invalid_mode_is_400(harness_setup, fake_harness_service):
    """Unknown mode/agent is a validation error, not a 500."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hi", "mode": "turbo"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_foreign_workspace_is_404(harness_setup, fake_harness_service):
    """Owner scoping: another user's workspace reads as not found."""
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['foreign'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hi"}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_missing_key_permission_is_403(harness_setup, fake_harness_service):
    """A key without harness:run cannot create sessions."""
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=[APIKeyPermission.WORKSPACES_READ.value],
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hi"}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_is_401(harness_setup, fake_harness_service):
    """No credentials yields 401 on harness endpoints."""
    client = Client(HTTP_X_ORGANIZATION_ID=str(harness_setup["org"].id))
    response = client.get(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/"
    )
    assert response.status_code == 401


@pytest.mark.django_db(transaction=True)
def test_timeline_todos_abort_flow(harness_setup, fake_harness_service):
    """Timeline, todos and abort endpoints honor read/run permissions."""
    run_client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    created = run_client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "flow"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    session_id = created.json()["id"]

    read_client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    timeline = read_client.get(f"/api/v1/harness/sessions/{session_id}/timeline")
    assert timeline.status_code == 200
    assert "messages" in timeline.json()
    assert timeline.json()["permissions"] == []
    assert timeline.json()["questions"] == []
    removed = read_client.get(f"/api/v1/harness/sessions/{session_id}/parts")
    assert removed.status_code == 404

    todos = read_client.get(f"/api/v1/harness/sessions/{session_id}/todos")
    assert todos.status_code == 200
    assert todos.json() == []

    denied = read_client.post(f"/api/v1/harness/sessions/{session_id}/abort")
    assert denied.status_code == 403

    stranger_client = _client(
        user=harness_setup["stranger"], org=harness_setup["org"], permissions=RUN
    )
    foreign = stranger_client.post(f"/api/v1/harness/sessions/{session_id}/abort")
    assert foreign.status_code == 404

    abort = run_client.post(f"/api/v1/harness/sessions/{session_id}/abort")
    assert abort.status_code == 200


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("harness_id", ["native", "claude", "codex"])
def test_timeline_projects_parts_and_detail_endpoint_is_session_scoped(
    harness_setup, fake_harness_service, harness_id
):
    """Timeline omits detail fields; explicit expansion loads one owned part."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="timeline",
    )
    session.harness_id = harness_id
    session.save(update_fields=["harness_id"])
    assistant = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="answer", harness_id=harness_id
    )
    text_part = HarnessPartRepository.create(
        message_id=assistant.id,
        type="text",
        state="completed",
        output="answer",
    )
    tool = HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="completed",
        call_id="call-1",
        title="Read a.txt",
        input={"tool": "read", "arguments": '{"path":"a.txt"}'},
        output="large tool output",
        meta={"step": 2, "attachments": [{"url": "data:image/png;base64,SECRET"}]},
    )
    patch = HarnessPartRepository.create(
        message_id=assistant.id,
        type="patch",
        state="completed",
        title="Patch src/a.py",
        output="--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new",
        meta={"path": "src/a.py", "old_content": "old copy", "new_content": "new copy"},
    )
    patch.display["_reasoning_line"] = "internal incremental state"
    patch.save(update_fields=["display"])
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    timeline = client.get(f"/api/v1/harness/sessions/{session.id}/timeline")
    assert timeline.status_code == 200
    envelope = timeline.json()
    assert envelope["session"]["harness_id"] == harness_id
    assert {message["harness_id"] for message in envelope["messages"]} == {harness_id}
    assert {"session", "messages", "permissions", "questions"} <= envelope.keys()
    assistant_view = next(
        item for item in envelope["messages"] if item["id"] == str(assistant.id)
    )
    # Timeline clients reconstruct assistant text exclusively from ordered parts.
    assert assistant_view["content"] == ""
    assert (
        next(
            item for item in assistant_view["parts"] if item["id"] == str(text_part.id)
        )["output"]
        == "answer"
    )
    empty_assistant = HarnessMessageRepository.create(
        session_id=session.id,
        role="assistant",
        content="aggregate assistant content must not leak",
        harness_id=harness_id,
    )
    empty_timeline = client.get(
        f"/api/v1/harness/sessions/{session.id}/timeline"
    ).json()
    empty_view = next(
        item
        for item in empty_timeline["messages"]
        if item["id"] == str(empty_assistant.id)
    )
    assert empty_view["content"] == ""
    assert empty_view["parts"] == []
    parts = assistant_view["parts"]
    tool_view = next(item for item in parts if item["id"] == str(tool.id))
    assert tool_view["detail_loaded"] is False
    assert tool_view["tool"] == "read"
    assert tool_view["input"] == {"tool": "read"}
    assert tool_view["output"] == ""
    assert tool_view["meta"] == {}
    patch_view = next(item for item in parts if item["id"] == str(patch.id))
    assert len(patch_view["display"]["preview"]) <= 4
    assert patch_view["display"]["additions"] == 1
    assert patch_view["display"]["deletions"] == 1
    assert "_reasoning_line" not in patch_view["display"]

    # Newly persisted questions retain their compact question/answer preview.
    question_part = HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="completed",
        title="Which color?",
        input={
            "tool": "question",
            "arguments": '{"questions":[{"question":"Which color?"}]}',
        },
        output='{"answers":["blue"]}',
    )
    HarnessPartRepository.mark_state(question_part, "completed")
    timeline = client.get(f"/api/v1/harness/sessions/{session.id}/timeline").json()
    question_view = next(
        item
        for message_item in timeline["messages"]
        for item in message_item["parts"]
        if item["id"] == str(question_part.id)
    )
    assert question_view["display"]["question_rows"] == [
        {
            "header": "",
            "question": "Which color?",
            "options": [],
            "multiple": False,
            "answer": "blue",
        }
    ]

    detail = client.get(f"/api/v1/harness/sessions/{session.id}/parts/{tool.id}")
    assert detail.status_code == 200
    assert detail.json()["detail_loaded"] is True
    assert detail.json()["input"] == {"tool": "read", "arguments": '{"path":"a.txt"}'}
    assert detail.json()["output"] == "large tool output"
    assert "attachments" in detail.json()["meta"]
    patch_detail = client.get(
        f"/api/v1/harness/sessions/{session.id}/parts/{patch.id}"
    ).json()
    assert "_reasoning_line" not in patch_detail["display"]
    assert patch_detail["output"].startswith("--- a/src/a.py")
    assert not {"old_content", "new_content"} & patch_detail["meta"].keys()
    other = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="other session",
    )
    assert (
        client.get(f"/api/v1/harness/sessions/{other.id}/parts/{tool.id}").status_code
        == 404
    )


@pytest.mark.django_db(transaction=True)
def test_timeline_requires_harness_read_permission(harness_setup, fake_harness_service):
    """Both timeline and part detail retain existing read authorization."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="permission",
    )
    no_read = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=[]
    )
    assert (
        no_read.get(f"/api/v1/harness/sessions/{session.id}/timeline").status_code
        == 403
    )
    assert (
        no_read.get(f"/api/v1/harness/sessions/{session.id}/parts/{uuid.uuid4()}").status_code
        == 403
    )


@pytest.mark.django_db(transaction=True)
def test_timeline_includes_pending_permissions_and_questions(
    harness_setup, fake_harness_service
):
    """GET timeline returns pending gates with bounded question definitions."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="need a decision",
    )
    permission = PermissionRequestRepository.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="reboot",
        title="$ reboot",
        call_id="call-perm",
    )
    question = QuestionRequestRepository.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        questions=[{"question": "Which color?", "secret": "x" * 10000}],
        call_id="call-q",
    )
    answered = QuestionRequestRepository.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        questions=[{"question": "Already answered?"}],
    )
    QuestionRequestRepository.resolve(answered, answers=["done"], status="answered")
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    response = client.get(f"/api/v1/harness/sessions/{session.id}/timeline")
    assert response.status_code == 200
    body = response.json()
    assert [item["request_id"] for item in body["permissions"]] == [str(permission.id)]
    assert body["permissions"][0]["tool"] == "bash"
    assert body["permissions"][0]["pattern"] == "reboot"
    assert body["permissions"][0]["status"] == "pending"
    assert [item["request_id"] for item in body["questions"]] == [str(question.id)]
    assert body["questions"][0]["questions"][0]["question"] == "Which color?"
    assert body["questions"][0]["status"] == "pending"
    pending = body["questions"][0]
    assert pending["questions"] == [
        {"header": "", "question": "Which color?", "options": [], "multiple": False}
    ]
    assert len(str(pending)) < 1000


@pytest.mark.django_db(transaction=True)
def test_timeline_omits_heavy_details_and_part_detail_fetches_them(
    harness_setup, fake_harness_service
):
    """Timeline projections omit large fields; authorized detail fetch restores them."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="large timeline details",
    )
    message = HarnessMessageRepository.create(session_id=session.id, role="assistant")
    arguments = "PRIVATE-WRITE-CONTENT-" * 1000
    output = "PRIVATE-TOOL-OUTPUT-" * 1000
    attachment = "A" * 100_000
    tool = HarnessPartRepository.create(
        message_id=message.id,
        type="tool",
        state="completed",
        call_id="large-call",
        title="Write file",
        input={"tool": "write", "arguments": arguments},
        output=output,
        meta={
            "attachments": [
                {
                    "type": "file",
                    "mime": "image/png",
                    "url": f"data:image/png;base64,{attachment}",
                    "filename": "large.png",
                }
            ]
        },
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )

    timeline = client.get(f"/api/v1/harness/sessions/{session.id}/timeline")
    assert timeline.status_code == 200, timeline.content[:500]
    timeline_json = json.dumps(timeline.json())
    for private in (arguments, output, attachment):
        assert private not in timeline_json
    tool_timeline = next(
        part
        for item in timeline.json()["messages"]
        for part in item["parts"]
        if part["id"] == str(tool.id)
    )
    assert tool_timeline["detail_loaded"] is False
    assert tool_timeline["display"]["tool"] == "write"

    detail = client.get(
        f"/api/v1/harness/sessions/{session.id}/parts/{tool.id}"
    )
    assert detail.status_code == 200, detail.content[:500]
    detail_json = detail.json()
    assert detail_json["detail_loaded"] is True
    assert detail_json["input"]["arguments"] == arguments
    assert detail_json["output"] == output
    assert detail_json["meta"]["attachments"][0]["url"].endswith(attachment)


@pytest.mark.django_db(transaction=True)
def test_timeline_includes_descendant_pending_gates(
    harness_setup, fake_harness_service
):
    """GET timeline includes child gates, not sibling-tree gates."""
    parent = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="parent",
    )
    child = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="explore",
        agent_name="explore",
        parent_id=parent.id,
        title="Explore backend",
    )
    other_parent = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="other",
    )
    other_child = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="other child",
        agent_name="explore",
        parent_id=other_parent.id,
    )
    child_perm = PermissionRequestRepository.create(
        organization_id=harness_setup["org"].id,
        session_id=child.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="find /workspace",
        title="$ find /workspace",
    )
    PermissionRequestRepository.create(
        organization_id=harness_setup["org"].id,
        session_id=other_child.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="rm -rf /tmp",
        title="$ rm -rf /tmp",
    )
    child_question = QuestionRequestRepository.create(
        organization_id=harness_setup["org"].id,
        session_id=child.id,
        workspace_id=harness_setup["owned"].id,
        questions=[{"question": "Which file?"}],
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    parent_body = client.get(f"/api/v1/harness/sessions/{parent.id}/timeline").json()
    assert [item["request_id"] for item in parent_body["permissions"]] == [
        str(child_perm.id)
    ]
    assert parent_body["permissions"][0]["session_id"] == str(child.id)
    assert parent_body["permissions"][0]["agent_name"] == "explore"
    assert [item["request_id"] for item in parent_body["questions"]] == [
        str(child_question.id)
    ]
    assert parent_body["questions"][0]["agent_name"] == "explore"

    child_body = client.get(f"/api/v1/harness/sessions/{child.id}/timeline").json()
    assert [item["request_id"] for item in child_body["permissions"]] == [
        str(child_perm.id)
    ]
    other_body = client.get(
        f"/api/v1/harness/sessions/{other_parent.id}/timeline"
    ).json()
    assert [item["session_id"] for item in other_body["permissions"]] == [
        str(other_child.id)
    ]


@pytest.mark.django_db(transaction=True)
def test_double_message_is_409_while_busy(harness_setup, monkeypatch):
    """A second prompt while busy is rejected with 409 (no provider)."""
    service = HarnessService(emit=_drop_emit)
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    session = service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="first",
    )
    # Fake a busy task without running the loop.
    import asyncio as _asyncio

    async def _never() -> None:
        await _asyncio.Event().wait()

    loop = _asyncio.new_event_loop()
    task = loop.create_task(_never())
    service._tasks[str(session.id)] = task
    try:
        client = _client(
            user=harness_setup["owner"],
            org=harness_setup["org"],
            permissions=RUN,
        )
        response = client.post(
            f"/api/v1/harness/sessions/{session.id}/message",
            data=json.dumps({"prompt": "second"}),
            content_type="application/json",
        )
        assert response.status_code == 409
    finally:
        task.cancel()
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_permission_resolve_via_api(harness_setup, fake_harness_service):
    """POST .../permissions/{pid} resolves once via the M3 service."""
    run_client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + PERMS + READ,
    )
    created = run_client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "perm-flow"}),
        content_type="application/json",
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    session = HarnessSession.objects.get(id=session_id)
    request = fake_harness_service.permissions.requests.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="rm -rf /tmp/x",
        title="$ rm -rf /tmp/x",
    )
    response = run_client.post(
        f"/api/v1/harness/sessions/{session_id}/permissions/{request.id}",
        data=json.dumps({"response": "once"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content[:500]
    assert response.json()["decision"] == "allow"


@pytest.mark.django_db(transaction=True)
def test_mode_switch_happy_path(harness_setup, fake_harness_service):
    """PATCH .../mode switches build->plan on an idle session."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="mode switch",
        mode="build",
    )
    assert session.mode == "build"
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}/mode",
        data=json.dumps({"mode": "plan"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content[:500]
    body = response.json()
    assert body["mode"] == "plan"
    assert body["agent_name"] == "plan"
    row = HarnessSession.objects.get(id=session.id)
    assert row.mode == "plan"
    assert row.agent_name == "plan"


@pytest.mark.django_db(transaction=True)
def test_mode_switch_invalid_mode_is_400(harness_setup, fake_harness_service):
    """Unknown mode is a validation error, not a 500."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="mode invalid",
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}/mode",
        data=json.dumps({"mode": "turbo"}),
        content_type="application/json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_mode_switch_while_busy_is_409(harness_setup, fake_harness_service):
    """Mode switch on a session with an active run is rejected with 409."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="mode busy",
    )
    # Fake a busy task without running the loop.
    import asyncio as _asyncio

    async def _never() -> None:
        await _asyncio.Event().wait()

    loop = _asyncio.new_event_loop()
    task = loop.create_task(_never())
    fake_harness_service._tasks[str(session.id)] = task
    try:
        client = _client(
            user=harness_setup["owner"],
            org=harness_setup["org"],
            permissions=RUN,
        )
        response = client.patch(
            f"/api/v1/harness/sessions/{session.id}/mode",
            data=json.dumps({"mode": "plan"}),
            content_type="application/json",
        )
        assert response.status_code == 409
    finally:
        task.cancel()
        loop.close()


@pytest.mark.django_db(transaction=True)
def test_mode_switch_foreign_workspace_is_404(harness_setup, fake_harness_service):
    """Owner scoping: mode switch on another user's session reads as not found."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["foreign"].id,
        organization_id=harness_setup["org"].id,
        prompt="mode foreign",
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}/mode",
        data=json.dumps({"mode": "plan"}),
        content_type="application/json",
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_mode_switch_needs_run_permission(harness_setup, fake_harness_service):
    """A read-only key cannot switch session mode (403)."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="mode perm",
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    response = client.patch(
        f"/api/v1/harness/sessions/{session.id}/mode",
        data=json.dumps({"mode": "plan"}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


@pytest.mark.django_db(transaction=True)
def test_missing_provider_config_is_4xx(harness_setup, monkeypatch):
    """Creating a session without provider/model config returns 404, not a hang."""
    service = HarnessService(emit=_drop_emit)
    monkeypatch.setattr(harness_api, "_resolve_harness_service", lambda: service)
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "no provider"}),
        content_type="application/json",
    )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.django_db(transaction=True)
def test_followup_message_with_mode_plan(harness_setup, fake_harness_service):
    """Follow-up POST with mode=plan aligns agent_name before the run."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="initial",
        mode="build",
        agent_name="build",
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/message",
        data=json.dumps({"prompt": "plan this", "mode": "plan"}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content[:500]
    body = response.json()
    assert body["mode"] == "plan"
    assert body["agent_name"] == "plan"
    assert body["status"] == "busy"


@pytest.mark.django_db(transaction=True)
def test_followup_message_to_subagent_is_400(harness_setup, fake_harness_service):
    """Users cannot send follow-up prompts to a subagent child session."""
    parent = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="parent",
    )
    child = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="child",
        parent_id=parent.id,
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{child.id}/message",
        data=json.dumps({"prompt": "hello subagent"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert "subagent" in body["detail"].lower()


@pytest.mark.django_db(transaction=True)
def test_permission_resolve_needs_permission_key(harness_setup, fake_harness_service):
    """Resolving without harness:permissions is 403."""
    run_client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    created = run_client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "perm-403"}),
        content_type="application/json",
    )
    session_id = created.json()["id"]
    session = HarnessSession.objects.get(id=session_id)
    request = fake_harness_service.permissions.requests.create(
        organization_id=harness_setup["org"].id,
        session_id=session.id,
        workspace_id=harness_setup["owned"].id,
        tool="bash",
        pattern="ls",
    )
    response = run_client.post(
        f"/api/v1/harness/sessions/{session_id}/permissions/{request.id}",
        data=json.dumps({"response": "once"}),
        content_type="application/json",
    )
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_production_harness_service_wires_runner_accessor():
    """REST uses the harness singleton whose accessor talks to the runner."""
    from apps.harness.harness_service import (
        get_harness_service,
        reset_default_harness_service,
    )

    reset_default_harness_service()
    try:
        service = harness_api._resolve_harness_service()
        assert callable(service._accessor_factory)
        assert get_harness_service() is service
    finally:
        reset_default_harness_service()


@pytest.mark.django_db(transaction=True)
def test_notice_dismiss_persists_and_shows_in_timeline(
    harness_setup, fake_harness_service
):
    """POST notice-dismiss stores the timestamp; GET timeline exposes it."""
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + READ,
    )
    created = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "mode": "build", "model": "fake-model"}),
        content_type="application/json",
    )
    assert created.status_code == 201, created.content[:500]
    session_id = created.json()["id"]
    session = HarnessSession.objects.get(id=session_id)
    message = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="", model="fake-model"
    )
    HarnessMessageRepository.complete(
        message, finish="aborted", error="aborted by user"
    )

    dismissed = client.post(
        f"/api/v1/harness/sessions/{session_id}/messages/{message.id}/notice-dismiss"
    )
    assert dismissed.status_code == 204, dismissed.content[:500]

    timeline = client.get(f"/api/v1/harness/sessions/{session_id}/timeline")
    assert timeline.status_code == 200, timeline.content[:500]
    dismissed_row = next(
        row for row in timeline.json()["messages"] if row["id"] == str(message.id)
    )
    assert dismissed_row["notice_dismissed_at"] is not None


@pytest.mark.django_db(transaction=True)
def test_notice_dismiss_rejects_foreign_message(harness_setup, fake_harness_service):
    """Dismissing a message of another session yields 404."""
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + READ,
    )
    first = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "mode": "build", "model": "fake-model"}),
        content_type="application/json",
    )
    second = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "world", "mode": "build", "model": "fake-model"}),
        content_type="application/json",
    )
    other_session = HarnessSession.objects.get(id=second.json()["id"])
    other_message = HarnessMessageRepository.create(
        session_id=other_session.id, role="assistant", content=""
    )
    response = client.post(
        f"/api/v1/harness/sessions/{first.json()['id']}/messages/{other_message.id}/notice-dismiss"
    )
    assert response.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_start_run_dismisses_prior_notices(harness_setup, fake_harness_service):
    """A follow-up send auto-clears older stopped/failed notices."""
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + READ,
    )
    created = client.post(
        f"/api/v1/workspaces/{harness_setup['owned'].id}/harness/sessions/",
        data=json.dumps({"prompt": "hello", "mode": "build", "model": "fake-model"}),
        content_type="application/json",
    )
    session_id = created.json()["id"]
    session = HarnessSession.objects.get(id=session_id)
    old = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content=""
    )
    HarnessMessageRepository.complete(old, finish="error", error="boom")
    assert old.notice_dismissed_at is None

    sent = client.post(
        f"/api/v1/harness/sessions/{session_id}/message",
        data=json.dumps({"prompt": "again", "model": "fake-model"}),
        content_type="application/json",
    )
    assert sent.status_code == 202, sent.content[:500]
    old.refresh_from_db()
    assert old.notice_dismissed_at is not None


@pytest.fixture
def active_run(fake_harness_service):
    """Pretend a run is active for a session so steers can be queued."""
    import asyncio as _asyncio

    from apps.harness.steering import SteeringChannel

    loop = _asyncio.new_event_loop()
    tasks = []

    def _start(session):  # type: ignore[no-untyped-def]
        async def _never() -> None:
            await _asyncio.Event().wait()

        key = str(session.id)
        task = loop.create_task(_never())
        loop.run_until_complete(_asyncio.sleep(0))
        tasks.append(task)

        async def _deliver(_entries):  # type: ignore[no-untyped-def]
            return None

        channel = SteeringChannel(deliver=_deliver)
        fake_harness_service._tasks[key] = task
        fake_harness_service._runs[key] = {"steering": channel}
        return channel

    yield _start
    for task in tasks:
        task.cancel()
    loop.close()


@pytest.mark.django_db(transaction=True)
def test_steer_queues_message_for_active_run(
    harness_setup, fake_harness_service, active_run
):
    """POST .../steer queues, lists in the timeline, and can be withdrawn."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="seed",
    )
    channel = active_run(session)
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + READ,
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/steer",
        data=json.dumps({"prompt": "  also add tests  "}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content[:500]
    body = response.json()
    assert body["content"] == "also add tests"
    assert [entry.id for entry in channel.pending()] == [body["id"]]

    listed = client.get(f"/api/v1/harness/sessions/{session.id}/timeline")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["steers"]] == [body["id"]]

    steer_url = f"/api/v1/harness/sessions/{session.id}/steer/{body['id']}"
    withdrawn = client.delete(steer_url)
    assert withdrawn.status_code == 200
    assert withdrawn.json()["content"] == "also add tests"
    assert channel.pending() == []
    assert client.delete(steer_url).status_code == 404


@pytest.mark.django_db(transaction=True)
def test_steer_without_active_run_is_409(harness_setup, fake_harness_service):
    """An idle session cannot be steered; clients fall back to /message."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="seed",
    )
    client = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )
    response = client.post(
        f"/api/v1/harness/sessions/{session.id}/steer",
        data=json.dumps({"prompt": "hello?"}),
        content_type="application/json",
    )
    assert response.status_code == 409
    assert response.json()["code"] == "no_active_run"


@pytest.mark.django_db(transaction=True)
def test_steer_rejects_empty_subagent_foreign_and_read_only_keys(
    harness_setup, fake_harness_service, active_run
):
    """Validation, scope and key permission are enforced like /message."""
    session = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="seed",
    )
    child = fake_harness_service.create_session(
        workspace_id=harness_setup["owned"].id,
        organization_id=harness_setup["org"].id,
        prompt="child",
        parent_id=session.id,
    )
    active_run(session)
    owner = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=RUN
    )

    def steer(client, target, prompt="hi"):  # type: ignore[no-untyped-def]
        return client.post(
            f"/api/v1/harness/sessions/{target.id}/steer",
            data=json.dumps({"prompt": prompt}),
            content_type="application/json",
        )

    assert steer(owner, session, "   ").status_code == 400
    assert steer(owner, child).status_code == 400
    stranger = _client(
        user=harness_setup["stranger"], org=harness_setup["org"], permissions=RUN
    )
    assert steer(stranger, session).status_code == 404
    read_only = _client(
        user=harness_setup["owner"], org=harness_setup["org"], permissions=READ
    )
    assert steer(read_only, session).status_code == 403


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("missing_owner", [False, True])
@pytest.mark.parametrize(
    ("method", "suffix", "payload", "code"),
    [
        ("post", "message", {"prompt": "hello"}, "permission_denied"),
        ("post", "steer", {"prompt": "hello"}, "authentication_error"),
        ("delete", "steer/{id}", None, "authentication_error"),
        ("post", "messages/{id}/edit", {"prompt": "edit"}, "permission_denied"),
        ("post", "permissions/{id}", {"response": "once"}, "permission_denied"),
        ("post", "questions/{id}", {"answers": [["yes"]]}, "permission_denied"),
    ],
)
def test_external_session_actions_preserve_owner_denial_contract(
    harness_setup,
    fake_harness_service,
    engine: str,
    missing_owner: bool,
    method: str,
    suffix: str,
    payload: dict | None,
    code: str,
) -> None:
    """Workspace access must not grant access to a session's personal account."""
    session = HarnessSession.objects.create(
        workspace=harness_setup["owned"],
        organization_id=harness_setup["org"].id,
        harness_id=engine,
        created_by=None if missing_owner else harness_setup["stranger"],
    )
    client = _client(
        user=harness_setup["owner"],
        org=harness_setup["org"],
        permissions=RUN + PERMS,
    )
    route = suffix.format(id=uuid.uuid4())
    response = getattr(client, method)(
        f"/api/v1/harness/sessions/{session.id}/{route}",
        data=json.dumps(payload) if payload is not None else None,
        content_type="application/json",
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Only the session owner may use its external engine connection",
        "code": code,
    }
    assert HarnessMessageRepository.list_for_session(session.id) == []


@pytest.mark.django_db
@pytest.mark.parametrize("endpoint", ["sessions", "part", "todos"])
@pytest.mark.parametrize("access", ["owner", "foreign", "missing", "no_read"])
def test_saved_history_endpoints_work_without_live_harness(
    harness_setup, monkeypatch, endpoint: str, access: str
) -> None:
    """Saved reads keep permission/scope failures without constructing execution."""
    session = HarnessSession.objects.create(
        workspace=harness_setup["owned"], organization_id=harness_setup["org"].id
    )
    message = HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content="Saved answer"
    )
    part = HarnessPartRepository.create(
        message_id=message.id, type="tool", output="Saved detail"
    )
    todo = Todo.objects.create(session=session, content="Saved todo")

    def unexpected_execution() -> None:
        pytest.fail("Saved history must not resolve live run orchestration")

    monkeypatch.setattr(harness_api, "_resolve_harness_service", unexpected_execution)
    client = _client(
        user=harness_setup["stranger" if access == "foreign" else "owner"],
        org=harness_setup["org"],
        permissions=[] if access == "no_read" else READ,
    )
    session_id = uuid.uuid4() if access == "missing" else session.id
    workspace_id = uuid.uuid4() if access == "missing" else session.workspace_id
    paths = {
        "sessions": f"/api/v1/workspaces/{workspace_id}/harness/sessions/",
        "part": f"/api/v1/harness/sessions/{session_id}/parts/{part.id}",
        "todos": f"/api/v1/harness/sessions/{session_id}/todos",
    }
    response = client.get(paths[endpoint])
    assert response.status_code == {
        "owner": 200, "foreign": 404, "missing": 404, "no_read": 403
    }[access]
    if access == "owner":
        body = response.json()
        if endpoint == "part":
            assert body["id"] == str(part.id)
            assert body["output"] == "Saved detail"
        else:
            expected_id = session.id if endpoint == "sessions" else todo.id
            assert [row["id"] for row in body] == [str(expected_id)]
