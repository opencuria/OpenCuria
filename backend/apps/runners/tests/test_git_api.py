"""REST API tests for the workspace git integration."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def client() -> Client:
    return Client()


def _auth_headers(token: str, org_id: str) -> dict[str, str]:
    return {
        "HTTP_X_API_KEY": token,
        "HTTP_X_ORGANIZATION_ID": str(org_id),
    }


def _create_api_key(*, user, permissions: list[str]) -> str:
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="test-key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return token


def _make_context(*, email_prefix: str = "git-api"):
    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"{email_prefix}-{uuid.uuid4().hex[:8]}@test.local",
        password="secret",
    )
    org = Organization.objects.create(
        name=f"Git Org {uuid.uuid4().hex[:6]}",
        slug=f"git-org-{uuid.uuid4().hex[:10]}",
    )
    Membership.objects.create(user=user, organization=org, role=MembershipRole.ADMIN)
    runner = Runner.objects.create(
        name="git-runner",
        api_token_hash=hash_token(f"git-token-{uuid.uuid4().hex[:6]}"),
        status=RunnerStatus.ONLINE,
        sid="git-sid",
        organization=org,
    )
    workspace = Workspace.objects.create(
        runner=runner,
        name="Git Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
    return user, org, runner, workspace


def _install_fake(monkeypatch, *, run_return=None, run_side_effect=None):
    """Patch apps.runners.api._get_service with ownership-real fake."""
    import apps.runners.api as runners_api
    from apps.runners.services import RunnerService

    real = RunnerService(sio_server=None)
    if run_side_effect is not None:
        run_mock = AsyncMock(side_effect=run_side_effect)
    else:
        run_mock = AsyncMock(return_value=run_return)
    fake = SimpleNamespace(
        get_workspace_for_user=real.get_workspace_for_user,
        get_workspace=real.get_workspace,
        run_git_operation=run_mock,
    )
    monkeypatch.setattr(runners_api, "_get_service", lambda: fake)
    return fake


def _read_perms() -> list[str]:
    return [APIKeyPermission.WORKSPACES_GIT_READ.value]


def _write_perms() -> list[str]:
    return [APIKeyPermission.WORKSPACES_GIT_WRITE.value]


def _all_git_perms() -> list[str]:
    return [
        APIKeyPermission.WORKSPACES_GIT_READ.value,
        APIKeyPermission.WORKSPACES_GIT_WRITE.value,
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "operation", "args", "result"),
    [
        (
            "repos/",
            "list_repos",
            {},
            {"repos": [{"id": "r1", "name": "repo", "path": "/workspace/repo"}]},
        ),
        ("repo/", "repo_snapshot", {}, {"snapshot": {"path": "/workspace/repo"}}),
        (
            "history/",
            "repo_history",
            {"history_limit": 50, "history_skip": 0},
            {"commits": [], "has_more": False},
        ),
        ("diff/", "working_diff", {}, {"diff": {"staged": []}}),
        (
            "commits/ABC123/",
            "commit_details",
            {"commit": "abc123"},
            {"details": {"hash": "abc123"}},
        ),
    ],
)
def test_git_reads_use_get_endpoints(
    client: Client, monkeypatch, path: str, operation: str, args: dict, result: dict
):
    """Each app read endpoint forwards validated args and preserves the result."""
    user, org, _, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    runner_result = {"ok": True, **result}
    fake = _install_fake(monkeypatch, run_return=runner_result)
    query = {} if operation == "list_repos" else {"repo_path": "/workspace/repo"}

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/{path}",
        query,
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json() == runner_result
    fake.run_git_operation.assert_awaited_once_with(
        workspace.id, operation, **query, args=args
    )


@pytest.mark.django_db
def test_git_history_paging_and_branch_forwarded(client: Client, monkeypatch):
    """Explicit history_limit/skip/branch reach the service verbatim."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    fake = _install_fake(
        monkeypatch,
        run_return={"ok": True, "commits": [], "has_more": True},
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/history/",
        {
            "repo_path": "/workspace/repo",
            "history_limit": 25,
            "history_skip": 10,
            "branch": "main",
        },
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    call = fake.run_git_operation.call_args
    assert call.args[1] == "repo_history"
    assert call.kwargs["args"] == {
        "history_limit": 25,
        "history_skip": 10,
        "branch": "main",
    }


@pytest.mark.django_db
def test_git_read_only_key_blocks_mutation(client: Client, monkeypatch):
    """A git_read-only key gets 403 for POST stage; service untouched."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a.txt"]}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
def test_git_write_only_key_allows_mutation_but_blocks_read(
    client: Client, monkeypatch
):
    """git_write may mutate but may not read via GET repos/."""
    user, org, runner, workspace = _make_context()
    write_token = _create_api_key(user=user, permissions=_write_perms())
    _install_fake(monkeypatch, run_return={"ok": True, "repos": []})

    staged = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a.txt"]}
        ),
        content_type="application/json",
        **_auth_headers(write_token, str(org.id)),
    )
    assert staged.status_code == 200

    read = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/repos/",
        **_auth_headers(write_token, str(org.id)),
    )
    assert read.status_code == 403
    assert read.json()["code"] == "permission_denied"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "operation",
    ["list_repos", "repo_snapshot", "repo_history", "working_diff", "commit_details"],
)
def test_git_post_rejects_read_operations(client: Client, monkeypatch, operation: str):
    """The mutation endpoint exposes no alternative route for Git reads."""
    user, org, _, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(monkeypatch, run_return={"ok": True})

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": operation, "repo_path": "/workspace/repo", "commit": "abc123"}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 422
    assert any(issue["loc"][-1] == "operation" for issue in response.json()["detail"])
    fake.run_git_operation.assert_not_awaited()


@pytest.mark.django_db
def test_git_post_checkout_remote_branch_write_perm_routing(
    client: Client, monkeypatch
):
    """POST checkout_remote_branch is a mutation: git_write ok, git_read 403."""
    user, org, runner, workspace = _make_context()
    read_token = _create_api_key(user=user, permissions=_read_perms())
    write_token = _create_api_key(user=user, permissions=_write_perms())
    fake = _install_fake(monkeypatch, run_return={"ok": True, "snapshot": {}})
    payload = {
        "operation": "checkout_remote_branch",
        "repo_path": "/workspace/repo",
        "remote_ref": "origin/feature",
    }

    denied = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(payload),
        content_type="application/json",
        **_auth_headers(read_token, str(org.id)),
    )
    assert denied.status_code == 403

    ok = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(payload),
        content_type="application/json",
        **_auth_headers(write_token, str(org.id)),
    )
    assert ok.status_code == 200
    call = fake.run_git_operation.call_args
    assert call.args[1] == "checkout_remote_branch"
    assert call.kwargs["repo_path"] == "/workspace/repo"
    assert call.kwargs["args"] == {"remote_ref": "origin/feature"}


@pytest.mark.django_db
def test_git_checkout_remote_branch_forwards_local_name(
    client: Client, monkeypatch
):
    """POST checkout_remote_branch forwards remote_ref plus local_name."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(monkeypatch, run_return={"ok": True, "snapshot": {}})

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {
                "operation": "checkout_remote_branch",
                "repo_path": "/workspace/repo",
                "remote_ref": "origin/feature",
                "local_name": "feature",
            }
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    call = fake.run_git_operation.call_args
    assert call.kwargs["args"] == {
        "remote_ref": "origin/feature",
        "local_name": "feature",
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("operation", "input_args", "expected_args"),
    [
        ("stage", {"paths": ["a.txt"], "message": None}, {"paths": ["a.txt"]}),
        (
            "create_branch",
            {"branch": " feature ", "start_point": "main", "checkout": False},
            {"branch": "feature", "start_point": "main", "checkout": False},
        ),
        (
            "push",
            {"remote": " origin ", "set_upstream": False},
            {"remote": "origin", "set_upstream": False},
        ),
        ("stash_pop", {"stash": "stash@{0}"}, {"stash": "stash@{0}"}),
    ],
)
def test_git_mutations_forward_validated_args(
    client: Client, monkeypatch, operation: str, input_args: dict, expected_args: dict
):
    """Forward normalized fields, preserving false flags and excluding nulls."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": operation, "repo_path": "/workspace/repo", **input_args}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    fake.run_git_operation.assert_awaited_once_with(
        workspace.id,
        operation,
        repo_path="/workspace/repo",
        args=expected_args,
        user=user,
    )


@pytest.mark.django_db
def test_git_commit_happy_uses_authenticated_user(client: Client, monkeypatch):
    """POST commit passes request.user; client cannot inject author identity."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "commit", "repo_path": "/workspace/repo", "message": "hello"}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    call = fake.run_git_operation.call_args
    assert call.args[1] == "commit"
    assert call.kwargs["args"] == {"message": "hello"}
    assert "author_name" not in call.kwargs["args"]
    assert "author_email" not in call.kwargs["args"]
    assert call.kwargs["user"].id == user.id
    assert call.kwargs["user"].email == user.email


@pytest.mark.django_db
@pytest.mark.parametrize(
    "extra",
    [
        {"author_name": "Mallory"},
        {"author_email": "m@x.y"},
        {"env": {"GIT_DIR": "/tmp"}},
        {"args": {"foo": 1}},
        {"argv": ["git", "status"]},
        {"message_b64": "aGVsbG8="},
        {"unknown_field": "x"},
        {"hash": "abc123"},
        {"history_limit": 10},
        {"history_skip": 0},
    ],
)
def test_git_commit_rejects_forbidden_fields_422(
    client: Client, monkeypatch, extra: dict
):
    """author_*/env/args/argv/aliases/unknown are 422 via extra=forbid."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )
    payload = {
        "operation": "commit",
        "repo_path": "/workspace/repo",
        "message": "hello",
        **extra,
    }

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(payload),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 422
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "stage", "repo_path": "/etc/passwd", "paths": ["a.txt"]},
        {
            "operation": "checkout_commit",
            "repo_path": "/workspace/repo",
            "commit": "xyz!",
        },
        {
            "operation": "checkout_branch",
            "repo_path": "/workspace/repo",
            "branch": "",
        },
        {"operation": "stage", "repo_path": "/workspace/repo", "paths": []},
        {"operation": "stage", "repo_path": "/workspace/repo", "paths": "a.txt"},
        {
            "operation": "create_branch",
            "repo_path": "/workspace/repo",
            "branch": ["a", "b"],
        },
        {"operation": "commit", "repo_path": "/workspace/repo"},
        {"operation": "stage", "repo_path": "/workspace/repo"},
        {"operation": "stage", "paths": ["a.txt"]},
        {"operation": "stage", "repo_path": None, "paths": ["a.txt"]},
        {"operation": "rm_rf_everything", "repo_path": "/workspace/repo"},
        {"operation": "snapshot"},
        {"operation": "snapshot", "repo_path": "/workspace/repo"},
        {
            "operation": "checkout_remote_branch",
            "repo_path": "/workspace/repo",
        },
        {
            "operation": "checkout_remote_branch",
            "repo_path": "/workspace/repo",
            "remote_ref": "",
        },
    ],
)
def test_git_post_rejects_invalid_fields(client: Client, monkeypatch, payload: dict):
    """Invalid repo/hash/branch/path shapes are rejected before dispatch."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(payload),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 422
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
def test_git_pull_branch_without_remote_rejected_422(client: Client, monkeypatch):
    """pull with branch but no remote is 422 (strict contract, no silent drop)."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "pull", "repo_path": "/workspace/repo", "branch": "main"}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 422
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
def test_git_get_rejects_invalid_repo_and_hash(client: Client, monkeypatch):
    """GET diff/commit/repo/history validate inputs as 400; service untouched."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    bad_repo = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/diff/",
        {"repo_path": "/etc/passwd"},
        **_auth_headers(token, str(org.id)),
    )
    assert bad_repo.status_code == 400

    bad_hash = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/commits/zzz/",
        {"repo_path": "/workspace/repo"},
        **_auth_headers(token, str(org.id)),
    )
    assert bad_hash.status_code == 400

    bad_snapshot_repo = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/repo/",
        {"repo_path": "/etc/passwd"},
        **_auth_headers(token, str(org.id)),
    )
    assert bad_snapshot_repo.status_code == 400

    bad_history = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/history/",
        {"repo_path": "/workspace/repo", "history_limit": 0},
        **_auth_headers(token, str(org.id)),
    )
    assert bad_history.status_code == 400
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
def test_git_owner_isolation_returns_404_without_service_call(
    client: Client, monkeypatch
):
    """Foreign workspace is invisible (404) and never reaches the service."""
    user, org, runner, workspace = _make_context()
    user_model = get_user_model()
    other = user_model.objects.create_user(
        email=f"other-{uuid.uuid4().hex[:8]}@test.local", password="secret"
    )
    Membership.objects.create(
        user=other, organization=org, role=MembershipRole.MEMBER
    )
    other_token = _create_api_key(user=other, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    repos = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/repos/",
        **_auth_headers(other_token, str(org.id)),
    )
    assert repos.status_code == 404

    mutation = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a.txt"]}
        ),
        content_type="application/json",
        **_auth_headers(other_token, str(org.id)),
    )
    assert mutation.status_code == 404
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
def test_git_unknown_workspace_returns_404(client: Client, monkeypatch):
    """Unknown workspace IDs are 404 without dispatch."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "repos": []}
    )

    response = client.get(
        f"/api/v1/workspaces/{uuid.uuid4()}/git/repos/",
        **_auth_headers(token, str(org.id)),
    )
    assert response.status_code == 404
    fake.run_git_operation.assert_not_called()


@pytest.mark.django_db
def test_git_conflict_result_preserves_snapshot_and_stderr(
    client: Client, monkeypatch
):
    """Runner ok:false/conflict maps to 409 with snapshot+stderr intact."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    runner_result = {
        "ok": False,
        "code": "conflict",
        "message": "merge conflict",
        "snapshot": {"repos": [{"path": "/workspace/repo"}]},
        "stderr": "CONFLICT (content)",
    }
    fake = _install_fake(monkeypatch, run_return=runner_result)

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "push", "repo_path": "/workspace/repo", "remote": "origin"}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    body = response.json()
    assert body["ok"] is False
    assert body["code"] == "conflict"
    assert body["message"] == "merge conflict"
    assert body["snapshot"] == {"repos": [{"path": "/workspace/repo"}]}
    assert body["stderr"] == "CONFLICT (content)"
    fake.run_git_operation.assert_awaited_once()


@pytest.mark.django_db
def test_git_runner_timeout_result_maps_to_504(client: Client, monkeypatch):
    """Runner ok:false/timeout maps to 504 with code/message intact."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    _install_fake(
        monkeypatch,
        run_return={"ok": False, "code": "timeout", "message": "runner timed out"},
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/repos/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 504
    body = response.json()
    assert body["code"] == "timeout"
    assert body["message"] == "runner timed out"


@pytest.mark.django_db
def test_git_runner_other_error_maps_to_400(client: Client, monkeypatch):
    """Runner ok:false with a generic code maps to 400 intact."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    _install_fake(
        monkeypatch,
        run_return={"ok": False, "code": "validation", "message": "bad request"},
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/repos/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation"


@pytest.mark.django_db
def test_git_service_timeout_error_maps_to_504(client: Client, monkeypatch):
    """Service RunnerTimeoutError surfaces as 504 runner_timeout."""
    from apps.runners.exceptions import RunnerTimeoutError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())

    async def _timeout(*args, **kwargs):
        raise RunnerTimeoutError("git:operation", 45)

    _install_fake(monkeypatch, run_side_effect=_timeout)

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/repos/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 504
    body = response.json()
    assert body["code"] == "runner_timeout"
    assert "detail" in body


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error_factory", "expected_code"),
    [
        ("offline", "runner_offline"),
        ("busy", "workspace_conflict"),
        ("conflict", "conflict"),
    ],
)
def test_git_service_busy_offline_maps_to_409(
    client: Client, monkeypatch, error_factory: str, expected_code: str
):
    """Offline/busy service errors surface as 409 with stable codes."""
    from apps.runners.exceptions import RunnerOfflineError, WorkspaceStateError
    from common.exceptions import ConflictError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())

    if error_factory == "offline":
        exc: Exception = RunnerOfflineError(str(runner.id))
    elif error_factory == "busy":
        exc = WorkspaceStateError("workspace is busy")
    else:
        exc = ConflictError("conflict")

    async def _raise(*args, **kwargs):
        raise exc

    _install_fake(monkeypatch, run_side_effect=_raise)

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a.txt"]}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    assert response.json()["code"] == expected_code


def _install_commit_message_fake(monkeypatch, *, result=None, side_effect=None):
    """Patch the commit suggestion service used by POST /git/commit-message/."""
    import apps.harness.commit_message_api as commit_message_api

    generate = AsyncMock(return_value=result, side_effect=side_effect)
    monkeypatch.setattr(
        commit_message_api,
        "CommitMessageService",
        lambda: SimpleNamespace(generate=generate),
    )
    return generate


def _commit_message_perms() -> list[str]:
    return [
        APIKeyPermission.WORKSPACES_GIT_READ.value,
        APIKeyPermission.HARNESS_RUN.value,
    ]


def test_git_commit_message_openapi_contract() -> None:
    """Moving the adapter must preserve the public operation and schema names."""
    from config.urls import api

    schema = api.get_openapi_schema()
    operation = schema["paths"][
        "/api/v1/workspaces/{workspace_id}/git/commit-message/"
    ]["post"]
    assert operation["operationId"] == "apps_runners_api_git_commit_message"
    assert operation["tags"] == ["workspaces-git"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GitCommitMessageIn"
    }
    assert operation["responses"][200]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GitCommitMessageOut"
    }


@pytest.mark.django_db
def test_git_commit_message_happy_returns_subject(client: Client, monkeypatch):
    """POST commit-message/ delegates with organization and user scope."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_commit_message_perms())
    generate = _install_commit_message_fake(monkeypatch, result="feat: add login")

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/workspace/repo"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json() == {"message": "feat: add login"}
    generate.assert_awaited_once_with(
        workspace_id=workspace.id,
        repo_path="/workspace/repo",
        organization_id=org.id,
        user_id=user.id,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("permissions", "missing"),
    [
        ([APIKeyPermission.WORKSPACES_GIT_READ.value], "harness:run"),
        ([APIKeyPermission.HARNESS_RUN.value], "workspaces:git_read"),
    ],
)
def test_git_commit_message_requires_git_read_and_harness_run(
    client: Client, monkeypatch, permissions, missing
):
    """Reading the diff and prompting the small model both need a scope."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=permissions)
    generate = _install_commit_message_fake(monkeypatch, result="unused")

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/workspace/repo"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 403
    assert missing in response.json()["detail"]
    generate.assert_not_awaited()


@pytest.mark.django_db
def test_git_commit_message_rejects_foreign_workspace_and_bad_repo(
    client: Client, monkeypatch
):
    """Foreign workspaces 404 and repo paths outside /workspace never dispatch."""
    user, org, runner, workspace = _make_context()
    other_user, other_org, _, _ = _make_context(email_prefix="other")
    token = _create_api_key(user=other_user, permissions=_commit_message_perms())
    own_token = _create_api_key(user=user, permissions=_commit_message_perms())
    generate = _install_commit_message_fake(monkeypatch, result="unused")

    foreign = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/workspace/repo"}),
        content_type="application/json",
        **_auth_headers(token, str(other_org.id)),
    )
    bad_repo = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/etc"}),
        content_type="application/json",
        **_auth_headers(own_token, str(org.id)),
    )

    assert foreign.status_code == 404
    assert bad_repo.status_code == 422
    generate.assert_not_awaited()


@pytest.mark.django_db
def test_git_commit_message_missing_small_model_maps_to_409(
    client: Client, monkeypatch
):
    """Service conflicts keep their stable code for the webapp toast."""
    from common.exceptions import ConflictError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_commit_message_perms())
    _install_commit_message_fake(
        monkeypatch,
        side_effect=ConflictError("Set a small model", code="small_model_missing"),
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/workspace/repo"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Set a small model"
    assert response.json()["code"] == "small_model_missing"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        ("not_found", 404, "not_found"),
        ("timeout", 504, "runner_timeout"),
        ("offline", 409, "runner_offline"),
        ("workspace", 409, "workspace_conflict"),
        ("validation", 400, "validation_error"),
        ("runtime_timeout", 504, "runner_timeout"),
        ("runtime", 502, "runner_call_failed"),
        ("unexpected", 502, "runner_call_failed"),
    ],
)
def test_git_commit_message_preserves_service_errors(
    client: Client, monkeypatch, error: str, status: int, code: str
) -> None:
    """Commit suggestions retain the git API's error status, detail and code."""
    from apps.runners.exceptions import (
        RunnerOfflineError,
        RunnerTimeoutError,
        WorkspaceStateError,
    )
    from common.exceptions import NotFoundError

    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_commit_message_perms())
    errors = {
        "not_found": NotFoundError("Workspace", str(workspace.id)),
        "timeout": RunnerTimeoutError("working_diff", 75),
        "offline": RunnerOfflineError(str(runner.id)),
        "workspace": WorkspaceStateError("Workspace is busy"),
        "validation": ValueError("Invalid repository"),
        "runtime_timeout": RuntimeError("Request timed out"),
        "runtime": RuntimeError("RPC failed"),
        "unexpected": Exception("Unexpected failure"),
    }
    failure = errors[error]
    _install_commit_message_fake(monkeypatch, side_effect=failure)

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/workspace/repo"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == status
    assert response.json() == {"detail": str(failure), "code": code, "gaps": None}


@pytest.mark.django_db
@pytest.mark.parametrize("org_header", [None, "invalid", "non_member"])
def test_git_commit_message_requires_organization_membership(
    client: Client, monkeypatch, org_header: str | None
) -> None:
    """Missing, malformed or non-member org context never prompts the model."""
    from common.exceptions import AuthenticationError, NotFoundError

    user, org, _, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_commit_message_perms())
    generate = _install_commit_message_fake(monkeypatch, result="unused")
    headers = {"HTTP_X_API_KEY": token}
    if org_header == "non_member":
        other = Organization.objects.create(name="Other", slug="other")
        headers["HTTP_X_ORGANIZATION_ID"] = str(other.id)
    elif org_header is not None:
        headers["HTTP_X_ORGANIZATION_ID"] = org_header

    # The current adapter lets these errors escape before its git error mapper.
    # Preserve that behavior here; changing it is separate from moving ownership.
    error_type = NotFoundError if org_header == "non_member" else AuthenticationError
    with pytest.raises(error_type):
        client.post(
            f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
            data=json.dumps({"repo_path": "/workspace/repo"}),
            content_type="application/json",
            **headers,
        )

    generate.assert_not_awaited()


@pytest.mark.django_db
def test_git_commit_message_rejects_another_members_workspace(
    client: Client, monkeypatch
) -> None:
    """Membership in the same organization does not grant workspace ownership."""
    user, org, _, workspace = _make_context()
    member = get_user_model().objects.create_user(
        email="other-member@test.local", password="secret"
    )
    Membership.objects.create(
        user=member, organization=org, role=MembershipRole.MEMBER
    )
    token = _create_api_key(user=member, permissions=_commit_message_perms())
    generate = _install_commit_message_fake(monkeypatch, result="unused")

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/commit-message/",
        data=json.dumps({"repo_path": "/workspace/repo"}),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    generate.assert_not_awaited()
