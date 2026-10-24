"""Git MCP adapters exercise the real shared RunnerService validation."""

from __future__ import annotations

import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest

from apps.accounts.models import APIKeyPermission
from apps.mcp_app.server import create_mcp_server
from apps.mcp_app.tools import TOOL_HANDLERS, TOOL_PERMISSIONS, TOOLS
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.exceptions import RunnerTimeoutError
from apps.runners.models import Runner, Workspace
from apps.runners.services import RunnerService
from common.exceptions import ConflictError
from common.utils import hash_token


def _key(user=None, *permissions: APIKeyPermission) -> SimpleNamespace:
    return SimpleNamespace(
        user=user, has_permission=lambda permission: permission in permissions
    )


@pytest.fixture
def git_setup(db, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Use real ownership/validation and mock only the final runner RPC."""
    org = Organization.objects.create(name="Git MCP", slug="git-mcp")
    owner, stranger = [
        get_user_model().objects.create_user(email=email, password="secret")
        for email in ("owner@example.com", "stranger@example.com")
    ]
    for user in (owner, stranger):
        Membership.objects.create(
            user=user, organization=org, role=MembershipRole.MEMBER
        )
    runner = Runner.objects.create(
        name="git-mcp-runner",
        api_token_hash=hash_token("git-mcp-token"),
        status=RunnerStatus.ONLINE,
        sid="git-mcp-sid",
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner, name="Git MCP", status=WorkspaceStatus.RUNNING, created_by=owner
    )
    service = RunnerService()
    service._await_git_result = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr("apps.mcp_app.access.runner_service", lambda: service)
    return SimpleNamespace(
        org=org,
        owner=owner,
        stranger=stranger,
        workspace=workspace,
        service=service,
        key=_key(
            owner,
            APIKeyPermission.WORKSPACES_GIT_READ,
            APIKeyPermission.WORKSPACES_GIT_WRITE,
        ),
    )


async def _call(api_key, name: str, arguments: dict) -> list:
    """Invoke the actual MCP dispatcher, including permissions and JSON Schema."""
    handler = create_mcp_server(api_key).request_handlers[CallToolRequest]
    result = await handler(
        CallToolRequest(
            method="tools/call",
            params=CallToolRequestParams(name=name, arguments=arguments),
        )
    )
    return result.root.content


def test_git_tool_contract() -> None:
    """Keep five async tools, strict schemas and the shared mutation catalog."""
    expected = {
        "get_git_state": APIKeyPermission.WORKSPACES_GIT_READ,
        "get_git_diff": APIKeyPermission.WORKSPACES_GIT_READ,
        "get_git_commit": APIKeyPermission.WORKSPACES_GIT_READ,
        "generate_git_commit_message": APIKeyPermission.WORKSPACES_GIT_READ,
        "git_operation": APIKeyPermission.WORKSPACES_GIT_WRITE,
    }
    assert {tool.name for tool in TOOLS if "git" in tool.name} == set(expected)
    for tool in TOOLS:
        if tool.name in expected:
            assert TOOL_PERMISSIONS[tool.name] == expected[tool.name]
            assert inspect.iscoroutinefunction(TOOL_HANDLERS[tool.name])
            assert tool.inputSchema["additionalProperties"] is False
    generic = next(tool for tool in TOOLS if tool.name == "git_operation")
    assert set(generic.inputSchema["properties"]["operation"]["enum"]) == (
        set(RunnerService.GIT_OPERATIONS) - RunnerService.GIT_READ_OPERATIONS
    )


@pytest.mark.parametrize(
    "permission,visible,denied,arguments",
    [
        (
            APIKeyPermission.WORKSPACES_GIT_READ,
            {
                "get_git_state",
                "get_git_diff",
                "get_git_commit",
                "generate_git_commit_message",
            },
            "git_operation",
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a"]},
        ),
        (
            APIKeyPermission.WORKSPACES_GIT_WRITE,
            {"git_operation"},
            "get_git_state",
            {},
        ),
    ],
)
async def test_git_permissions(permission, visible, denied, arguments) -> None:
    """Advertising and actual calls both enforce read/write separation."""
    key = _key(None, permission)
    handler = create_mcp_server(key).request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))
    assert {tool.name for tool in result.root.tools if "git" in tool.name} == visible
    content = await _call(key, denied, {"workspace_id": str(uuid.uuid4()), **arguments})
    assert "Permission denied" in content[0].text


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "tool,arguments,operation,service_args",
    [
        ("get_git_state", {}, "list_repos", {}),
        ("get_git_diff", {"repo_path": "/workspace/repo"}, "working_diff", {}),
        (
            "get_git_commit",
            {"repo_path": "/workspace/repo", "commit": "ABC123"},
            "commit_details",
            {"commit": "abc123"},
        ),
        (
            "git_operation",
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a.txt"]},
            "stage",
            {"paths": ["a.txt"]},
        ),
        (
            "git_operation",
            {"operation": "commit", "repo_path": "/workspace/repo", "message": "hello"},
            "commit",
            {"message": "hello"},
        ),
    ],
)
async def test_git_dispatch(
    git_setup, tool, arguments, operation, service_args
) -> None:
    """MCP uses real service normalization and server-derived commit identity."""
    setup = git_setup
    result = await _call(
        setup.key, tool, {"workspace_id": str(setup.workspace.id), **arguments}
    )
    assert json.loads(result[0].text) == {"ok": True}
    payload = setup.service._await_git_result.call_args.kwargs["payload"]
    assert payload["workspace_id"] == str(setup.workspace.id)
    assert payload["operation"] == operation
    assert payload.get("repo_path") == arguments.get("repo_path")
    if operation == "commit":
        name, email = RunnerService.commit_identity_for_user(setup.owner)
        service_args = {**service_args, "author_name": name, "author_email": email}
    assert payload["args"] == service_args


@pytest.mark.django_db(transaction=True)
async def test_git_ownership_and_mutation_guards(git_setup) -> None:
    """Foreign workspaces, read-only mutations and generic reads never dispatch."""
    setup = git_setup
    args = {"workspace_id": str(setup.workspace.id)}
    result = await _call(
        _key(setup.stranger, APIKeyPermission.WORKSPACES_GIT_READ),
        "get_git_state",
        args,
    )
    assert result[0].text == "Error: Workspace not found"
    result = await TOOL_HANDLERS["git_operation"](
        _key(setup.owner, APIKeyPermission.WORKSPACES_GIT_READ),
        setup.org.id,
        {**args, "operation": "stage", "repo_path": "/workspace/repo", "paths": ["a"]},
    )
    assert "Permission denied" in result[0].text
    for operation in RunnerService.GIT_READ_OPERATIONS:
        result = await TOOL_HANDLERS["git_operation"](
            setup.key, setup.org.id, {**args, "operation": operation}
        )
        assert "get_git_state/get_git_diff/get_git_commit" in result[0].text
    setup.service._await_git_result.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_git_service_validation_cannot_be_bypassed(git_setup) -> None:
    """Even direct handler calls forward invalid/unknown args to the service."""
    setup = git_setup
    workspace = {"workspace_id": str(setup.workspace.id)}
    repo = {**workspace, "repo_path": "/workspace/repo"}
    commit = {**repo, "operation": "commit", "message": "hello"}
    cases = [
        ("get_git_state", {}, "workspace_id is required"),
        ("get_git_state", {"workspace_id": "invalid"}, "Invalid workspace_id UUID"),
        ("get_git_diff", workspace, "repo_path is required"),
        ("get_git_diff", {**workspace, "repo_path": "/etc/passwd"}, "under /workspace"),
        ("get_git_commit", repo, "commit is required"),
        ("get_git_commit", {**repo, "commit": "xyz!"}, "Invalid commit hash"),
        ("get_git_state", {**workspace, "env": {}}, "Unknown argument"),
        ("get_git_diff", {**repo, "paths": ["a"]}, "Unknown argument"),
        ("get_git_commit", {**repo, "operation": "push"}, "Unknown argument"),
        ("git_operation", workspace, "operation is required"),
        ("git_operation", {**repo, "operation": "unknown"}, "Unknown git operation"),
        ("git_operation", {**repo, "operation": "stage"}, "paths is required"),
    ]
    cases.extend(
        ("git_operation", {**commit, field: value}, "Unknown argument")
        for field, value in {
            "env": {"GIT_DIR": "/tmp"},
            "author_name": "Mallory",
            "author_email": "m@x.y",
            "args": {},
            "argv": ["git"],
            "message_b64": "aGVsbG8=",
            "hash": "abc123",
            "unknown_field": "x",
        }.items()
    )
    for tool, arguments, error in cases:
        result = await TOOL_HANDLERS[tool](setup.key, setup.org.id, arguments)
        assert error in result[0].text, (tool, arguments, result)
    setup.service._await_git_result.assert_not_called()


@pytest.mark.django_db(transaction=True)
async def test_git_runner_failures(git_setup) -> None:
    """Runner domain errors remain JSON; transport exceptions remain MCP errors."""
    setup = git_setup
    args = {"workspace_id": str(setup.workspace.id)}
    failure = {
        "ok": False,
        "code": "conflict",
        "message": "merge conflict",
        "stderr": "CONFLICT",
    }
    setup.service._await_git_result.return_value = failure
    result = await _call(setup.key, "get_git_state", args)
    assert json.loads(result[0].text) == failure
    setup.service._await_git_result.side_effect = RunnerTimeoutError("list_repos", 75)
    result = await _call(setup.key, "get_git_state", args)
    assert result[0].text.startswith("Error:")
    assert "did not respond" in result[0].text


@pytest.fixture
def commit_message_fake(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Replace the shared HarnessService commit-message generator."""
    generate = AsyncMock(return_value="feat: add login")
    monkeypatch.setattr(
        "apps.mcp_app.access.harness_service",
        lambda: SimpleNamespace(generate_commit_message=generate),
    )
    return generate


@pytest.mark.django_db(transaction=True)
async def test_generate_git_commit_message(git_setup, commit_message_fake) -> None:
    """The MCP tool mirrors REST: owned workspace, org and API-key user scope."""
    setup = git_setup
    key = _key(
        setup.owner, APIKeyPermission.WORKSPACES_GIT_READ, APIKeyPermission.HARNESS_RUN
    )
    result = await _call(
        key,
        "generate_git_commit_message",
        {"workspace_id": str(setup.workspace.id), "repo_path": "/workspace/repo"},
    )

    assert json.loads(result[0].text) == {"message": "feat: add login"}
    commit_message_fake.assert_awaited_once_with(
        workspace_id=setup.workspace.id,
        repo_path="/workspace/repo",
        organization_id=setup.org.id,
        user_id=setup.owner.id,
    )


@pytest.mark.django_db(transaction=True)
async def test_generate_git_commit_message_guards(
    git_setup, commit_message_fake
) -> None:
    """harness:run, strict args and workspace ownership are enforced."""
    setup = git_setup
    args = {"workspace_id": str(setup.workspace.id), "repo_path": "/workspace/repo"}
    full = (APIKeyPermission.WORKSPACES_GIT_READ, APIKeyPermission.HARNESS_RUN)
    handler = TOOL_HANDLERS["generate_git_commit_message"]
    cases = [
        (_key(setup.owner, APIKeyPermission.WORKSPACES_GIT_READ), args, "harness:run"),
        (_key(setup.owner, *full), {**args, "paths": ["a"]}, "Unknown argument"),
        (_key(setup.owner, *full), {"workspace_id": args["workspace_id"]}, "repo_path"),
        (_key(setup.stranger, *full), args, "Workspace not found"),
    ]
    for key, arguments, error in cases:
        result = await handler(key, setup.org.id, arguments)
        assert error in result[0].text, (arguments, result)
    commit_message_fake.assert_not_awaited()

    commit_message_fake.side_effect = ConflictError(
        "Set a small model", code="small_model_missing"
    )
    result = await handler(_key(setup.owner, *full), setup.org.id, args)
    assert result[0].text == "Error: Set a small model"
