from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.scheduled_tasks.services import ScheduledTaskService


@pytest.mark.parametrize(
    "weekdays",
    [[True], ["1"], [1.0], "1", {1}],
)
def test_weekdays_reject_non_integer_values(weekdays):
    with pytest.raises(ValueError):
        ScheduledTaskService._validate_schedule(
            {
                "recurrence": "weekly",
                "weekdays": weekdays,
                "local_time": "09:00",
                "timezone_name": "UTC",
            }
        )


@pytest.mark.parametrize("skills", [[1], [None], ["not-a-uuid"]])
def test_skill_ids_must_be_uuid_strings(skills):
    with pytest.raises(ValueError):
        ScheduledTaskService._validate_skill_ids(skills)


@pytest.mark.asyncio
async def test_update_model_and_skill_changes_are_validated_without_chat_creation():
    class Repository:
        def update(self, task, **fields):
            task.updated = fields
            return task

    class Harness:
        def validate_provider_for_run(self, organization_id, session):
            return "openrouter/test"

    task = SimpleNamespace(
        name="scheduled",
        prompt="review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
        recurrence="daily",
        weekdays=[],
        local_time="09:00",
        timezone_name="UTC",
        enabled=True,
        next_run_at=datetime.now(timezone.utc),
        workspace_id="workspace",
        organization_id="org",
        owner_id=1,
    )
    service = ScheduledTaskService(repository=Repository(), harness=Harness())
    with pytest.raises(ValueError, match="model must be a string"):
        service.update(task, {"model": 3})
    with pytest.raises(ValueError, match="Invalid skill_id"):
        service.update(task, {"skill_ids": ["nope"]})
    updated = service.update(task, {"model": "openrouter/new-model"})
    assert updated.updated["model"] == "openrouter/new-model"


def test_cron_schedule_normalizes_expression_and_ignores_weekdays():
    schedule = ScheduledTaskService._validate_schedule(
        {
            "recurrence": "cron",
            "weekdays": [1, 2],
            "cron_expression": "*/15  8-17 * * 1-5",
            "timezone_name": "Europe/Berlin",
        }
    )
    assert schedule == {
        "recurrence": "cron",
        "weekdays": [],
        "cron_expression": "*/15 8-17 * * 1-5",
        "run_date": None,
        "local_time": time(9),
        "timezone_name": "Europe/Berlin",
    }
    cleared = ScheduledTaskService._validate_schedule(
        {"recurrence": "daily", "cron_expression": "* * * * *"}
    )
    assert cleared["cron_expression"] == ""


@pytest.mark.parametrize("run_date", ["2026-11-03", date(2026, 11, 3)])
def test_once_schedule_keeps_only_date_and_time(run_date):
    schedule = ScheduledTaskService._validate_schedule(
        {
            "recurrence": "once",
            "run_date": run_date,
            "weekdays": [1],
            "cron_expression": "* * * * *",
            "local_time": "14:30",
            "timezone_name": "Europe/Berlin",
        }
    )
    assert schedule == {
        "recurrence": "once",
        "weekdays": [],
        "cron_expression": "",
        "run_date": date(2026, 11, 3),
        "local_time": time(14, 30),
        "timezone_name": "Europe/Berlin",
    }
    daily = ScheduledTaskService._validate_schedule(
        {"recurrence": "daily", "run_date": "2026-11-03"}
    )
    assert daily["run_date"] is None


@pytest.mark.parametrize(
    "run_date",
    [
        None,
        "",
        "2026-02-30",
        "03.11.2026",
        "2026-11-03T09:00",
        20261103,
        datetime(2026, 11, 3, tzinfo=timezone.utc),
    ],
)
def test_once_schedule_rejects_missing_or_invalid_date(run_date):
    with pytest.raises(ValueError, match="run_date"):
        ScheduledTaskService._validate_schedule(
            {"recurrence": "once", "run_date": run_date}
        )


def test_once_update_requires_future_time_only_while_enabled():
    class Repository:
        def update(self, task, **fields):
            for key, value in fields.items():
                setattr(task, key, value)
            return task

    past = datetime(2020, 1, 1, 9, tzinfo=timezone.utc)
    task = SimpleNamespace(
        name="once",
        prompt="review",
        mode="build",
        model="",
        reasoning_effort="",
        skill_ids=[],
        recurrence="once",
        weekdays=[],
        run_date=date(2020, 1, 1),
        local_time=time(9),
        timezone_name="UTC",
        enabled=False,
        next_run_at=past,
        workspace_id="workspace",
        organization_id="org",
        owner_id=1,
    )
    service = ScheduledTaskService(repository=Repository())
    # A finished one-time task stays editable while it is disabled.
    assert service.update(task, {"name": "renamed"}).next_run_at == past
    with pytest.raises(ValueError, match="must be in the future"):
        service.update(task, {"enabled": True})
    future = date.today() + timedelta(days=30)
    updated = service.update(
        task, {"run_date": future.isoformat(), "enabled": True}
    )
    assert updated.enabled is True
    assert updated.next_run_at == datetime.combine(
        future, time(9), tzinfo=timezone.utc
    )


@pytest.mark.parametrize("expression", [None, "", "61 * * * *", 5])
def test_cron_schedule_rejects_missing_or_invalid_expression(expression):
    with pytest.raises(ValueError):
        ScheduledTaskService._validate_schedule(
            {"recurrence": "cron", "cron_expression": expression}
        )
