"""Editor endpoints reuse organization, owner, and API-key permission checks."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from django.test import Client

from apps.accounts.models import APIKeyPermission
from apps.runners.services import RunnerService
from apps.runners.tests.test_git_api import (
    _auth_headers,
    _create_api_key,
    _make_context,
)


def install(monkeypatch, result=None):
    """Keep ownership real while isolating the external runner transport."""
    import apps.runners.api as api

    real = RunnerService(sio_server=None)
    result = result or {"ok": True, "content": "text", "revision": "a" * 64}
    fake = SimpleNamespace(
        get_workspace_for_user=real.get_workspace_for_user,
        editor_file_operation=AsyncMock(return_value=result),
        run_git_operation=AsyncMock(return_value=result),
    )
    monkeypatch.setattr(api, "_get_service", lambda: fake)
    return fake


@pytest.mark.django_db
def test_editor_file_read_save_permissions_and_stale_response(monkeypatch):
    user, org, _, workspace = _make_context()
    read = _create_api_key(user=user, permissions=[APIKeyPermission.WORKSPACES_READ])
    write = _create_api_key(user=user, permissions=[APIKeyPermission.WORKSPACES_UPDATE])
    fake = install(monkeypatch)
    client = Client()
    url = f"/api/v1/workspaces/{workspace.id}/files/content/"
    response = client.get(url, {"path": "/workspace/a"}, **_auth_headers(read, org.id))
    assert response.status_code == 200
    assert fake.editor_file_operation.call_args.args[1:] == (
        "read",
        {"path": "/workspace/a"},
    )
    payload = {
        "path": "/workspace/a",
        "content": "changed",
        "expected_revision": "a" * 64,
    }
    response = client.put(
        url,
        json.dumps(payload),
        content_type="application/json",
        **_auth_headers(read, org.id),
    )
    assert response.status_code == 403
    fake.editor_file_operation.return_value = {
        "ok": False,
        "code": "stale_document",
        "message": "Reload the file",
    }
    response = client.put(
        url,
        json.dumps(payload),
        content_type="application/json",
        **_auth_headers(write, org.id),
    )
    assert response.status_code == 409
    assert response.json()["code"] == "stale_document"
    assert fake.editor_file_operation.call_args.args[1:] == ("write", payload)


@pytest.mark.django_db
def test_editor_other_organization_and_owner_are_denied(monkeypatch):
    owner, org, _, workspace = _make_context()
    outsider, other_org, _, _ = _make_context()
    token = _create_api_key(
        user=outsider, permissions=[APIKeyPermission.WORKSPACES_READ]
    )
    fake = install(monkeypatch)
    response = Client().get(
        f"/api/v1/workspaces/{workspace.id}/files/content/",
        {"path": "/workspace/a"},
        **_auth_headers(token, other_org.id),
    )
    assert response.status_code == 404
    fake.editor_file_operation.assert_not_called()


@pytest.mark.django_db
def test_git_file_content_and_partial_mutation_permission_boundaries(monkeypatch):
    user, org, _, workspace = _make_context()
    read = _create_api_key(
        user=user, permissions=[APIKeyPermission.WORKSPACES_GIT_READ]
    )
    write = _create_api_key(
        user=user, permissions=[APIKeyPermission.WORKSPACES_GIT_WRITE]
    )
    fake = install(monkeypatch)
    client = Client()
    read_url = f"/api/v1/workspaces/{workspace.id}/git/file-content/"
    query = {"repo_path": "/workspace/repo", "path": "a", "revision": "index"}
    response = client.get(read_url, query, **_auth_headers(read, org.id))
    assert response.status_code == 200
    assert fake.run_git_operation.call_args.args[1] == "file_content"
    assert (
        client.get(read_url, query, **_auth_headers(write, org.id)).status_code == 403
    )
    payload = {
        "repo_path": "/workspace/repo",
        "operation": "stage_patch",
        "path": "a",
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
        "expected_index_revision": "a" * 64,
        "expected_worktree_revision": "b" * 64,
    }
    url = f"/api/v1/workspaces/{workspace.id}/git/operation/"
    response = client.post(
        url,
        json.dumps(payload),
        content_type="application/json",
        **_auth_headers(read, org.id),
    )
    assert response.status_code == 403
    response = client.post(
        url,
        json.dumps(payload),
        content_type="application/json",
        **_auth_headers(write, org.id),
    )
    assert response.status_code == 200
    assert fake.run_git_operation.call_args.args[1] == "stage_patch"


@pytest.mark.parametrize(
    "operation,args",
    [
        ("stage_patch", {"path": "../outside", "patch": "@@ -1 +1 @@\n-a\n+b\n"}),
        ("stage_patch", {"path": "a", "patch": "diff --git a/a b/a\n"}),
        ("stage_patch", {"path": "a", "patch": "@@ -1 +1 @@\n-a\n+b\n", "env": {}}),
        ("file_content", {"path": ".git/config", "revision": "index"}),
        ("file_content", {"path": "a", "revision": "HEAD:../private"}),
    ],
)
def test_editor_git_arguments_reject_paths_patch_headers_and_free_environment(
    operation, args
):
    with pytest.raises(ValueError):
        RunnerService._sanitize_git_args(operation, args)


def test_git_paths_preserve_filename_whitespace_across_validation_layers():
    """A path from Git status must address that exact file in every operation."""
    from apps.runners.schemas import GitOperationIn

    path = " leading and trailing "
    validated = GitOperationIn(
        operation="stage", repo_path="/workspace/repo", paths=[path]
    )
    assert validated.paths == [path]
    assert RunnerService._sanitize_git_args("stage", {"paths": [path]}) == {
        "paths": [path]
    }


def test_unstage_requires_head_token_and_validates_rename_source():
    """A stable index alone cannot prove a staged diff's HEAD side is current."""
    payload = {
        "path": "new.txt",
        "old_path": "old.txt",
        "patch": "@@ -1 +1 @@\n-old\n+new\n",
        "expected_index_revision": "a" * 64,
        "expected_worktree_revision": "b" * 64,
        "expected_head_revision": "c" * 64,
    }
    assert RunnerService._sanitize_git_args("unstage_patch", payload) == payload
    for changes in (
        {"expected_head_revision": None},
        {"old_path": "../outside"},
        {"old_path": ".git/config"},
    ):
        with pytest.raises(ValueError):
            RunnerService._sanitize_git_args("unstage_patch", {**payload, **changes})
