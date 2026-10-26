"""Tests for the permission request flow and allowlist."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.harness.harness_service import HarnessService
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.permissions.models import PermissionAllowlist, PermissionRequest
from apps.harness.permissions.repositories import PermissionRequestRepository
from apps.harness.permissions.scopes import permission_scope
from apps.harness.permissions.service import PermissionService
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessSessionRepository,
)


def _service() -> PermissionService:
    return PermissionService(
        evaluator=PermissionEvaluator(global_rules={"*": "ask"})
    )


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
async def test_shared_on_permission_skips_persisted_allowlist(
    harness_workspace,
) -> None:
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


@pytest.fixture
def pending_request(db) -> PermissionRequest:
    """Create a pending request through the repository used by the harness."""
    return PermissionRequestRepository.create(
        organization_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        tool="edit",
        pattern="/workspace/src/**",
    )


@pytest.mark.parametrize(
    ("response", "decision", "status", "remember"),
    [
        ("once", "allow", "approved", "once"),
        ("always", "allow", "approved", "always"),
        ("reject", "deny", "rejected", "once"),
    ],
)
def test_resolve_permission(
    pending_request: PermissionRequest,
    response: str,
    decision: str,
    status: str,
    remember: str,
) -> None:
    """Resolve once/always/reject and persist grants only for always."""
    service = _service()
    result = service.resolve(pending_request.id, response)
    pending_request.refresh_from_db()

    assert result.request_id == pending_request.id
    assert result.decision == decision
    assert result.remember == remember
    assert pending_request.status == status
    assert pending_request.remember == remember
    assert pending_request.resolved_at is not None
    assert service.is_allowlisted(
        pending_request.organization_id,
        pending_request.tool,
        "/workspace/src/main.py",
        pending_request.workspace_id,
    ) == (response == "always")
    assert PermissionAllowlist.objects.count() == int(response == "always")


def test_allowlist_scope_and_action_matching(
    pending_request: PermissionRequest,
) -> None:
    """Grants are action-specific and bound to their organization/workspace."""
    service = _service()
    service.resolve(pending_request.id, "always")
    org_id, workspace_id = (
        pending_request.organization_id,
        pending_request.workspace_id,
    )

    assert service.is_allowlisted(
        org_id, "edit", "/workspace/src/main.py", workspace_id
    )
    for org, tool, action, workspace in [
        (org_id, "edit", "/workspace/private.py", workspace_id),
        (org_id, "read", "/workspace/src/main.py", workspace_id),
        (org_id, "edit", "/workspace/src/main.py", uuid.uuid4()),
        (org_id, "edit", "/workspace/src/main.py", None),
        (uuid.uuid4(), "edit", "/workspace/src/main.py", workspace_id),
    ]:
        assert not service.is_allowlisted(org, tool, action, workspace)


def test_resolve_invalid_missing_and_resolved_requests(
    pending_request: PermissionRequest,
) -> None:
    """Invalid answers leave the request pending; unknown/settled IDs fail."""
    service = _service()
    with pytest.raises(ValueError, match="once.*always.*reject"):
        service.resolve(pending_request.id, "maybe")
    pending_request.refresh_from_db()
    assert pending_request.status == "pending"
    assert pending_request.resolved_at is None
    assert not PermissionAllowlist.objects.exists()

    service.resolve(pending_request.id, "once")
    for request_id in (pending_request.id, uuid.uuid4()):
        with pytest.raises(LookupError, match="not found"):
            service.resolve(request_id, "once")


def test_list_pending_for_session_filters_by_tool(
    pending_request: PermissionRequest,
) -> None:
    """Pending asks are scoped to a session and optionally a tool."""
    for action in ("git status", "git diff"):
        PermissionRequestRepository.create(
            organization_id=pending_request.organization_id,
            session_id=pending_request.session_id,
            tool="bash",
            pattern=action,
        )
    bash_pending = PermissionRequestRepository.list_pending_for_session(
        pending_request.session_id, tool="bash"
    )
    all_pending = PermissionRequestRepository.list_pending_for_session(
        pending_request.session_id
    )
    assert {item.pattern for item in bash_pending} == {"git status", "git diff"}
    assert len(all_pending) == 3
