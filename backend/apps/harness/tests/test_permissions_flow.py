"""Tests for the permission request flow and allowlist."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.models import PermissionAllowlist, PermissionRequest
from apps.harness.permissions.repositories import PermissionRequestRepository
from apps.harness.permissions.scopes import permission_scope
from apps.harness.permissions.service import PermissionService
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessSessionRepository,
)


def _service(**kwargs) -> PermissionService:
    return PermissionService(
        evaluator=PermissionEvaluator(global_rules={"*": "ask"}), **kwargs
    )


def _ids() -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4()


@pytest.mark.django_db(transaction=True)
async def test_redacted_actions_keep_secret_specific_always_scope(
    harness_workspace,
) -> None:
    """Identically redacted commands do not share grants across credentials."""
    events: list[dict[str, object]] = []

    async def emit(_name: str, data: dict[str, object]) -> None:
        events.append(dict(data))

    permissions = _service()
    service = HarnessService(permissions=permissions, emit=emit)
    org_id = harness_workspace.runner.organization_id
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="sensitive action",
        agent_name="build",
        mode="build",
        model="test-model",
    )
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant"
    )
    actions = [
        "curl --token sensitive-test-secret-01 https://example.invalid",
        "curl --token sensitive-test-secret-02 https://example.invalid",
    ]
    display = "curl --token [redacted] https://example.invalid"

    first = asyncio.create_task(
        service._on_permission(
            session,
            assistant,
            tool="bash",
            action=display,
            permission_action=actions[0],
            title=display,
            call_id="sensitive-1",
        )
    )
    request = None
    for _ in range(50):
        request = await sync_to_async(
            PermissionRequest.objects.filter(session_id=session.id).first
        )()
        if request is not None:
            break
        await asyncio.sleep(0.01)
    assert request is not None
    assert actions[0] not in request.pattern
    assert actions[0] not in request.title
    assert request.pattern == permission_scope(actions[0], display)
    for _ in range(50):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events[0]["pattern"] == display
    assert actions[0] not in repr(events)
    await service.resolve_permission(
        session=session, request_id=request.id, response="always"
    )
    assert await first == "once"

    second = asyncio.create_task(
        service._on_permission(
            session,
            assistant,
            tool="bash",
            action=display,
            permission_action=actions[1],
            title=display,
            call_id="sensitive-2",
        )
    )
    pending = None
    for _ in range(50):
        pending = await sync_to_async(
            PermissionRequest.objects.filter(
                session_id=session.id, pattern=permission_scope(actions[1], display)
            ).first
        )()
        if pending is not None:
            break
        await asyncio.sleep(0.01)
    assert pending is not None
    assert pending.status == "pending"
    assert not second.done()
    await service.resolve_permission(
        session=session, request_id=pending.id, response="once"
    )
    assert await second == "once"

    matching = await service._on_permission(
        session,
        assistant,
        tool="bash",
        action=display,
        permission_action=actions[0],
        title=display,
        call_id="sensitive-3",
    )
    assert matching == "once"
    assert await sync_to_async(
        PermissionRequest.objects.filter(session_id=session.id).count
    )() == 2
    legacy_redacted_allow = await sync_to_async(permissions.allowlist.add)(
        organization_id=org_id,
        workspace_id=session.workspace_id,
        tool="bash",
        pattern=display,
    )
    assert legacy_redacted_allow.pattern == display
    second_scope = permission_scope(actions[1], display)
    first_scope = permission_scope(actions[0], display)
    assert not await sync_to_async(permissions.is_allowlisted)(
        org_id, "bash", second_scope, session.workspace_id
    )
    assert await sync_to_async(permissions.is_allowlisted)(
        org_id, "bash", first_scope, session.workspace_id
    )
    await sync_to_async(permissions.allowlist.add)(
        organization_id=org_id,
        workspace_id=session.workspace_id,
        tool="bash",
        pattern="curl *",
    )
    assert not await sync_to_async(permissions.is_allowlisted)(
        org_id, "bash", second_scope, session.workspace_id
    )
    plain_scope = permission_scope("curl https://example.invalid", "curl https://example.invalid")
    assert await sync_to_async(permissions.is_allowlisted)(
        org_id, "bash", plain_scope, session.workspace_id
    )


@pytest.mark.django_db(transaction=True)
async def test_shared_on_permission_skips_persisted_allowlist(harness_workspace) -> None:
    """The shared callback used by both engines honors existing grants."""
    permissions = PermissionService(
        evaluator=PermissionEvaluator(global_rules={"edit": "allow"})
    )
    service = HarnessService(permissions=permissions)
    org_id = harness_workspace.runner.organization_id
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="permission callback",
        agent_name="build",
        mode="build",
        model="test-model",
    )
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant"
    )
    await sync_to_async(PermissionAllowlist.objects.create)(
        organization_id=org_id,
        workspace_id=harness_workspace.id,
        tool="read",
        pattern="/workspace/docs/*.md",
    )

    assert await service._on_permission(
        session,
        assistant,
        tool="read",
        action="/workspace/docs/guide.md",
        title="Read guide",
        call_id="call-shared",
    ) == "once"
    assert not PermissionRequest.objects.filter(session_id=session.id).exists()

    permissions.evaluator = PermissionEvaluator(global_rules={"read": "deny"})
    assert not await service._authorize_file_read(
        session, assistant, "/workspace/docs/guide.md"
    )


@pytest.mark.django_db(transaction=True)
async def test_always_resolves_only_matching_pending_sibling(harness_workspace) -> None:
    """Always does not approve other pending actions for the same tool."""
    service = HarnessService(permissions=_service())
    org_id = harness_workspace.runner.organization_id
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org_id,
        title="sibling gates",
        agent_name="build",
        mode="build",
        model="test-model",
    )
    matching = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="edit",
        pattern="/workspace/src/allowed.py",
    )
    nonmatching = await sync_to_async(PermissionRequestRepository.create)(
        organization_id=org_id,
        session_id=session.id,
        workspace_id=harness_workspace.id,
        tool="edit",
        pattern="/workspace/src/other.py",
    )
    await sync_to_async(PermissionAllowlist.objects.create)(
        organization_id=org_id,
        workspace_id=harness_workspace.id,
        tool="edit",
        pattern="/workspace/src/allowed.py",
    )

    await service._resolve_sibling_permissions(
        session=session,
        tool="edit",
        skip_id=uuid.uuid4(),
        decision="allow",
        remember="always",
    )

    matching.refresh_from_db()
    nonmatching.refresh_from_db()
    assert matching.status == "approved"
    assert nonmatching.status == "pending"


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
def test_allowlist_scope_and_action_matching() -> None:
    """Grants are action-specific and workspace-safe."""
    service = _service()
    org_id, session_id = _ids()
    workspace_id = uuid.uuid4()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        workspace_id=workspace_id,
        tool="read",
        pattern="/workspace/src/**",
    )
    assert request is not None
    service.resolve(request.id, "always")

    assert service.is_allowlisted(
        org_id, "read", "/workspace/src/main.py", workspace_id
    )
    assert not service.is_allowlisted(
        org_id, "read", "/workspace/private.py", workspace_id
    )
    assert not service.is_allowlisted(
        org_id, "edit", "/workspace/src/main.py", workspace_id
    )
    assert not service.is_allowlisted(
        org_id, "read", "/workspace/src/main.py", uuid.uuid4()
    )
    assert not service.is_allowlisted(org_id, "read", "/workspace/src/main.py")


@pytest.mark.django_db
def test_persisted_allowlist_does_not_override_explicit_deny() -> None:
    """An evaluator deny takes precedence over persisted always grants."""
    service = _service()
    org_id, session_id = _ids()
    _, request = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="dangerous command",
    )
    assert request is not None
    service.resolve(request.id, "always")
    service.evaluator = PermissionEvaluator(
        global_rules={"bash": {"dangerous*": "deny"}}
    )

    assert service.check("bash", "dangerous command", organization_id=org_id) == "deny"
    decision, followup = service.check_or_request(
        organization_id=org_id,
        session_id=session_id,
        tool="bash",
        pattern="dangerous command",
    )
    assert decision == "deny"
    assert followup is None


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
