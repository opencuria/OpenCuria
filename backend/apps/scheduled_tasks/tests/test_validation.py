from datetime import datetime, timezone
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
    recurrence, weekdays, _, zone, cron = ScheduledTaskService._validate_schedule(
        {
            "recurrence": "cron",
            "weekdays": [1, 2],
            "cron_expression": "*/15  8-17 * * 1-5",
            "timezone_name": "Europe/Berlin",
        }
    )
    assert (recurrence, weekdays, zone, cron) == (
        "cron",
        [],
        "Europe/Berlin",
        "*/15 8-17 * * 1-5",
    )
    _, _, _, _, cleared = ScheduledTaskService._validate_schedule(
        {"recurrence": "daily", "cron_expression": "* * * * *"}
    )
    assert cleared == ""


@pytest.mark.parametrize("expression", [None, "", "61 * * * *", 5])
def test_cron_schedule_rejects_missing_or_invalid_expression(expression):
    with pytest.raises(ValueError):
        ScheduledTaskService._validate_schedule(
            {"recurrence": "cron", "cron_expression": expression}
        )
