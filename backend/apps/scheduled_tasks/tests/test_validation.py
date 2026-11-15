import uuid
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from apps.harness.models import HarnessSession
from apps.scheduled_tasks.services import ScheduledTaskService
from common.exceptions import AuthenticationError, NotFoundError

CLAUDE_RESOLVE = "apps.harness.engines.connections.EngineConnectionService.resolve"
CODEX_VALIDATE = (
    "apps.harness.engines.service.EngineCatalogService.validate_codex_config"
)


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


class CapturingRepository:
    """Record created schedule values without touching the database."""

    def __init__(self):
        self.created = None

    def create(self, **values):
        self.created = values
        return SimpleNamespace(**values)


def _create_schedule(service, **engine):
    """Create a daily schedule for owner 7 with the given engine settings."""
    return service.create(
        organization_id=engine.pop("organization_id", uuid.uuid4()),
        owner_id=7,
        workspace=engine.pop("workspace", SimpleNamespace(id=uuid.uuid4())),
        values={
            "name": "Engine review",
            "prompt": "Review the repository",
            "recurrence": "daily",
            "local_time": "09:00",
            "timezone_name": "UTC",
            **engine,
        },
    )


def test_native_schedule_checks_owner_provider_with_an_unsaved_session():
    checked = []

    class Harness:
        def validate_provider_for_run(self, organization_id, session):
            checked.append((organization_id, session))
            return "openrouter/resolved-default"

    repo = CapturingRepository()
    org_id, workspace = uuid.uuid4(), SimpleNamespace(id=uuid.uuid4())
    _create_schedule(
        ScheduledTaskService(repository=repo, harness=Harness()),
        organization_id=org_id,
        workspace=workspace,
        mode=" Plan ",
        model=" openrouter/test ",
        reasoning_effort=" HIGH ",
    )

    [(checked_org, session)] = checked
    assert checked_org == org_id
    # No database access is allowed here: the session only carries the
    # candidate settings into the owner's provider readiness check.
    assert isinstance(session, HarnessSession)
    assert (session.workspace_id, session.organization_id) == (workspace.id, org_id)
    assert (session.created_by_id, session.mode, session.agent_name) == (
        7,
        "plan",
        "plan",
    )
    assert (session.model, session.reasoning_effort, session.skill_ids) == (
        "openrouter/test",
        "high",
        [],
    )
    # The schedule keeps the owner's selection, not the resolved default.
    assert (
        repo.created["harness_id"],
        repo.created["model"],
        repo.created["reasoning_effort"],
    ) == ("native", "openrouter/test", "high")


def test_codex_schedule_stores_catalog_normalized_settings(monkeypatch):
    calls = []

    def validate(self, organization_id, user_id, model, effort, **scope):
        calls.append((organization_id, user_id, model, effort, scope))
        return "chatgpt/normalized", "medium"

    monkeypatch.setattr(CODEX_VALIDATE, validate)
    repo = CapturingRepository()
    org_id = uuid.uuid4()
    _create_schedule(
        ScheduledTaskService(repository=repo),
        organization_id=org_id,
        harness_id=" CODEX ",
        model=" chatgpt/raw ",
        reasoning_effort=" Medium ",
    )

    assert calls == [(org_id, 7, "chatgpt/raw", "Medium", {})]
    assert (
        repo.created["harness_id"],
        repo.created["model"],
        repo.created["reasoning_effort"],
    ) == ("codex", "chatgpt/normalized", "medium")


@pytest.mark.parametrize(
    ("harness_id", "target", "error", "message"),
    [
        (
            "claude",
            CLAUDE_RESOLVE,
            NotFoundError("ProviderAccount", "7"),
            "Connect your personal Claude authorization before scheduling Claude tasks",
        ),
        (
            "codex",
            CODEX_VALIDATE,
            NotFoundError("ProviderAccount", "7"),
            "Connect your selected ChatGPT or OpenRouter account before "
            "scheduling Codex tasks",
        ),
        (
            "codex",
            CODEX_VALIDATE,
            AuthenticationError("ChatGPT authorization expired"),
            "Connect your selected ChatGPT or OpenRouter account before "
            "scheduling Codex tasks",
        ),
        # Only a missing Claude authorization becomes an actionable hint.
        ("claude", CLAUDE_RESOLVE, AuthenticationError("Not a member"), None),
    ],
)
def test_external_engine_schedule_requires_owner_authorization(
    monkeypatch, harness_id, target, error, message
):
    def unavailable(*args, **kwargs):
        raise error

    monkeypatch.setattr(target, unavailable)
    repo = CapturingRepository()
    service = ScheduledTaskService(repository=repo)

    with pytest.raises(ValueError if message else type(error)) as excinfo:
        _create_schedule(service, harness_id=harness_id)

    if message:
        assert str(excinfo.value) == message
        assert excinfo.value.__cause__ is error
    else:
        assert excinfo.value is error
    assert repo.created is None
