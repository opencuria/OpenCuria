from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.scheduled_tasks.models import ScheduledTaskRun
from common.utils import generate_api_token, hash_token


@pytest.fixture
def schedule_api_setup(db):
    user_model = get_user_model()
    org = Organization.objects.create(
        name="Scheduled API", slug=f"scheduled-api-{uuid.uuid4().hex}"
    )
    owner = user_model.objects.create_user(
        email=f"schedule-{uuid.uuid4().hex}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"schedule-other-{uuid.uuid4().hex}@example.com", password="secret"
    )
    Membership.objects.create(user=owner, organization=org, role=MembershipRole.MEMBER)
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        organization=org, api_token_hash=uuid.uuid4().hex, status=RunnerStatus.ONLINE
    )
    workspace = Workspace.objects.create(
        runner=runner,
        created_by=owner,
        name="owner workspace",
        status=WorkspaceStatus.RUNNING,
    )
    return org, owner, stranger, workspace


def schedule_client(user, organization, permissions):
    token = generate_api_token()
    APIKey.objects.create(
        user=user,
        name="scheduled test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=permissions,
    )
    return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(organization.id))


def test_create_list_and_pause_schedule_owner_scoped(schedule_api_setup, monkeypatch):
    from apps.harness.harness_service import HarnessService

    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: "openrouter/test",
    )
    org, owner, stranger, workspace = schedule_api_setup
    client = schedule_client(
        owner,
        org,
        [APIKeyPermission.HARNESS_READ.value, APIKeyPermission.HARNESS_RUN.value],
    )
    response = client.post(
        "/api/v1/scheduled-tasks/",
        data={
            "name": "Daily review",
            "workspace_id": str(workspace.id),
            "prompt": "Review changes",
            "recurrence": "weekly",
            "weekdays": [0, 2, 4],
            "local_time": "09:15",
            "timezone_name": "America/New_York",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    task = response.json()
    assert task["weekdays"] == [0, 2, 4]
    assert task["timezone_name"] == "America/New_York"
    assert client.get("/api/v1/scheduled-tasks/").json()[0]["id"] == task["id"]

    stranger_client = schedule_client(
        stranger, org, [APIKeyPermission.HARNESS_READ.value]
    )
    assert stranger_client.get("/api/v1/scheduled-tasks/").json() == []
    assert (
        stranger_client.get(f"/api/v1/scheduled-tasks/{task['id']}/").status_code == 404
    )

    paused = client.patch(
        f"/api/v1/scheduled-tasks/{task['id']}/",
        data={"enabled": False},
        content_type="application/json",
    )
    assert paused.status_code == 200, paused.content
    assert paused.json()["enabled"] is False


def test_local_time_stays_hhmm_across_task_endpoints(schedule_api_setup, monkeypatch):
    from django.utils import timezone

    from apps.harness.harness_service import HarnessService

    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: "openrouter/test",
    )
    org, owner, _, workspace = schedule_api_setup
    client = schedule_client(
        owner,
        org,
        [APIKeyPermission.HARNESS_READ.value, APIKeyPermission.HARNESS_RUN.value],
    )
    created = client.post(
        "/api/v1/scheduled-tasks/",
        data={
            "name": "Weekly review",
            "workspace_id": str(workspace.id),
            "prompt": "Review changes",
            "recurrence": "weekly",
            "weekdays": [0],
            "local_time": "09:15",
            "timezone_name": "UTC",
        },
        content_type="application/json",
    )
    assert created.status_code == 201, created.content
    task_id = created.json()["id"]
    assert created.json()["local_time"] == "09:15"

    run = ScheduledTaskRun.objects.create(
        scheduled_task_id=task_id,
        scheduled_for=timezone.now(),
    )

    fetched = client.get(f"/api/v1/scheduled-tasks/{task_id}/")
    assert fetched.status_code == 200, fetched.content
    assert fetched.json()["local_time"] == "09:15"

    listed = client.get("/api/v1/scheduled-tasks/")
    assert listed.status_code == 200, listed.content
    listed_task = next(task for task in listed.json() if task["id"] == task_id)
    assert listed_task["local_time"] == "09:15"

    updated = client.patch(
        f"/api/v1/scheduled-tasks/{task_id}/",
        data={"local_time": "09:15"},
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content
    assert updated.json()["local_time"] == "09:15"
    history = client.get(f"/api/v1/scheduled-tasks/{task_id}/runs/")
    assert history.status_code == 200, history.content
    assert [item["id"] for item in history.json()] == [str(run.id)]


def test_claude_harness_id_round_trips_through_api(schedule_api_setup, monkeypatch):
    from apps.harness.engines.connections import EngineConnectionService
    from apps.harness.harness_service import HarnessService
    from apps.skills.models import Skill

    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: "openrouter/test",
    )
    monkeypatch.setattr(
        EngineConnectionService,
        "resolve",
        lambda self, organization_id, user_id, connection_id=None: object(),
    )
    org, owner, stranger, workspace = schedule_api_setup
    skill = Skill.objects.create(
        name="Scheduled Claude skill",
        body="Use owner-specific context",
        user=owner,
        created_by=owner,
    )
    client = schedule_client(
        owner,
        org,
        [APIKeyPermission.HARNESS_READ.value, APIKeyPermission.HARNESS_RUN.value],
    )
    response = client.post(
        "/api/v1/scheduled-tasks/",
        data={
            "name": "Claude review",
            "workspace_id": str(workspace.id),
            "prompt": "Review changes",
            "harness_id": "claude",
            "recurrence": "daily",
            "local_time": "09:00",
            "timezone_name": "UTC",
        },
        content_type="application/json",
    )
    assert response.status_code == 201, response.content
    assert response.json()["harness_id"] == "claude"
    assert response.json()["model"] == "sonnet"
    assert response.json()["reasoning_effort"] == "high"

    with_skill = client.patch(
        f"/api/v1/scheduled-tasks/{response.json()['id']}/",
        data={"skill_ids": [str(skill.id)]},
        content_type="application/json",
    )
    assert with_skill.status_code == 200, with_skill.content
    assert with_skill.json()["skill_ids"] == [str(skill.id)]
    foreign = Skill.objects.create(
        name="Another user's skill",
        body="Private context",
        user=stranger,
        created_by=stranger,
    )
    rejected = client.patch(
        f"/api/v1/scheduled-tasks/{response.json()['id']}/",
        data={"skill_ids": [str(foreign.id)]},
        content_type="application/json",
    )
    assert rejected.status_code == 400
    assert "not found or not accessible" in rejected.json()["detail"]

    task_id = response.json()["id"]
    updated = client.patch(
        f"/api/v1/scheduled-tasks/{task_id}/",
        data={"harness_id": "native"},
        content_type="application/json",
    )
    assert updated.status_code == 200, updated.content
    assert updated.json()["harness_id"] == "native"


def test_create_requires_harness_run_permission(schedule_api_setup):
    org, owner, _, workspace = schedule_api_setup
    client = schedule_client(owner, org, [APIKeyPermission.HARNESS_READ.value])
    response = client.post(
        "/api/v1/scheduled-tasks/",
        data={
            "name": "Daily review",
            "workspace_id": str(workspace.id),
            "prompt": "Review changes",
            "local_time": "09:15",
            "timezone_name": "UTC",
        },
        content_type="application/json",
    )
    assert response.status_code == 403


def test_history_and_missing_org_header_are_mapped(schedule_api_setup, monkeypatch):
    from apps.harness.harness_service import HarnessService

    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: "openrouter/test",
    )
    org, owner, _, workspace = schedule_api_setup
    client = schedule_client(
        owner,
        org,
        [APIKeyPermission.HARNESS_READ.value, APIKeyPermission.HARNESS_RUN.value],
    )
    created = client.post(
        "/api/v1/scheduled-tasks/",
        data={
            "name": "history",
            "workspace_id": str(workspace.id),
            "prompt": "Inspect",
            "local_time": "09:00",
            "timezone_name": "UTC",
        },
        content_type="application/json",
    )
    assert created.status_code == 201
    task_id = created.json()["id"]
    assert client.get(f"/api/v1/scheduled-tasks/{task_id}/runs/").status_code == 200
    assert client.get("/api/v1/scheduled-tasks/", **{"HTTP_X_ORGANIZATION_ID": ""}).status_code == 400
    assert client.patch(
        f"/api/v1/scheduled-tasks/{uuid.uuid4()}/",
        data={"enabled": False},
        content_type="application/json",
    ).status_code == 404
