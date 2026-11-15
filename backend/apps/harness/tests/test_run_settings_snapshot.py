"""Queued prompts retain a coherent engine configuration across API races."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from apps.harness.command_service import HarnessCommandDispatcher
from apps.harness.run_settings_snapshot import RunSettingsSnapshot
from common.exceptions import ConflictError


def session():
    return SimpleNamespace(
        id=uuid.uuid4(),
        mode="build",
        agent_name="build",
        model="first-model",
        reasoning_effort="high",
        harness_id="native",
        skill_ids=["first-skill"],
    )


async def test_queued_prompt_rejects_changed_configuration_before_execution():
    current = session()
    snapshot = RunSettingsSnapshot.capture(current)
    payload = {
        "session_id": str(current.id),
        "prompt": "first prompt",
        "expected_settings": snapshot.to_payload(),
    }
    current.model = "second-model"
    service = SimpleNamespace(
        conversations=SimpleNamespace(get_session=Mock(return_value=current)),
        start_run=AsyncMock(),
    )
    with pytest.raises(ConflictError) as caught:
        await HarnessCommandDispatcher(service).execute("start", payload)
    assert caught.value.code == "run_configuration_changed"
    service.start_run.assert_not_awaited()


async def test_unchanged_configuration_reaches_execution_once():
    current = session()
    service = SimpleNamespace(
        conversations=SimpleNamespace(get_session=Mock(return_value=current)),
        start_run=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
    )
    await HarnessCommandDispatcher(service).execute(
        "start",
        {
            "session_id": str(current.id),
            "prompt": "first prompt",
            "expected_settings": RunSettingsSnapshot.capture(current).to_payload(),
        },
    )
    service.start_run.assert_awaited_once_with(current, prompt="first prompt")


def test_skill_persistence_reread_preserves_captured_engine_but_updated_skills():
    original = session()
    snapshot = RunSettingsSnapshot.capture(original)
    reread = session()
    reread.mode, reread.agent_name = "plan", "plan"
    reread.model, reread.reasoning_effort = "second-model", "low"
    reread.skill_ids = ["validated-new-selection"]
    snapshot.retain_execution_settings(reread)
    assert (reread.mode, reread.agent_name, reread.model, reread.reasoning_effort) == (
        "build",
        "build",
        "first-model",
        "high",
    )
    assert reread.skill_ids == ["validated-new-selection"]
    assert RunSettingsSnapshot.from_payload(snapshot.to_payload()) == snapshot


@pytest.mark.parametrize("value", [None, {}, {"skill_ids": []}])
def test_invalid_persisted_snapshot_is_rejected(value):
    with pytest.raises(ValueError, match="Invalid run settings snapshot"):
        RunSettingsSnapshot.from_payload(value)


@pytest.mark.django_db(transaction=True)
async def test_skill_update_cannot_redirect_real_run_to_a_concurrently_saved_model(
    harness_workspace, monkeypatch
):
    from asgiref.sync import sync_to_async

    from apps.harness.harness_service import HarnessService
    from apps.harness.repositories import HarnessSessionRepository
    from apps.harness.tests.test_harness_service import FakeProvider, _session

    current = await sync_to_async(_session)(harness_workspace)
    service = HarnessService(
        provider_factory=lambda _org: FakeProvider([]), emit=AsyncMock()
    )
    update = service.session_settings.update_skill_ids

    def update_after_concurrent_change(*args, **kwargs):
        HarnessSessionRepository.set_model(current, "concurrently-saved-model")
        return update(*args, **kwargs)

    monkeypatch.setattr(
        service.session_settings, "update_skill_ids", update_after_concurrent_change
    )
    try:
        assistant = await service.start_run(
            current,
            "keep submission settings",
            user_id=harness_workspace.created_by_id,
            skill_ids=[],
        )
        await service.run_registry.get_task(str(current.id))
        assert assistant.model == "fake-model"
        stored = await sync_to_async(HarnessSessionRepository.get_by_id)(current.id)
        assert stored.model == "concurrently-saved-model"
    finally:
        await service.shutdown_runs()


async def test_omitted_edit_overrides_retain_submission_defaults(monkeypatch):
    from apps.harness.command_service import RemoteHarnessService
    from apps.harness.repositories import HarnessMessageRepository

    current = session()
    result = SimpleNamespace(id=uuid.uuid4())
    client = SimpleNamespace(call=AsyncMock(return_value=str(result.id)))
    reads = SimpleNamespace(
        sessions=Mock(),
        messages=Mock(),
        parts=Mock(),
        conversations=SimpleNamespace(get_session=Mock(return_value=current)),
        session_settings=Mock(),
        workspace_admission=Mock(),
        metadata=Mock(),
        interactions=Mock(),
    )
    monkeypatch.setattr(
        HarnessMessageRepository, "get_by_id", Mock(return_value=result)
    )
    remote = RemoteHarnessService(reads, client=client)
    assert (
        await remote.edit_user_message(
            current.id,
            prompt="edit",
            message_id=uuid.uuid4(),
            user_id=1,
            mode=None,
            model=None,
            reasoning_effort=None,
            skill_ids=None,
        )
        is result
    )
    submitted = client.call.await_args.args[1]
    assert submitted["mode"] == "build"
    assert submitted["model"] == "first-model"
    assert submitted["reasoning_effort"] == "high"
    assert submitted["skill_ids"] == ["first-skill"]
