"""Scheduled task tools: create, inspect, pause, run and delete by name."""

from __future__ import annotations

from datetime import timedelta

import pytest
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage
from apps.harness.provider_accounts import ProviderAccountService
from apps.scheduled_tasks.models import ScheduledTask, ScheduledTaskRun
from apps.scheduled_tasks.services import ScheduledTaskService

from .conftest import fails, ok

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _provider_ready(monkeypatch):
    monkeypatch.setattr(
        HarnessService,
        "validate_provider_for_run",
        lambda self, organization_id, session, provider=None: "openrouter/test",
    )


async def test_weekly_task_lifecycle(world):
    ctx = world.ctx()
    await ok(ctx, "skill_create", name="Deps", body="Check dependencies.")

    created = await ok(
        ctx,
        "scheduled_task_create",
        name="Dependency check",
        workspace="billing-api",
        prompt="Check for outdated dependencies and open a summary.",
        recurrence="weekly",
        weekdays=["mon", "thu"],
        time="08:30",
        timezone="Europe/Berlin",
        skills=["Deps"],
    )

    assert created["schedule"] == "weekly on Mon, Thu at 08:30 Europe/Berlin"
    assert created["weekdays"] == ["mon", "thu"]
    assert created["skills"] == ["Deps"]
    assert created["status"] == "active"

    listed = await ok(ctx, "scheduled_task_list", workspace="billing-api")
    assert [t["name"] for t in listed["scheduled_tasks"]] == ["Dependency check"]

    paused = await ok(
        ctx, "scheduled_task_update", scheduled_task="dependency check", enabled=False
    )
    assert paused["status"] == "paused"
    assert "next_run_at" not in paused

    moved = await ok(
        ctx,
        "scheduled_task_update",
        scheduled_task="Dependency check",
        recurrence="cron",
        cron_expression="0 9 * * 1-5",
    )
    assert moved["schedule"] == "cron '0 9 * * 1-5' (Europe/Berlin)"

    deleted = await ok(ctx, "scheduled_task_delete", scheduled_task="Dependency check")
    assert deleted["deleted"] is True
    assert (await ok(ctx, "scheduled_task_list"))["total"] == 0


async def test_one_time_task_and_validation_messages(world):
    ctx = world.ctx()
    tomorrow = (timezone.now() + timedelta(days=2)).date().isoformat()

    created = await ok(
        ctx,
        "scheduled_task_create",
        name="Release notes",
        workspace=str(world.workspace.id),
        prompt="Draft release notes.",
        recurrence="once",
        run_date=tomorrow,
        time="10:00",
    )
    assert created["schedule"] == f"once on {tomorrow} at 10:00 UTC"

    bad_day = await fails(
        ctx,
        "scheduled_task_create",
        name="x",
        workspace="billing-api",
        prompt="x",
        recurrence="weekly",
        weekdays=["monday"],
    )
    assert "Invalid arguments" in bad_day and "weekdays" in bad_day

    no_days = await fails(
        ctx,
        "scheduled_task_create",
        name="x",
        workspace="billing-api",
        prompt="x",
        recurrence="weekly",
    )
    assert "weekly recurrence requires weekdays" in no_days


@pytest.mark.parametrize("effort", [None, "low", "high", "ultra"])
async def test_codex_task_effort_is_saved_updated_and_used_on_run(
    world, codex_account, monkeypatch, effort
):
    ctx = world.ctx()
    settings = {"reasoning_effort": effort} if effort else {}
    created = await ok(
        ctx,
        "scheduled_task_create",
        name="Codex review",
        workspace="billing-api",
        prompt="Review this repo",
        engine="codex",
        recurrence="daily",
        **settings,
    )
    assert created["model"] == "chatgpt/gpt-6.1-sol"
    assert created["reasoning_effort"] == (effort or "high")
    updated = await ok(
        ctx,
        "scheduled_task_update",
        scheduled_task="Codex review",
        reasoning_effort="medium",
    )
    assert updated["reasoning_effort"] == "medium"
    kept = await ok(
        ctx,
        "scheduled_task_update",
        scheduled_task="Codex review",
        mode="plan",
    )
    assert kept["reasoning_effort"] == "medium"
    detail = await ok(ctx, "scheduled_task_get", scheduled_task="Codex review")
    assert detail["reasoning_effort"] == "medium"
    runs = []

    async def start_run(self, target, prompt, **kwargs):
        runs.append((target.harness_id, target.model, target.reasoning_effort))
        return await sync_to_async(HarnessMessage.objects.create)(
            session=target,
            role="assistant",
            reasoning_effort=target.reasoning_effort,
        )

    monkeypatch.setattr(HarnessService, "start_run", start_run)
    result = await ok(ctx, "scheduled_task_run_now", scheduled_task="Codex review")
    assert result["run"]["status"] == "running"
    assert runs == [("codex", "chatgpt/gpt-6.1-sol", "medium")]
    reset = await ok(
        ctx,
        "scheduled_task_update",
        scheduled_task="Codex review",
        reasoning_effort="",
    )
    assert reset["reasoning_effort"] == "high"


async def test_codex_task_rejects_unsupported_effort_and_unavailable_account(
    world,
    codex_account,
):
    settings = {
        "name": "Invalid Codex review",
        "workspace": "billing-api",
        "prompt": "Review",
        "engine": "codex",
        "recurrence": "daily",
        "model": "chatgpt/gpt-6-luna",
        "reasoning_effort": "ultra",
    }
    error = await fails(world.ctx(), "scheduled_task_create", **settings)
    assert "Invalid Codex reasoning effort" in error
    await sync_to_async(ProviderAccountService().select_account)(
        world.org.id, world.user.id, "chatgpt", None
    )
    denied = await fails(
        world.ctx(),
        "scheduled_task_create",
        **{**settings, "reasoning_effort": "high"},
    )
    assert "Connect your selected ChatGPT or OpenRouter account" in denied
    assert await sync_to_async(ScheduledTask.objects.count)() == 0


async def test_run_now_and_history(world, monkeypatch):
    ctx = world.ctx()
    await ok(
        ctx,
        "scheduled_task_create",
        name="Nightly",
        workspace="billing-api",
        prompt="Run the test suite.",
        recurrence="daily",
    )

    async def run_now(self, task):
        def record():
            return ScheduledTaskRun.objects.create(
                scheduled_task=task,
                scheduled_for=timezone.now(),
                trigger="manual",
                status=ScheduledTaskRun.Status.SKIPPED,
                reason="workspace_not_running",
            )

        from asgiref.sync import sync_to_async

        return await sync_to_async(record)()

    monkeypatch.setattr(ScheduledTaskService, "run_now", run_now)

    result = await ok(ctx, "scheduled_task_run_now", scheduled_task="Nightly")
    detail = await ok(ctx, "scheduled_task_get", scheduled_task="Nightly")

    assert result["run"]["status"] == "skipped"
    assert result["run"]["reason"] == "workspace_not_running"
    assert detail["recent_runs"][0]["trigger"] == "manual"
    assert detail["total_runs"] == 1
    assert detail["prompt"] == "Run the test suite."
