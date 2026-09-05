"""Tests for the permission request flow and allowlist."""

from __future__ import annotations

import uuid

import pytest

from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.models import (
    PermissionAllowlist,
    PermissionRequest,
)
from apps.harness.permissions.service import (
    PermissionRequestRepository,
    PermissionService,
)


def _service(**kwargs) -> PermissionService:
    return PermissionService(
        evaluator=PermissionEvaluator(global_rules={"*": "ask"}), **kwargs
    )


def _ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


@pytest.mark.django_db
def test_allow_rule_needs_no_request() -> None:
    """Allow decisions create no permission request."""
    service = PermissionService(
        evaluator=PermissionEvaluator(global_rules={"*": "allow"})
    )
    org_id, session_id = _ids()
    decision, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="read",
        pattern="/workspace/a.txt",
    )
    assert decision == "allow"
    assert request is None
    assert PermissionRequest.objects.count() == 0


@pytest.mark.django_db
def test_deny_rule_needs_no_request() -> None:
    """Deny decisions create no permission request."""
    service = PermissionService(
        evaluator=PermissionEvaluator(global_rules={"bash": "deny"})
    )
    org_id, session_id = _ids()
    decision, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="rm -rf /",
    )
    assert decision == "deny"
    assert request is None


@pytest.mark.django_db
def test_ask_creates_pending_request() -> None:
    """Ask decisions persist a pending request."""
    service = _service()
    org_id, session_id = _ids()
    workspace_id = uuid.uuid4()
    decision, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        workspace_id=workspace_id,
        tool="bash",
        pattern="rm -rf /tmp/x",
        title="$ rm -rf /tmp/x",
    )
    assert decision == "ask"
    assert request is not None
    assert request.status == "pending"
    assert request.tool == "bash"
    assert request.pattern == "rm -rf /tmp/x"
    assert request.session_id == session_id
    assert request.workspace_id == workspace_id


@pytest.mark.django_db
def test_resolve_once_approves_without_allowlist() -> None:
    """once approvals resolve allow without persisting a grant."""
    service = _service()
    org_id, session_id = _ids()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="ls",
    )
    assert request is not None
    result = service.resolve(request.id, "once")
    assert result.decision == "allow"
    assert result.remember == "once"
    stored = PermissionRequest.objects.get(id=request.id)
    assert stored.status == "approved"
    assert PermissionAllowlist.objects.count() == 0
    # A second identical check still asks (no persisted grant).
    decision, second = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="ls",
    )
    assert decision == "ask"
    assert second is not None


@pytest.mark.django_db
def test_resolve_always_writes_allowlist_and_applies() -> None:
    """always approvals persist and future checks return allow."""
    service = _service()
    org_id, session_id = _ids()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="edit",
        pattern="/workspace/src/**",
    )
    assert request is not None
    result = service.resolve(request.id, "always")
    assert result.decision == "allow"
    assert result.remember == "always"
    assert PermissionAllowlist.objects.filter(
        organization_id=org_id, tool="edit"
    ).exists()
    decision, second = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="edit",
        pattern="/workspace/src/**",
    )
    assert decision == "allow"
    assert second is None


@pytest.mark.django_db
def test_resolve_reject_denies() -> None:
    """reject resolves deny without an allowlist entry."""
    service = _service()
    org_id, session_id = _ids()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="reboot",
    )
    assert request is not None
    result = service.resolve(request.id, "reject")
    assert result.decision == "deny"
    stored = PermissionRequest.objects.get(id=request.id)
    assert stored.status == "rejected"
    assert PermissionAllowlist.objects.count() == 0


@pytest.mark.django_db
def test_resolve_invalid_response_rejected() -> None:
    """Unknown resolve responses raise ValueError."""
    service = _service()
    org_id, session_id = _ids()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="ls",
    )
    assert request is not None
    with pytest.raises(ValueError, match="once\\|always\\|reject"):
        service.resolve(request.id, "maybe")


@pytest.mark.django_db
def test_resolve_missing_or_resolved_raises() -> None:
    """Resolving twice (or unknown ids) raises LookupError."""
    service = _service()
    with pytest.raises(LookupError, match="not found"):
        service.resolve(uuid.uuid4(), "once")
    org_id, session_id = _ids()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="ls",
    )
    assert request is not None
    service.resolve(request.id, "once")
    with pytest.raises(LookupError, match="not found"):
        service.resolve(request.id, "once")


@pytest.mark.django_db
def test_list_pending_for_session_filters_by_tool() -> None:
    """Pending asks for one session can be listed and filtered by tool."""
    service = _service()
    org_id, session_id = _ids()
    service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="git status",
    )
    service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="git diff",
    )
    service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="edit",
        pattern="/workspace/a.py",
    )
    bash_pending = PermissionRequestRepository.list_pending_for_session(
        session_id, tool="bash"
    )
    all_pending = PermissionRequestRepository.list_pending_for_session(session_id)
    assert len(bash_pending) == 2
    assert {item.pattern for item in bash_pending} == {"git status", "git diff"}
    assert len(all_pending) == 3
