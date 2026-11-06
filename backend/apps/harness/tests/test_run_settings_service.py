"""Characterize engine run settings validated for unattended scheduled launches."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from apps.harness.run_settings_service import RunSettingsService

CLAUDE_RESOLVE = "apps.harness.engines.connections.EngineConnectionService.resolve"


def _validate(service: RunSettingsService, harness_id: str) -> tuple[str, str]:
    """Validate an Auto model and effort selection for owner 7."""
    return service.validate_scheduled(
        organization_id=uuid.uuid4(),
        owner_id=7,
        workspace_id=uuid.uuid4(),
        mode="build",
        harness_id=harness_id,
        model="",
        reasoning_effort="",
        skill_ids=[],
    )


def _unused_harness():
    pytest.fail("Only native settings consult the harness provider check")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(" Native ", "native"), ("CLAUDE", "claude"), ("codex", "codex")],
)
def test_harness_ids_ignore_case_and_whitespace(value, expected):
    assert RunSettingsService.normalize_harness_id(value) == expected


@pytest.mark.parametrize("value", [None, 3, "", "opencode"])
def test_unknown_harness_ids_are_rejected(value):
    with pytest.raises(ValueError, match="harness_id must be native, claude or codex"):
        RunSettingsService.normalize_harness_id(value)


def test_unknown_engines_are_never_validated_as_claude(monkeypatch):
    resolved = []
    monkeypatch.setattr(CLAUDE_RESOLVE, lambda *args: resolved.append(args))

    with pytest.raises(ValueError, match="harness_id"):
        _validate(RunSettingsService(harness=_unused_harness), "opencode")
    assert resolved == []


def test_claude_defaults_need_only_the_owners_authorization(monkeypatch):
    resolved = []

    def resolve(self, organization_id, user_id):
        resolved.append(user_id)
        return SimpleNamespace(auth_type="api_token", token="secret")

    monkeypatch.setattr(CLAUDE_RESOLVE, resolve)

    settings = _validate(RunSettingsService(harness=_unused_harness), "claude")

    assert settings == ("sonnet", "high")
    assert resolved == [7]
