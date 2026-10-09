from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time, timezone
from types import SimpleNamespace

import pytest

from apps.scheduled_tasks.models import ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService


class Repository:
    def __init__(self):
        self.created = None
        self.updated = None

    def create(self, **values):
        self.created = values
        return SimpleNamespace(**values)

    def update(self, task, **values):
        self.updated = values
        for key, value in values.items():
            setattr(task, key, value)
        return task

    def update_run(self, run, **values):
        for key, value in values.items():
            setattr(run, key, value)
        return run

    def delete_unstarted_session(self, *args, **kwargs):
        return True

    def touch_workspace_activity(self, *_):
        return None


class Workspace:
    id = uuid.uuid4()


@pytest.fixture
def allow_claude_connection(monkeypatch):
    resolved = []

    def resolve(self, organization_id, user_id, connection_id=None):
        resolved.append((organization_id, user_id, connection_id))
        return SimpleNamespace(auth_type="api_token", token="test-secret")

    monkeypatch.setattr(
        "apps.harness.engines.connections.EngineConnectionService.resolve", resolve
    )
    return resolved


def test_create_claude_schedule_normalizes_defaults_and_keeps_auth_out_of_config(
    allow_claude_connection,
):
    repo = Repository()
    service = ScheduledTaskService(repository=repo)
    org_id = uuid.uuid4()
    task = service.create(
        organization_id=org_id,
        owner_id=37,
        workspace=Workspace(),
        values={
            "name": "Claude review",
            "prompt": "Review this repo",
            "harness_id": "claude",
            "recurrence": "daily",
            "local_time": "09:00",
            "timezone_name": "UTC",
        },
    )

    assert task.harness_id == "claude"
    assert task.model == "sonnet"
    assert task.reasoning_effort == "high"
    assert task.skill_ids == []
    assert allow_claude_connection == [(org_id, 37, None)]
    assert "test-secret" not in repr(repo.created)
    assert "connection_id" not in repo.created


def test_create_claude_schedule_validates_skills_and_rejects_foreign_ids(
    allow_claude_connection, monkeypatch
):
    service = ScheduledTaskService(repository=Repository())
    base = {
        "name": "Claude review",
        "prompt": "Review this repo",
        "harness_id": "claude",
        "recurrence": "daily",
        "local_time": "09:00",
        "timezone_name": "UTC",
    }

    owner_skill_id = str(uuid.uuid4())
    foreign_skill_id = str(uuid.uuid4())
    visible_for = []

    def resolve_skills(skill_ids, *, user_id, organization_id):
        visible_for.append((skill_ids, user_id, organization_id))
        if foreign_skill_id in skill_ids:
            raise ValueError(f"Skill not found or not accessible: {foreign_skill_id}")
        return ["## Owner skill\\nSelected prompt context"]

    monkeypatch.setattr(
        "apps.harness.harness_service.resolve_skill_bodies", resolve_skills
    )

    for field, value, message in (
        ("harness_id", "unsupported", "harness_id"),
        ("model", "openrouter/anthropic/claude", "Invalid Claude model"),
        ("reasoning_effort", "turbo", "Invalid Claude reasoning effort"),
        ("skill_ids", [foreign_skill_id], "not found or not accessible"),
    ):
        with pytest.raises(ValueError, match=message):
            service.create(
                organization_id=uuid.uuid4(),
                owner_id=37,
                workspace=Workspace(),
                values={**base, field: value},
            )

    org_id = uuid.uuid4()
    created = service.create(
        organization_id=org_id,
        owner_id=37,
        workspace=Workspace(),
        values={**base, "skill_ids": [owner_skill_id]},
    )
    assert created.skill_ids == [owner_skill_id]
    assert visible_for[-1] == ([owner_skill_id], 37, org_id)


def test_switch_to_claude_keeps_skills_and_resets_engine_defaults(
    allow_claude_connection, monkeypatch
):
    repo = Repository()
    skill_id = str(uuid.uuid4())
    resolved_skills = []
    monkeypatch.setattr(
        "apps.harness.harness_service.resolve_skill_bodies",
        lambda skill_ids, *, user_id, organization_id: (
            resolved_skills.append((skill_ids, user_id, organization_id))
            or ["## Owner skill\\nSelected prompt context"]
        ),
    )
    task = SimpleNamespace(
        name="Review",
        prompt="Check",
        mode="build",
        model="openrouter/openai/gpt-test",
        reasoning_effort="medium",
        harness_id="native",
        skill_ids=[skill_id],
        recurrence="daily",
        weekdays=[],
        local_time=time(9),
        timezone_name="UTC",
        enabled=True,
        next_run_at=datetime.now(timezone.utc),
        workspace_id=Workspace.id,
        organization_id=uuid.uuid4(),
        owner_id=37,
    )

    updated = ScheduledTaskService(repository=repo).update(
        task, {"harness_id": "claude"}
    )

    assert updated.harness_id == "claude"
    assert updated.model == "sonnet"
    assert updated.reasoning_effort == "high"
    assert updated.skill_ids == [skill_id]
    assert resolved_skills == [([skill_id], task.owner_id, task.organization_id)]
    assert allow_claude_connection == [(task.organization_id, task.owner_id, None)]


def test_claude_run_dispatch_passes_harness_and_owner_without_secret():
    captured = {}

    class FakeHarness:
        def create_session(self, **values):
            captured.update(values)
            return SimpleNamespace(id=uuid.uuid4(), **values)

        async def start_run(self, session, prompt, **values):
            return SimpleNamespace(id=uuid.uuid4())

        async def _emit_conversations_changed(self, workspace_id):
            return None

    repo = Repository()
    service = ScheduledTaskService(repository=repo, harness=FakeHarness())
    task = SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=Workspace.id,
        organization_id=uuid.uuid4(),
        owner_id=37,
        prompt="Review this repo",
        mode="build",
        model="sonnet",
        reasoning_effort="high",
        harness_id="claude",
        skill_ids=[],
    )
    run = SimpleNamespace(
        id=uuid.uuid4(),
        status=ScheduledTaskRun.Status.CLAIMED,
        configuration_snapshot={"harness_id": "claude"},
    )

    result = asyncio.run(service._create_and_start_run(task, run, service._harness()))

    assert result.status == ScheduledTaskRun.Status.RUNNING
    assert captured["harness_id"] == "claude"
    assert captured["user_id"] == task.owner_id
    assert "connection_id" not in captured
    assert "token" not in captured
