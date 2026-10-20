"""REST/MCP regressions for restoring captured-image process snapshots."""

from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.mcp_app.server import (
    _call_create_workspace,
    _call_create_workspace_from_captured_image,
    create_mcp_server,
)
from apps.organizations.models import Membership, MembershipRole
from apps.runners.capture_repository import CaptureRepository
from apps.runners.enums import ProcessStatus, TaskType, WorkspaceStatus
from apps.runners.models import (
    CaptureRequest,
    ImageInstance,
    Task,
    Workspace,
    WorkspaceProcess,
)
from apps.runners.repositories import TaskRepository
from apps.runners.services import get_runner_service
from common.utils import generate_api_token, hash_token

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def member(user, organization):
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


@pytest.fixture
def api_key_factory():
    """Create real API keys and REST headers with an explicit permission set."""

    def create(user, organization, permissions):
        token = generate_api_token()
        key = APIKey.objects.create(
            user=user,
            name="process-interface-test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=[permission.value for permission in permissions],
        )
        headers = {
            "HTTP_X_API_KEY": token,
            "HTTP_X_ORGANIZATION_ID": str(organization.id),
        }
        return key, headers

    return create


def _capture_generation(source, *, name, captured_image_id=None):
    _, task = CaptureRepository.allocate(
        source.id, name=name, captured_image_id=captured_image_id
    )
    image = ImageInstance.objects.get(
        origin_workspace=source, generation=(1 if captured_image_id is None else 2)
    )
    TaskRepository.complete(task)
    image.status = ImageInstance.Status.READY
    image.runner_ref = f"/images/interface-generation-{image.generation}.qcow2"
    image.save(update_fields=["status", "runner_ref"])
    CaptureRequest.objects.filter(image=image).delete()
    # A completed capture may retain its projected operation until its parent
    # finishes. Clear that source-workspace state before capturing its next version.
    Workspace.objects.filter(pk=source.pk).update(
        current_task=None, active_operation=None
    )
    return image


def _captured_line_with_process_generations(runner, user):
    """Capture two immutable generations, including one deliberately temp row."""
    source = Workspace.objects.create(
        runner=runner,
        created_by=user,
        name="Imported process source",
        runtime_type="qemu",
        status=WorkspaceStatus.STOPPED,
    )
    api = WorkspaceProcess.objects.create(
        workspace=source,
        created_by=user,
        name="api",
        command="python server.py",
        workdir="/srv/api",
        status=ProcessStatus.RUNNING,
        pid=4312,
        log_path=".opencuria/processes/api.log",
        run_count=8,
        exit_code=7,
        session_id=uuid.uuid4(),
    )
    worker = WorkspaceProcess.objects.create(
        workspace=source,
        created_by=user,
        name="worker",
        command="rq worker old",
        workdir="/srv/worker",
        status=ProcessStatus.EXITED,
        pid=4313,
        log_path="worker-old.log",
        run_count=3,
        exit_code=0,
    )
    WorkspaceProcess.objects.create(
        workspace=source,
        created_by=user,
        name="temporary helper",
        command="python helper.py",
        workdir="/tmp",
        kind="temp",
        session_id=uuid.uuid4(),
        pid=4314,
        log_path="temporary.log",
    )

    first = _capture_generation(source, name="Imported processes")
    line = first.captured_image
    assert first.process_definitions == [
        {"name": "api", "command": "python server.py", "workdir": "/srv/api"},
        {"name": "worker", "command": "rq worker old", "workdir": "/srv/worker"},
    ]

    api.command = "python server.py --generation-two"
    api.workdir = "/srv/api-v2"
    api.save(update_fields=["command", "workdir"])
    worker.delete()
    WorkspaceProcess.objects.create(
        workspace=source,
        created_by=user,
        name="scheduler",
        command="celery beat",
        workdir="/srv/scheduler",
        status=ProcessStatus.FAILED,
        exit_code=1,
    )
    second = _capture_generation(source, name="", captured_image_id=line.id)
    assert second.process_definitions == [
        {
            "name": "api",
            "command": "python server.py --generation-two",
            "workdir": "/srv/api-v2",
        },
        {"name": "scheduler", "command": "celery beat", "workdir": "/srv/scheduler"},
    ]
    return line, first, second


def _mcp_clone(api_key, organization, line):
    """Call the registered MCP operation directly, without its network transport."""
    result = _call_create_workspace_from_captured_image(
        api_key,
        organization.id,
        {"captured_image_id": str(line.id), "name": "Imported process clone"},
    )
    assert len(result) == 1
    if result[0].text.startswith("Error:"):
        return result[0].text
    return json.loads(result[0].text)


def _mcp_general_create(api_key, organization, image):
    """Call create_workspace with an explicit base image (no MCP transport)."""
    result = _call_create_workspace(
        api_key,
        organization.id,
        {
            "name": "General endpoint foreign image",
            "image_artifact_id": str(image.id),
        },
    )
    assert len(result) == 1
    return result[0].text


def _mcp_tool_call(api_key, arguments):
    """Exercise MCP's registered tool dispatch and API-key permission check."""
    from mcp import types

    server = create_mcp_server(api_key)
    request = types.CallToolRequest(
        params=types.CallToolRequestParams(
            name="create_workspace_from_captured_image", arguments=arguments
        )
    )
    result = asyncio.run(server.request_handlers[types.CallToolRequest](request))
    return result.root


def _clone(interface, api_key, headers, organization, line):
    if interface == "rest":
        response = Client().post(
            f"/api/v1/captured-images/{line.id}/workspaces/",
            data=json.dumps({"name": "Imported process clone"}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 202, response.content
        return response.json()

    return _mcp_clone(api_key, organization, line)


@pytest.fixture
def runner_service_with_mocked_transport(monkeypatch):
    """Use the real RunnerService path but replace only runner transport emit."""
    service = get_runner_service()
    emit = AsyncMock()
    monkeypatch.setattr(service, "_emit_to_runner", emit)
    return service, emit


@pytest.mark.parametrize("interface", ["rest", "mcp"])
def test_clone_restores_latest_captured_process_generation_without_runtime_state(
    interface,
    member,
    runner,
    organization,
    api_key_factory,
    runner_service_with_mocked_transport,
):
    api_key, headers = api_key_factory(
        member, organization, [APIKeyPermission.IMAGES_CLONE]
    )
    line, first, latest = _captured_line_with_process_generations(runner, member)

    payload = _clone(interface, api_key, headers, organization, line)
    workspace = Workspace.objects.get(pk=payload["workspace_id"])
    task = Task.objects.get(pk=payload["task_id"])
    assert task.type == TaskType.CREATE_WORKSPACE_FROM_IMAGE_ARTIFACT
    assert workspace.base_image_instance_id == latest.id
    assert workspace.base_image_instance_id != first.id
    assert workspace.created_by_id == member.id
    assert workspace.credentials_present is False
    assert not workspace.credentials.exists()

    restored = list(
        WorkspaceProcess.objects.filter(workspace=workspace).order_by("name")
    )
    assert {
        process.name: (process.command, process.workdir) for process in restored
    } == {
        "api": ("python server.py --generation-two", "/srv/api-v2"),
        "scheduler": ("celery beat", "/srv/scheduler"),
    }
    assert all(process.kind == "persistent" for process in restored)
    for process in restored:
        assert process.created_by_id == member.id
        assert process.status == ProcessStatus.EXITED
        assert process.run_count == 0
        assert process.pid is None
        assert process.log_path == ""
        assert process.exit_code is None
        assert process.session_id is None
        assert process.ended_at is None

    # Process definitions are database state only: the runner receives the
    # workspace-create task, not temporary environment or process-start RPCs.
    _, emit = runner_service_with_mocked_transport
    emitted = [call.args[1] for call in emit.await_args_list]
    assert emitted == ["task:create_workspace_from_image_artifact"]
    assert not any("process_start" in event for event in emitted)
    dispatch_payload = emit.await_args.args[2]
    assert dispatch_payload["env_vars"] == {}
    assert dispatch_payload["files"] == []
    assert dispatch_payload["ssh_keys"] == []


@pytest.mark.parametrize("interface", ["rest", "mcp"])
def test_clone_requires_images_clone_permission_without_creating_workspace(
    interface, member, runner, organization, api_key_factory
):
    line, _, _ = _captured_line_with_process_generations(runner, member)
    api_key, headers = api_key_factory(member, organization, [])
    before = (
        Workspace.objects.count(),
        Task.objects.count(),
        WorkspaceProcess.objects.count(),
    )

    if interface == "rest":
        response = Client().post(
            f"/api/v1/captured-images/{line.id}/workspaces/",
            data=json.dumps({"name": "Unauthorized clone"}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 403, response.content
    else:
        result = _mcp_tool_call(api_key, {"captured_image_id": str(line.id)})
        assert result.content[0].text == (
            "Error: Permission denied: images:clone required"
        )
    assert (
        Workspace.objects.count(),
        Task.objects.count(),
        WorkspaceProcess.objects.count(),
    ) == before


@pytest.mark.parametrize("interface", ["rest", "mcp"])
def test_general_workspace_create_rejects_foreign_captured_image_without_dispatch(
    interface,
    member,
    runner,
    organization,
    api_key_factory,
    runner_service_with_mocked_transport,
):
    """General create must not import another owner's captured process snapshot."""
    image_owner = member
    foreign = get_user_model().objects.create_user(
        email="general-create-foreign@example.com", password="secret"
    )
    Membership.objects.create(
        user=foreign, organization=organization, role=MembershipRole.MEMBER
    )
    api_key, headers = api_key_factory(
        foreign, organization, [APIKeyPermission.WORKSPACES_CREATE]
    )
    line, _, latest = _captured_line_with_process_generations(runner, image_owner)
    assert latest.captured_image_id == line.id

    # Deliberately omit credential_ids: this operation requests no credentials
    # and must not require CREDENTIALS_READ in addition to WORKSPACES_CREATE.
    before = (
        Workspace.objects.count(),
        Task.objects.count(),
        WorkspaceProcess.objects.count(),
    )
    _, emit = runner_service_with_mocked_transport

    if interface == "rest":
        response = Client().post(
            "/api/v1/workspaces/",
            data=json.dumps(
                {
                    "name": "Foreign image import",
                    "image_artifact_id": str(latest.id),
                }
            ),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 404, response.content
    else:
        from apps.mcp_app.server import _TOOL_PERMISSIONS

        assert (
            _TOOL_PERMISSIONS["create_workspace"] == APIKeyPermission.WORKSPACES_CREATE
        )
        # MCP exposes the generic create tool to this correctly-scoped key;
        # invoke its operation handler without the MCP network transport.
        from mcp import types

        server = create_mcp_server(api_key)
        listed_tools = asyncio.run(
            server.request_handlers[types.ListToolsRequest](None)
        )
        assert "create_workspace" in {tool.name for tool in listed_tools.root.tools}
        result = _call_create_workspace(
            api_key,
            organization.id,
            {
                "name": "Foreign image import",
                "image_artifact_id": str(latest.id),
            },
        )
        assert len(result) == 1
        payload = json.loads(result[0].text)
        assert "error" in payload
        assert "Image artifact" in payload["error"]

    assert (
        Workspace.objects.count(),
        Task.objects.count(),
        WorkspaceProcess.objects.count(),
    ) == before
    assert not WorkspaceProcess.objects.filter(workspace__created_by=foreign).exists()
    emit.assert_not_awaited()


@pytest.mark.parametrize("interface", ["rest", "mcp"])
def test_clone_does_not_disclose_or_clone_foreign_users_captured_image(
    interface, member, runner, organization, api_key_factory
):
    line, _, _ = _captured_line_with_process_generations(runner, member)
    foreign = get_user_model().objects.create_user(
        email="foreign-image-owner@example.com", password="secret"
    )
    Membership.objects.create(
        user=foreign, organization=organization, role=MembershipRole.MEMBER
    )
    api_key, headers = api_key_factory(
        foreign, organization, [APIKeyPermission.IMAGES_CLONE]
    )
    before = (
        Workspace.objects.count(),
        Task.objects.count(),
        WorkspaceProcess.objects.count(),
    )

    if interface == "rest":
        response = Client().post(
            f"/api/v1/captured-images/{line.id}/workspaces/",
            data=json.dumps({"name": "Foreign clone"}),
            content_type="application/json",
            **headers,
        )
        assert response.status_code == 404
    else:
        result = _mcp_clone(api_key, organization, line)
        assert isinstance(result, str)
        assert result.startswith("Error: Captured image ")

    assert (
        Workspace.objects.count(),
        Task.objects.count(),
        WorkspaceProcess.objects.count(),
    ) == before
