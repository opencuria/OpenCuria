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
        available_runtimes=["docker"],
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
def test_git_snapshot_happy_passes_history_args(client: Client, monkeypatch):
    """GET snapshot returns 200 and forwards history paging to the service."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    runner_result = {
        "ok": True,
        "operation": "snapshot",
        "snapshot": {"repos": [{"path": "/workspace/repo"}]},
    }
    fake = _install_fake(monkeypatch, run_return=runner_result)

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["snapshot"] == {"repos": [{"path": "/workspace/repo"}]}
    fake.run_git_operation.assert_awaited_once()
    call = fake.run_git_operation.call_args
    assert str(call.args[0]) == str(workspace.id)
    assert call.args[1] == "snapshot"
    assert call.kwargs["args"] == {"history_limit": 200, "history_skip": 0}


@pytest.mark.django_db
def test_git_snapshot_history_paging_forwarded(client: Client, monkeypatch):
    """Explicit history_limit/skip reach the service verbatim."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    fake = _install_fake(
        monkeypatch,
        run_return={"ok": True, "snapshot": {"repos": []}},
    )

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/",
        {"history_limit": 50, "history_skip": 10},
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    call = fake.run_git_operation.call_args
    assert call.args[1] == "snapshot"
    assert call.kwargs["args"] == {"history_limit": 50, "history_skip": 10}


@pytest.mark.django_db
def test_git_diff_happy_forwards_repo_path(client: Client, monkeypatch):
    """GET diff forwards the validated repo path to working_diff."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    runner_result = {"ok": True, "operation": "working_diff", "diff": {"staged": []}}
    fake = _install_fake(monkeypatch, run_return=runner_result)

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/diff/",
        {"repo_path": "/workspace/repo"},
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json()["diff"] == {"staged": []}
    call = fake.run_git_operation.call_args
    assert call.args[1] == "working_diff"
    assert call.kwargs["repo_path"] == "/workspace/repo"
    assert call.kwargs["args"] == {}


@pytest.mark.django_db
def test_git_commit_details_happy_forwards_hash(client: Client, monkeypatch):
    """GET commit details forwards normalized hash and repo path."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    runner_result = {"ok": True, "operation": "commit_details", "details": {"hash": "abc123"}}
    fake = _install_fake(monkeypatch, run_return=runner_result)

    response = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/commits/ABC123/",
        {"repo_path": "/workspace/repo"},
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json()["details"] == {"hash": "abc123"}
    call = fake.run_git_operation.call_args
    assert call.args[1] == "commit_details"
    assert call.kwargs["repo_path"] == "/workspace/repo"
    assert call.kwargs["args"] == {"commit": "abc123"}


@pytest.mark.django_db
def test_git_read_only_key_blocks_mutation(client: Client, monkeypatch):
    """A git_read-only key gets 403 for POST stage; service untouched."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
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
    """git_write may mutate but may not read via GET snapshot."""
    user, org, runner, workspace = _make_context()
    write_token = _create_api_key(user=user, permissions=_write_perms())
    _install_fake(monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}})

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
        f"/api/v1/workspaces/{workspace.id}/git/",
        **_auth_headers(write_token, str(org.id)),
    )
    assert read.status_code == 403
    assert read.json()["code"] == "permission_denied"


@pytest.mark.django_db
def test_git_post_snapshot_read_perm_routing(client: Client, monkeypatch):
    """POST snapshot is a read op: git_read succeeds, git_write alone 403."""
    user, org, runner, workspace = _make_context()
    read_token = _create_api_key(user=user, permissions=_read_perms())
    write_token = _create_api_key(user=user, permissions=_write_perms())
    _install_fake(monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}})

    ok = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps({"operation": "snapshot"}),
        content_type="application/json",
        **_auth_headers(read_token, str(org.id)),
    )
    assert ok.status_code == 200

    denied = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps({"operation": "snapshot"}),
        content_type="application/json",
        **_auth_headers(write_token, str(org.id)),
    )
    assert denied.status_code == 403


@pytest.mark.django_db
def test_git_stage_happy_forwards_typed_args(client: Client, monkeypatch):
    """POST stage forwards repo_path plus typed paths."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
    )

    response = client.post(
        f"/api/v1/workspaces/{workspace.id}/git/operation/",
        data=json.dumps(
            {"operation": "stage", "repo_path": "/workspace/repo", "paths": ["a.txt"]}
        ),
        content_type="application/json",
        **_auth_headers(token, str(org.id)),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    call = fake.run_git_operation.call_args
    assert call.args[1] == "stage"
    assert call.kwargs["repo_path"] == "/workspace/repo"
    assert call.kwargs["args"] == {"paths": ["a.txt"]}


@pytest.mark.django_db
def test_git_commit_happy_uses_authenticated_user(client: Client, monkeypatch):
    """POST commit passes request.user; client cannot inject author identity."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
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
    ],
)
def test_git_commit_rejects_forbidden_fields_422(
    client: Client, monkeypatch, extra: dict
):
    """author_*/env/args/argv/aliases/unknown are 422 via extra=forbid."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
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
        {"operation": "rm_rf_everything", "repo_path": "/workspace/repo"},
    ],
)
def test_git_post_rejects_invalid_fields(client: Client, monkeypatch, payload: dict):
    """Invalid repo/hash/branch/path shapes are rejected before dispatch."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_all_git_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
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
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
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
    """GET diff/commit validate repo path and hash as 400; service untouched."""
    user, org, runner, workspace = _make_context()
    token = _create_api_key(user=user, permissions=_read_perms())
    fake = _install_fake(
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
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

    bad_history = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/",
        {"history_limit": 0},
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
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
    )

    snapshot = client.get(
        f"/api/v1/workspaces/{workspace.id}/git/",
        **_auth_headers(other_token, str(org.id)),
    )
    assert snapshot.status_code == 404

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
        monkeypatch, run_return={"ok": True, "snapshot": {"repos": []}}
    )

    response = client.get(
        f"/api/v1/workspaces/{uuid.uuid4()}/git/",
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
        f"/api/v1/workspaces/{workspace.id}/git/",
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
        f"/api/v1/workspaces/{workspace.id}/git/",
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
        f"/api/v1/workspaces/{workspace.id}/git/",
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
