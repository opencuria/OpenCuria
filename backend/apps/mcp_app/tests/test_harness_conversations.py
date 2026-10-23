"""Conversation feed contracts and query scaling shared by MCP and REST."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.harness_service import HarnessService
from apps.harness.repositories import HarnessSessionRepository
from apps.mcp_app import server as mcp_server
from apps.organizations.models import Membership, Organization
from apps.runners.models import Runner, Workspace
from common.exceptions import NotFoundError
from common.utils import generate_api_token, hash_token


@pytest.fixture
def conversations_setup(db, monkeypatch):
    """Use real ownership queries and a service without runner/provider I/O."""
    org = Organization.objects.create(name="Feed", slug=f"feed-{uuid.uuid4().hex[:8]}")
    owner = get_user_model().objects.create_user(
        email=f"feed-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org)
    runner = Runner.objects.create(
        name="Feed runner",
        organization=org,
        api_token_hash=hash_token(uuid.uuid4().hex),
    )
    token = generate_api_token()
    key = APIKey.objects.create(
        user=owner,
        name="Feed key",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    service = HarnessService()
    monkeypatch.setattr(mcp_server, "_get_harness_service", lambda: service)
    client = Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(org.id))
    return org, owner, runner, key, client


@pytest.mark.django_db
@pytest.mark.parametrize("workspace_count", [1, 10])
def test_conversations_query_count_and_rest_parity(
    conversations_setup, django_assert_num_queries, workspace_count: int
) -> None:
    """Both adapters return public root engines without per-workspace reads."""
    org, owner, runner, key, client = conversations_setup
    roots = {}
    for index in range(workspace_count):
        workspace = Workspace.objects.create(
            name=f"Workspace {index}", runner=runner, created_by=owner
        )
        engine = ("native", "claude", "codex")[index % 3]
        root = HarnessSessionRepository.create(
            workspace_id=workspace.id,
            organization_id=org.id,
            harness_id=engine,
            engine_state={"connection_id": "private-connection"},
        )
        roots[str(root.id)] = engine
        HarnessSessionRepository.create(
            workspace_id=workspace.id,
            organization_id=org.id,
            parent_id=root.id,
            harness_id="codex",
        )

    # Membership/workspaces: 3; root sessions/unread/attention: 6.
    with django_assert_num_queries(9):
        result = mcp_server._call_list_harness_conversations(key, org.id, {})
    rows = json.loads(result[0].text)
    assert {row["session_id"]: row["harness_id"] for row in rows} == roots
    assert "private-connection" not in result[0].text

    response = client.get("/api/v1/harness/conversations/")
    assert response.status_code == 200
    rest_rows = response.json()
    # REST encodes milliseconds; MCP retains the service's full microseconds.
    for payload in (rows, rest_rows):
        for row in payload:
            for field in ("updated_at", "last_message_at"):
                row[field] = datetime.fromisoformat(
                    row[field].replace("Z", "+00:00")
                ).isoformat(timespec="milliseconds")
    assert rows == rest_rows


@pytest.mark.django_db
def test_conversations_empty_for_other_workspace_owner(conversations_setup) -> None:
    """Organization membership alone does not expose another owner's sessions."""
    org, _owner, runner, key, _client = conversations_setup
    other = get_user_model().objects.create_user(
        email=f"feed-other-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    Membership.objects.create(user=other, organization=org)
    workspace = Workspace.objects.create(name="Other", runner=runner, created_by=other)
    HarnessSessionRepository.create(
        workspace_id=workspace.id, organization_id=org.id, harness_id="claude"
    )

    result = mcp_server._call_list_harness_conversations(key, org.id, {})
    assert json.loads(result[0].text) == []


@pytest.mark.django_db
def test_conversations_rejects_foreign_organization(conversations_setup) -> None:
    """The handler checks membership before loading any feed rows."""
    _org, _owner, _runner, key, _client = conversations_setup
    foreign = Organization.objects.create(
        name="Foreign", slug=f"foreign-{uuid.uuid4().hex[:8]}"
    )
    with pytest.raises(NotFoundError):
        mcp_server._call_list_harness_conversations(key, foreign.id, {})
