"""MCP access-control tests for the workspace git integration."""

from __future__ import annotations

import inspect
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from mcp.types import ListToolsRequest

from apps.accounts.models import APIKeyPermission
from apps.mcp_app.server import (
    _GIT_MCP_ALLOWED_ARGS,
    _GIT_MUTATION_OPERATIONS,
    _GIT_OPERATIONS,
    _GIT_READ_OPERATIONS,
    _TOOL_HANDLERS,
    _TOOL_PERMISSIONS,
    _TOOLS,
    _call_get_git_commit,
    _call_get_git_diff,
    _call_get_git_state,
    _call_git_operation,
    _check_git_tool_permission,
    _git_mcp_args,
    create_mcp_server,
)
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.runners.services import RunnerService
from common.utils import hash_token

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


def _make_git_setup():
    """Create an org with owner + stranger and one owned workspace (sync)."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Git MCP Org {uuid.uuid4().hex[:6]}",
        slug=f"git-mcp-org-{uuid.uuid4().hex[:8]}",
    )
    owner = user_model.objects.create_user(
        email=f"git-mcp-owner-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    stranger = user_model.objects.create_user(
        email=f"git-mcp-stranger-{uuid.uuid4().hex[:6]}@example.com",
        password="secret",
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="git-mcp-runner",
        api_token_hash=hash_token(f"git-mcp-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"git-mcp-sid-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Git MCP Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    return {
        "org": org,
        "owner": owner,
        "stranger": stranger,
        "workspace": workspace,
    }


def _text(result) -> str:
    assert len(result) == 1
    return result[0].text


def _payload(result) -> dict | list:
    return json.loads(_text(result))


def _perm_key(*perms: APIKeyPermission) -> SimpleNamespace:
    values = {p.value for p in perms}

    class _Key:
        user = None

        def has_permission(self, permission) -> bool:
            value = getattr(permission, "value", permission)
            return value in values

    return _Key()  # type: ignore[return-value]


async def _list_tool_names(api_key) -> list[str]:
    server = create_mcp_server(api_key)
    handler = server.request_handlers[ListToolsRequest]
    result = await handler(ListToolsRequest(method="tools/list"))
    return [tool.name for tool in result.root.tools]


def test_git_tools_registered_with_expected_permissions() -> None:
    """Exactly four git tools exist; reads need git_read, generic needs git_write."""
    git_tools = {tool.name for tool in _TOOLS if "git" in tool.name}
    assert git_tools == {
        "get_git_state",
        "get_git_diff",
        "get_git_commit",
        "git_operation",
    }
    assert _TOOL_PERMISSIONS["get_git_state"] == APIKeyPermission.WORKSPACES_GIT_READ
    assert _TOOL_PERMISSIONS["get_git_diff"] == APIKeyPermission.WORKSPACES_GIT_READ
    assert _TOOL_PERMISSIONS["get_git_commit"] == APIKeyPermission.WORKSPACES_GIT_READ
    assert _TOOL_PERMISSIONS["git_operation"] == APIKeyPermission.WORKSPACES_GIT_WRITE
    for name in (
        "get_git_state",
        "get_git_diff",
        "get_git_commit",
        "git_operation",
    ):
        assert name in _TOOL_HANDLERS
        assert inspect.iscoroutinefunction(_TOOL_HANDLERS[name])


def test_git_tool_schemas_reject_unknown_fields() -> None:
    """All git tool schemas forbid additional properties."""
    for tool in _TOOLS:
        if "git" not in tool.name:
            continue
        assert tool.inputSchema.get("additionalProperties") is False


def test_generic_git_tool_schema_is_mutation_only() -> None:
    """The generic git_operation schema must not advertise read operations."""
    tool = next(tool for tool in _TOOLS if tool.name == "git_operation")
    enum = tool.inputSchema["properties"]["operation"]["enum"]
    assert set(enum) == set(_GIT_MUTATION_OPERATIONS)
    assert not (set(enum) & set(_GIT_READ_OPERATIONS))
    assert set(_GIT_MUTATION_OPERATIONS) == set(_GIT_OPERATIONS) - set(
        _GIT_READ_OPERATIONS
    )


def test_generic_git_tool_allowed_args_mirror_service() -> None:
    """MCP arg whitelist stays in sync with RunnerService._GIT_ALLOWED_ARGS."""
    from apps.runners.services import RunnerService

    assert set(_GIT_MCP_ALLOWED_ARGS) == set(RunnerService._GIT_ALLOWED_ARGS)
    for operation, allowed in RunnerService._GIT_ALLOWED_ARGS.items():
        assert _GIT_MCP_ALLOWED_ARGS[operation] == allowed


@pytest.mark.django_db(transaction=True)
async def test_mcp_git_tool_filtering_by_key_permissions() -> None:
    """Read-only keys see only dedicated reads; write-only sees only generic."""
    read_names = await _list_tool_names(
        _perm_key(APIKeyPermission.WORKSPACES_GIT_READ)
    )
    assert "get_git_state" in read_names
    assert "get_git_diff" in read_names
    assert "get_git_commit" in read_names
    assert "git_operation" not in read_names

    write_names = await _list_tool_names(
        _perm_key(APIKeyPermission.WORKSPACES_GIT_WRITE)
    )
    assert "git_operation" in write_names
    assert "get_git_state" not in write_names
    assert "get_git_diff" not in write_names
    assert "get_git_commit" not in write_names


@pytest.mark.django_db(transaction=True)
async def test_mcp_generic_git_write_only_key_cannot_read() -> None:
    """No privilege escalation: write-only key is denied read ops via generic."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    write_key = SimpleNamespace(user=setup["owner"])
    write_key.has_permission = lambda perm: getattr(
        perm, "value", perm
    ) == APIKeyPermission.WORKSPACES_GIT_WRITE.value
    for operation in ("snapshot", "working_diff", "commit_details"):
        error = _check_git_tool_permission(write_key, operation)
        assert error is not None
        assert APIKeyPermission.WORKSPACES_GIT_READ.value in _text(error)
    # Mutations still pass the per-call guard for a write-only key.
    assert _check_git_tool_permission(write_key, "stage") is None


@pytest.mark.django_db(transaction=True)
async def test_mcp_generic_git_read_only_key_cannot_mutate() -> None:
    """Read-only key passes reads via generic guard but is denied mutations."""
    read_key = SimpleNamespace(user=None)
    read_key.has_permission = lambda perm: getattr(
        perm, "value", perm
    ) == APIKeyPermission.WORKSPACES_GIT_READ.value
    for operation in ("snapshot", "working_diff", "commit_details"):
        assert _check_git_tool_permission(read_key, operation) is None
    error = _check_git_tool_permission(read_key, "stage")
    assert error is not None
    assert APIKeyPermission.WORKSPACES_GIT_WRITE.value in _text(error)


@pytest.mark.django_db(transaction=True)
async def test_mcp_git_rejects_foreign_workspace_before_service(monkeypatch) -> None:
    """A member cannot read git state of a workspace they do not own."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()

    real_service = RunnerService()

    class _NoDispatchService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            return real_service.get_workspace_for_user(
                workspace_id, user=user, organization_id=organization_id
            )

        async def run_git_operation(self, *args, **kwargs):
            raise AssertionError("service must not be called for foreign workspace")

    monkeypatch.setattr("apps.mcp_app.server._runner_service", lambda: _NoDispatchService())
    api_key = SimpleNamespace(
        user=setup["stranger"], has_permission=lambda permission: True
    )
    result = await _call_get_git_state(
        api_key,
        setup["org"].id,
        {"workspace_id": str(setup["workspace"].id)},
    )
    assert _text(result) == "Error: Workspace not found"


@pytest.mark.django_db(transaction=True)
async def test_mcp_get_git_state_happy_path(monkeypatch) -> None:
    """get_git_state dispatches snapshot with history paging, no repo path."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    calls: list[tuple] = []

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            assert str(workspace_id) == str(setup["workspace"].id)
            assert user == setup["owner"]
            return setup["workspace"]

        async def run_git_operation(
            self, workspace_id, operation, *, repo_path=None, args=None, user=None
        ):
            calls.append((workspace_id, operation, repo_path, args, user))
            return {"ok": True, "operation": "snapshot", "snapshot": {"repos": []}}

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(user=setup["owner"])
    result = await _call_get_git_state(
        api_key,
        setup["org"].id,
        {
            "workspace_id": str(setup["workspace"].id),
            "history_limit": 50,
            "history_skip": 10,
        },
    )
    payload = _payload(result)
    assert payload["ok"] is True
    assert calls and calls[0][1] == "snapshot"
    assert calls[0][2] is None
    assert calls[0][3] == {"history_limit": 50, "history_skip": 10}
    assert calls[0][4] == setup["owner"]


@pytest.mark.django_db(transaction=True)
async def test_mcp_get_git_diff_happy_path(monkeypatch) -> None:
    """get_git_diff dispatches working_diff with the validated repo path."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    calls: list[tuple] = []

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            return setup["workspace"]

        async def run_git_operation(
            self, workspace_id, operation, *, repo_path=None, args=None, user=None
        ):
            calls.append((workspace_id, operation, repo_path, args, user))
            return {"ok": True, "operation": "working_diff", "diff": {"staged": []}}

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(user=setup["owner"])
    result = await _call_get_git_diff(
        api_key,
        setup["org"].id,
        {
            "workspace_id": str(setup["workspace"].id),
            "repo_path": "/workspace/repo",
        },
    )
    assert _payload(result)["diff"] == {"staged": []}
    assert calls and calls[0][1] == "working_diff"
    assert calls[0][2] == "/workspace/repo"
    assert calls[0][3] == {}


@pytest.mark.django_db(transaction=True)
async def test_mcp_get_git_commit_happy_path(monkeypatch) -> None:
    """get_git_commit dispatches commit_details with hash and repo path."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    calls: list[tuple] = []

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            return setup["workspace"]

        async def run_git_operation(
            self, workspace_id, operation, *, repo_path=None, args=None, user=None
        ):
            calls.append((workspace_id, operation, repo_path, args, user))
            return {"ok": True, "operation": "commit_details", "details": {}}

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(user=setup["owner"])
    result = await _call_get_git_commit(
        api_key,
        setup["org"].id,
        {
            "workspace_id": str(setup["workspace"].id),
            "repo_path": "/workspace/repo",
            "commit": "abc123",
        },
    )
    assert _payload(result)["ok"] is True
    assert calls and calls[0][1] == "commit_details"
    assert calls[0][2] == "/workspace/repo"
    assert calls[0][3] == {"commit": "abc123"}


@pytest.mark.django_db(transaction=True)
async def test_mcp_git_operation_commit_happy_path_passes_user(monkeypatch) -> None:
    """commit via generic tool forwards user; author_* never accepted."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    calls: list[tuple] = []

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            return setup["workspace"]

        async def run_git_operation(
            self, workspace_id, operation, *, repo_path=None, args=None, user=None
        ):
            calls.append((workspace_id, operation, repo_path, args, user))
            return {"ok": True, "operation": "commit", "snapshot": {"repos": []}}

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(
        user=setup["owner"],
        has_permission=lambda perm: getattr(perm, "value", perm)
        == APIKeyPermission.WORKSPACES_GIT_WRITE.value,
    )
    result = await _call_git_operation(
        api_key,
        setup["org"].id,
        {
            "workspace_id": str(setup["workspace"].id),
            "operation": "commit",
            "repo_path": "/workspace/repo",
            "message": "hello",
        },
    )
    assert _payload(result)["ok"] is True
    assert calls and calls[0][1] == "commit"
    assert calls[0][2] == "/workspace/repo"
    assert calls[0][3] == {"message": "hello"}
    assert "author_name" not in calls[0][3]
    assert "author_email" not in calls[0][3]
    assert calls[0][4] == setup["owner"]


@pytest.mark.django_db(transaction=True)
async def test_mcp_git_operation_stage_happy_path(monkeypatch) -> None:
    """stage via generic tool forwards strict paths payload."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    calls: list[tuple] = []

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            return setup["workspace"]

        async def run_git_operation(
            self, workspace_id, operation, *, repo_path=None, args=None, user=None
        ):
            calls.append((workspace_id, operation, repo_path, args, user))
            return {"ok": True, "operation": "stage", "snapshot": {"repos": []}}

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(
        user=setup["owner"],
        has_permission=lambda perm: getattr(perm, "value", perm)
        == APIKeyPermission.WORKSPACES_GIT_WRITE.value,
    )
    result = await _call_git_operation(
        api_key,
        setup["org"].id,
        {
            "workspace_id": str(setup["workspace"].id),
            "operation": "stage",
            "repo_path": "/workspace/repo",
            "paths": ["a.txt"],
        },
    )
    assert _payload(result)["ok"] is True
    assert calls and calls[0][1] == "stage"
    assert calls[0][3] == {"paths": ["a.txt"]}


@pytest.mark.django_db(transaction=True)
async def test_mcp_git_runner_errors_returned_intact(monkeypatch) -> None:
    """Runner ok:false payloads pass through as MCP JSON (no stripping)."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()

    class _FakeService:
        def get_workspace_for_user(self, workspace_id, *, user, organization_id):
            return setup["workspace"]

        async def run_git_operation(
            self, workspace_id, operation, *, repo_path=None, args=None, user=None
        ):
            return {
                "ok": False,
                "code": "conflict",
                "message": "merge conflict",
                "snapshot": {"repos": [{"path": "/workspace/repo"}]},
                "stderr": "CONFLICT (content)",
            }

    monkeypatch.setattr(
        "apps.mcp_app.server._runner_service", lambda: _FakeService()
    )
    api_key = SimpleNamespace(
        user=setup["owner"],
        has_permission=lambda perm: getattr(perm, "value", perm)
        == APIKeyPermission.WORKSPACES_GIT_WRITE.value,
    )
    result = await _call_git_operation(
        api_key,
        setup["org"].id,
        {
            "workspace_id": str(setup["workspace"].id),
            "operation": "push",
            "repo_path": "/workspace/repo",
            "remote": "origin",
        },
    )
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["code"] == "conflict"
    assert payload["message"] == "merge conflict"
    assert payload["snapshot"] == {"repos": [{"path": "/workspace/repo"}]}
    assert payload["stderr"] == "CONFLICT (content)"


@pytest.mark.django_db(transaction=True)
async def test_mcp_git_handlers_validate_args_before_owner() -> None:
    """Missing/invalid git args fail fast before any service dispatch."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    api_key = SimpleNamespace(
        user=setup["owner"], has_permission=lambda permission: True
    )
    org_id = setup["org"].id
    workspace_id = str(setup["workspace"].id)

    assert "workspace_id is required" in _text(
        await _call_get_git_state(api_key, org_id, {})
    )
    assert "Invalid workspace_id UUID" in _text(
        await _call_get_git_state(
            api_key, org_id, {"workspace_id": "not-a-uuid"}
        )
    )
    assert "repo_path is required" in _text(
        await _call_get_git_diff(api_key, org_id, {"workspace_id": workspace_id})
    )
    assert "must be under /workspace" in _text(
        await _call_get_git_diff(
            api_key,
            org_id,
            {"workspace_id": workspace_id, "repo_path": "/etc/passwd"},
        )
    )
    assert "commit is required" in _text(
        await _call_get_git_commit(
            api_key,
            org_id,
            {"workspace_id": workspace_id, "repo_path": "/workspace/repo"},
        )
    )
    assert "Invalid commit hash" in _text(
        await _call_get_git_commit(
            api_key,
            org_id,
            {
                "workspace_id": workspace_id,
                "repo_path": "/workspace/repo",
                "commit": "xyz!",
            },
        )
    )
    assert "operation is required" in _text(
        await _call_git_operation(api_key, org_id, {"workspace_id": workspace_id})
    )
    assert "Unknown git operation" in _text(
        await _call_git_operation(
            api_key,
            org_id,
            {"workspace_id": workspace_id, "operation": "rm_rf_everything"},
        )
    )
    assert "repo_path is required" in _text(
        await _call_git_operation(
            api_key,
            org_id,
            {"workspace_id": workspace_id, "operation": "stage"},
        )
    )


def test_git_mcp_args_rejects_forbidden_and_unknown_keys() -> None:
    """env/author_*/args/argv/aliases/typos are rejected, never forwarded."""
    base = {
        "workspace_id": str(uuid.uuid4()),
        "operation": "commit",
        "repo_path": "/workspace/repo",
        "message": "hello",
    }
    for extra in (
        {"env": {"GIT_DIR": "/tmp"}},
        {"author_name": "Mallory"},
        {"author_email": "m@x.y"},
        {"args": {"foo": 1}},
        {"argv": ["git", "status"]},
        {"message_b64": "aGVsbG8="},
        {"hash": "abc123"},
        {"unknown_field": "x"},
    ):
        out, error = _git_mcp_args("commit", {**base, **extra})
        assert out is None
        assert error is not None
    out, error = _git_mcp_args("commit", dict(base))
    assert error is None
    assert out == {"message": "hello"}


def test_git_mcp_args_validates_required_paths_hash_branch_remote() -> None:
    """Per-operation required fields and shapes fail fast with clear errors."""
    workspace_id = str(uuid.uuid4())
    out, error = _git_mcp_args(
        "stage",
        {"workspace_id": workspace_id, "operation": "stage"},
    )
    assert out is None
    assert "paths is required" in _text(error)

    out, error = _git_mcp_args(
        "commit_details",
        {"workspace_id": workspace_id, "operation": "commit_details"},
    )
    assert out is None
    assert "commit is required" in _text(error)

    out, error = _git_mcp_args(
        "commit_details",
        {
            "workspace_id": workspace_id,
            "operation": "commit_details",
            "commit": "xyz!",
        },
    )
    assert out is None
    assert "Invalid commit hash" in _text(error)

    out, error = _git_mcp_args(
        "checkout_branch",
        {"workspace_id": workspace_id, "operation": "checkout_branch"},
    )
    assert out is None
    assert "branch is required" in _text(error)

    out, error = _git_mcp_args(
        "fetch",
        {
            "workspace_id": workspace_id,
            "operation": "fetch",
            "remote": "   ",
        },
    )
    assert out is None
    assert "Invalid remote" in _text(error)


def test_git_mcp_args_validates_pagination_without_clipping() -> None:
    """history_limit/skip reject out-of-range input instead of clipping."""
    workspace_id = str(uuid.uuid4())
    out, error = _git_mcp_args(
        "snapshot",
        {"workspace_id": workspace_id, "history_limit": 0},
    )
    assert out is None
    assert "history_limit" in _text(error)

    out, error = _git_mcp_args(
        "snapshot",
        {"workspace_id": workspace_id, "history_limit": 501},
    )
    assert out is None
    assert "history_limit" in _text(error)

    out, error = _git_mcp_args(
        "snapshot",
        {"workspace_id": workspace_id, "history_limit": "not-a-number"},
    )
    assert out is None
    assert "history_limit" in _text(error)

    out, error = _git_mcp_args(
        "snapshot",
        {"workspace_id": workspace_id, "history_skip": -1},
    )
    assert out is None
    assert "history_skip" in _text(error)

    out, error = _git_mcp_args(
        "snapshot",
        {"workspace_id": workspace_id, "history_limit": 50, "history_skip": 10},
    )
    assert error is None
    assert out == {"history_limit": 50, "history_skip": 10}


def test_git_mcp_args_rejects_long_merge_message_without_clipping() -> None:
    """Merge messages over 4096 chars are rejected, never silently truncated."""
    workspace_id = str(uuid.uuid4())
    long_message = "x" * 4097
    out, error = _git_mcp_args(
        "merge_into_current",
        {
            "workspace_id": workspace_id,
            "operation": "merge_into_current",
            "repo_path": "/workspace/repo",
            "branch": "feature",
            "message": long_message,
        },
    )
    assert out is None
    assert "message too long" in _text(error)

    out, error = _git_mcp_args(
        "merge_current_into",
        {
            "workspace_id": workspace_id,
            "operation": "merge_current_into",
            "repo_path": "/workspace/repo",
            "target": "main",
            "message": long_message,
        },
    )
    assert out is None
    assert "message too long" in _text(error)

    ok_message = "x" * 4096
    out, error = _git_mcp_args(
        "merge_into_current",
        {
            "workspace_id": workspace_id,
            "operation": "merge_into_current",
            "repo_path": "/workspace/repo",
            "branch": "feature",
            "message": ok_message,
        },
    )
    assert error is None
    assert out == {"branch": "feature", "message": ok_message}


def test_git_mcp_args_pull_branch_without_remote_rejected() -> None:
    """pull with branch but no remote is a strict error (no silent drop)."""
    workspace_id = str(uuid.uuid4())
    out, error = _git_mcp_args(
        "pull",
        {
            "workspace_id": workspace_id,
            "operation": "pull",
            "repo_path": "/workspace/repo",
            "branch": "main",
        },
    )
    assert out is None
    assert "remote is required" in _text(error)

    out, error = _git_mcp_args(
        "pull",
        {
            "workspace_id": workspace_id,
            "operation": "pull",
            "repo_path": "/workspace/repo",
            "remote": "origin",
            "branch": "main",
        },
    )
    assert error is None
    assert out == {"remote": "origin", "branch": "main"}


@pytest.mark.django_db(transaction=True)
async def test_mcp_generic_git_operation_rejects_reads_with_tool_guidance() -> None:
    """generic git_operation with a read op points at dedicated tools (no dispatch)."""
    from asgiref.sync import sync_to_async

    setup = await sync_to_async(_make_git_setup)()
    api_key = SimpleNamespace(
        user=setup["owner"], has_permission=lambda permission: True
    )
    for operation in ("snapshot", "working_diff", "commit_details"):
        result = await _call_git_operation(
            api_key,
            setup["org"].id,
            {
                "workspace_id": str(setup["workspace"].id),
                "operation": operation,
                "repo_path": "/workspace/repo",
            },
        )
        text = _text(result)
        assert "Unknown git operation" in text
        assert "get_git_state" in text
        assert "get_git_diff" in text
        assert "get_git_commit" in text
