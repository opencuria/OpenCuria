"""Characterize saved session settings, validation and workspace capture fences."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.models import HarnessSession
from apps.harness.repositories import HarnessSessionRepository
from apps.harness.session_settings_service import SessionSettingsService
from apps.runners.models import Workspace
from apps.skills.services import SkillService
from common.exceptions import ConflictError, NotFoundError

pytestmark = pytest.mark.django_db


@pytest.fixture
def session(harness_workspace: Workspace) -> HarnessSession:
    """Persist a session without constructing a provider or starting a run."""
    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="Original title",
        model="original-model",
        reasoning_effort="high",
    )


@pytest.mark.parametrize(
    ("action", "value", "field", "expected"),
    [
        ("set_model", "  selected-model  ", "model", "selected-model"),
        ("set_model", "  ", "model", ""),
        ("set_reasoning_effort", " LOW ", "reasoning_effort", "low"),
        ("set_reasoning_effort", "  ", "reasoning_effort", ""),
    ],
)
def test_native_settings_normalize_and_persist_without_feed_events(
    session: HarnessSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    value: str,
    field: str,
    expected: str,
) -> None:
    """Model and effort affect later runs without publishing a feed change."""
    service = SessionSettingsService()
    notifications = []
    monkeypatch.setattr(service.events, "emit_sync", notifications.append)

    updated = getattr(service, action)(session.id, value)

    assert getattr(updated, field) == expected
    session.refresh_from_db()
    assert getattr(session, field) == expected
    assert notifications == []


@pytest.mark.parametrize("is_child", [False, True])
def test_rename_persists_before_notifying_only_roots(
    session: HarnessSession, monkeypatch: pytest.MonkeyPatch, is_child: bool
) -> None:
    """Renames trim whitespace and invalidate the feed only for root sessions."""
    if is_child:
        session = HarnessSessionRepository.create(
            workspace_id=session.workspace_id,
            organization_id=session.organization_id,
            parent_id=session.id,
        )
    service = SessionSettingsService()
    notifications = []

    def notify(workspace_id: uuid.UUID) -> None:
        session.refresh_from_db()
        assert session.title == "New title"
        notifications.append(workspace_id)

    monkeypatch.setattr(service.events, "emit_sync", notify)
    updated = service.update_title(session.id, "  New title  ")

    assert updated.title == "New title"
    assert notifications == ([] if is_child else [session.workspace_id])


@pytest.mark.parametrize(
    ("action", "value"),
    [
        ("set_mode", "plan"),
        ("set_model", "model"),
        ("set_reasoning_effort", "high"),
        ("update_title", "Title"),
        ("update_skill_ids", []),
    ],
)
def test_missing_session_error_precedes_setting_validation(
    action: str, value: str | list[str]
) -> None:
    """All valid mutations retain the named HarnessSession not-found error."""
    session_id = uuid.uuid4()
    kwargs = (
        {"user_id": 1, "organization_id": uuid.uuid4()}
        if action == "update_skill_ids"
        else {}
    )
    with pytest.raises(NotFoundError) as error:
        getattr(SessionSettingsService(), action)(session_id, value, **kwargs)
    assert error.value.message == f"HarnessSession '{session_id}' not found"


def test_invalid_mode_is_rejected_before_session_lookup() -> None:
    """Mode validation historically precedes lookup, unlike other settings."""
    with pytest.raises(ValueError, match="Invalid mode"):
        SessionSettingsService().set_mode(uuid.uuid4(), "invalid")


@pytest.mark.parametrize(
    ("action", "value"),
    [("set_reasoning_effort", "invalid"), ("update_title", "  ")],
)
def test_invalid_setting_keeps_persistence_and_notifications(
    session: HarnessSession,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    value: str,
) -> None:
    """Validation failures leave the saved session and feed untouched."""
    service = SessionSettingsService()
    notifications = []
    monkeypatch.setattr(service.events, "emit_sync", notifications.append)
    with pytest.raises(ValueError):
        getattr(service, action)(session.id, value)
    session.refresh_from_db()
    assert session.title == "Original title"
    assert session.reasoning_effort == "high"
    assert notifications == []


def test_skill_selection_validates_visibility_and_keeps_order(
    session: HarnessSession, harness_workspace: Workspace
) -> None:
    """Only visible skills can replace the selection; clearing stays valid."""
    owner = harness_workspace.created_by
    skills = SkillService()
    first = skills.create_personal_skill(name="First", body="First body", user=owner)
    second = skills.create_personal_skill(name="Second", body="Second body", user=owner)
    service = SessionSettingsService()
    kwargs = {"user_id": owner.id, "organization_id": session.organization_id}

    updated = service.update_skill_ids(
        session.id, [f" {second.id} ", str(first.id), str(second.id), ""], **kwargs
    )
    assert updated.skill_ids == [str(second.id), str(first.id)]
    with pytest.raises(ValueError, match="not found or not accessible"):
        service.update_skill_ids(session.id, [str(uuid.uuid4())], **kwargs)
    with pytest.raises(ValueError, match="Invalid skill_id"):
        service.update_skill_ids(session.id, ["invalid"], **kwargs)
    session.refresh_from_db()
    assert session.skill_ids == [str(second.id), str(first.id)]

    assert service.update_skill_ids(session.id, [], **kwargs).skill_ids == []
    session.refresh_from_db()
    assert session.skill_ids == []


def test_skill_selection_cannot_bypass_capture_fence(
    session: HarnessSession, harness_workspace: Workspace
) -> None:
    """Even clearing selected skills checks the workspace fence first."""
    Workspace.objects.filter(id=session.workspace_id).update(
        active_operation="capturing_image"
    )
    with pytest.raises(ConflictError):
        SessionSettingsService().update_skill_ids(
            session.id,
            [],
            user_id=harness_workspace.created_by_id,
            organization_id=session.organization_id,
        )


@pytest.mark.parametrize(
    ("action", "value", "field", "expected"),
    [
        ("set_model", " OPUS ", "model", "opus"),
        ("set_reasoning_effort", " MAX ", "reasoning_effort", "max"),
        ("set_reasoning_effort", "", "reasoning_effort", "high"),
    ],
)
def test_claude_settings_use_engine_normalization(
    session: HarnessSession, action: str, value: str, field: str, expected: str
) -> None:
    """Claude aliases and default effort retain their engine-specific rules."""
    session.harness_id = "claude"
    session.save(update_fields=["harness_id"])
    updated = getattr(SessionSettingsService(), action)(session.id, value)
    assert getattr(updated, field) == expected
    session.refresh_from_db()
    assert getattr(session, field) == expected


@pytest.mark.parametrize(
    ("action", "value"),
    [
        ("set_model", ""),
        ("set_model", "other/model"),
        ("set_reasoning_effort", "ultra"),
    ],
)
def test_invalid_claude_settings_do_not_mutate_session(
    session: HarnessSession, action: str, value: str
) -> None:
    """Unsupported models/efforts cannot replace stored settings."""
    session.harness_id = "claude"
    session.save(update_fields=["harness_id"])
    with pytest.raises(ValueError):
        getattr(SessionSettingsService(), action)(session.id, value)
    session.refresh_from_db()
    assert session.model == "original-model"
    assert session.reasoning_effort == "high"
