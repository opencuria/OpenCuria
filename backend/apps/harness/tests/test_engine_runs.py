"""Durable external-engine run ownership, cleanup and fencing tests."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService
from apps.harness.models import (
    HarnessMessage,
    HarnessMessageRole,
    HarnessPart,
    HarnessRunStatus,
    HarnessSessionStatus,
)
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from common.exceptions import ConflictError


def _make_attempt(harness_workspace, *, user=None):
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    session.harness_id = "claude"
    session.status = HarnessSessionStatus.BUSY
    session.save(update_fields=["harness_id", "status"])
    assistant = HarnessMessageRepository.create(
        session_id=session.id,
        role=HarnessMessageRole.ASSISTANT,
        model="sonnet",
    )
    assistant.harness_id = "claude"
    assistant.save(update_fields=["harness_id"])
    owner = HarnessRunService().create_attempt(
        session_id=session.id,
        assistant_message_id=assistant.id,
        user_id=user.id if user else None,
        harness_id="claude",
    )
    return session, assistant, owner


@pytest.mark.django_db(transaction=True)
def test_attempt_persists_owner_association_and_deletion_cascades(harness_workspace):
    user = get_user_model().objects.create_user(
        email=f"run-{uuid.uuid4().hex[:8]}@example.com", password="secret"
    )
    session, assistant, owner = _make_attempt(harness_workspace, user=user)
    row = HarnessRunRepository.get_by_id(owner.run_id)

    assert row is not None
    assert row.session_id == session.id
    assert row.assistant_message_id == assistant.id
    assert row.organization_id == session.organization_id
    assert row.initiated_by_id == user.id
    assert row.harness_id == "claude"
    assert row.status == HarnessRunStatus.STARTING
    assert row.owner_token == owner.owner_token
    assert row.lease_id is None and row.lease_epoch == ""
    assert row.error == ""

    user.delete()
    row.refresh_from_db()
    assert row.initiated_by_id is None

    session.delete()
    assert not HarnessRunRepository.model.objects.filter(id=owner.run_id).exists()


@pytest.mark.django_db(transaction=True)
def test_attempt_rejects_native_mismatched_or_foreign_turns(harness_workspace):
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    assistant = HarnessMessageRepository.create(
        session_id=session.id, role=HarnessMessageRole.ASSISTANT
    )
    with pytest.raises(ValueError, match="non-native"):
        HarnessRunRepository.create_attempt(
            session_id=session.id,
            assistant_message_id=assistant.id,
            harness_id="native",
        )

    session.harness_id = "claude"
    session.save(update_fields=["harness_id"])
    with pytest.raises(ValueError, match="engine"):
        HarnessRunRepository.create_attempt(
            session_id=session.id,
            assistant_message_id=assistant.id,
            harness_id="claude",
        )

    assistant.harness_id = "claude"
    assistant.save(update_fields=["harness_id"])
    other = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    other.harness_id = "claude"
    other.save(update_fields=["harness_id"])
    with pytest.raises(ValueError, match="belong to the session"):
        HarnessRunRepository.create_attempt(
            session_id=other.id,
            assistant_message_id=assistant.id,
            harness_id="claude",
        )


@pytest.mark.django_db(transaction=True)
def test_one_unresolved_attempt_per_session_and_fresh_owner_token(harness_workspace):
    session, assistant, first_owner = _make_attempt(harness_workspace)
    with pytest.raises(ConflictError):
        HarnessRunService().create_attempt(
            session_id=session.id,
            assistant_message_id=assistant.id,
            harness_id="claude",
        )

    assert HarnessRunRepository.complete_if_owner(
        first_owner.run_id,
        first_owner.owner_token,
        status=HarnessRunStatus.COMPLETED,
    )
    new_assistant = HarnessMessageRepository.create(
        session_id=session.id, role=HarnessMessageRole.ASSISTANT
    )
    new_assistant.harness_id = "claude"
    new_assistant.save(update_fields=["harness_id"])
    second = HarnessRunService().create_attempt(
        session_id=session.id,
        assistant_message_id=new_assistant.id,
        harness_id="claude",
    )
    assert second.owner_token != first_owner.owner_token


@pytest.mark.django_db(transaction=True)
def test_fencing_blocks_missing_rows_stale_workers_and_terminal_mutations(
    harness_workspace,
):
    session, _assistant, owner = _make_attempt(harness_workspace)
    assert HarnessRunRepository.start_if_owner(owner.run_id, owner.owner_token)
    lease_id, epoch = uuid.uuid4(), "epoch-7"
    assert HarnessRunRepository.set_identity_if_owner(
        owner.run_id,
        owner.owner_token,
        lease_id=lease_id,
        lease_epoch=epoch,
    )
    assert not HarnessRunRepository.set_identity_if_owner(
        owner.run_id,
        owner.owner_token,
        lease_id=uuid.uuid4(),
        lease_epoch=epoch,
    )
    assert HarnessRunRepository.touch_if_owner(owner.run_id, owner.owner_token)

    # Simulate ownership being fenced by a replacement/recovery token.
    HarnessRunRepository.model.objects.filter(id=owner.run_id).update(
        owner_token=uuid.uuid4()
    )
    assert not HarnessRunRepository.touch_if_owner(owner.run_id, owner.owner_token)
    assert not HarnessRunRepository.set_identity_if_owner(
        owner.run_id,
        owner.owner_token,
        lease_id=uuid.uuid4(),
        lease_epoch=epoch,
    )
    assert not HarnessRunRepository.complete_if_owner(owner.run_id, owner.owner_token)
    assert not HarnessRunRepository.touch_if_owner(uuid.uuid4(), uuid.uuid4())
    session.refresh_from_db()
    assert session.status == HarnessSessionStatus.BUSY


@pytest.mark.django_db(transaction=True)
def test_closing_attempt_cannot_complete_without_confirmed_lease_cleanup(
    harness_workspace,
):
    _session, _assistant, owner = _make_attempt(harness_workspace)
    assert HarnessRunRepository.start_if_owner(owner.run_id, owner.owner_token)
    assert HarnessRunRepository.set_identity_if_owner(
        owner.run_id,
        owner.owner_token,
        lease_id=uuid.uuid4(),
        lease_epoch="epoch",
    )
    assert HarnessRunRepository.begin_close_if_owner(owner.run_id, owner.owner_token)

    assert not HarnessRunRepository.complete_if_owner(
        owner.run_id,
        owner.owner_token,
        status=HarnessRunStatus.INTERRUPTED,
        error="safe sanitized failure",
        cleanup_confirmed=False,
    )
    row = HarnessRunRepository.get_by_id(owner.run_id)
    assert row is not None and row.status == HarnessRunStatus.CLOSING
    assert row.completed_at is None
    assert not HarnessRunRepository.touch_if_owner(owner.run_id, owner.owner_token)

    assert HarnessRunRepository.complete_if_owner(
        owner.run_id,
        owner.owner_token,
        status=HarnessRunStatus.INTERRUPTED,
        error="safe sanitized failure",
        cleanup_confirmed=True,
    )
    row.refresh_from_db()
    assert row.status == HarnessRunStatus.INTERRUPTED
    assert row.error == "Engine run failed."
    assert row.completed_at is not None


class _FakeAccessor:
    def __init__(self, *, result=None, fail=False):
        self.calls = []
        self.result = result or {"ok": True, "lease_state": "released"}
        self.fail = fail

    async def desktop_action(self, action, args):
        self.calls.append((action, args))
        if self.fail:
            raise RuntimeError("offline")
        return self.result


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_recent_attempt_is_busy_and_stale_attempt_requires_confirmed_release(
    harness_workspace,
):
    session, assistant, owner = _make_attempt(harness_workspace)
    service = HarnessRunService(heartbeat_grace=timedelta(seconds=1))

    with pytest.raises(ConflictError, match="active run"):
        await service.prepare_session(session.id)

    stale = timezone.now() - timedelta(minutes=10)
    HarnessRunRepository.model.objects.filter(id=owner.run_id).update(
        heartbeat_at=stale,
        lease_id=uuid.uuid4(),
        lease_epoch="runner-epoch",
    )
    inaccessible = _FakeAccessor(fail=True)
    with pytest.raises(ConflictError, match="cleanup is pending"):
        await service.prepare_session(session.id, inaccessible)
    row = HarnessRunRepository.get_by_id(owner.run_id)
    assert row is not None and row.status == HarnessRunStatus.CLOSING
    assert HarnessMessage.objects.get(id=assistant.id).completed_at is None

    unconfirmed = _FakeAccessor(result={"ok": False, "lease_state": "closing"})
    with pytest.raises(ConflictError, match="cleanup is pending"):
        await service.prepare_session(session.id, unconfirmed)
    assert unconfirmed.calls[0][0] == "release"

    confirmed = _FakeAccessor()
    assert await service.prepare_session(session.id, confirmed)
    row.refresh_from_db()
    assistant.refresh_from_db()
    session.refresh_from_db()
    assert row.status == HarnessRunStatus.INTERRUPTED
    assert assistant.finish == "aborted"
    assert assistant.error == "Run interrupted after worker ownership expired."
    assert assistant.completed_at is not None
    assert session.status == HarnessSessionStatus.IDLE
    assert confirmed.calls[0][1] == {
        "lease_id": str(row.lease_id),
        "epoch": "runner-epoch",
        "kind": "agent",
        "owner_id": str(session.id),
        "revision": 1,
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_stale_attempt_without_identity_is_safe_to_interrupt(harness_workspace):
    session, assistant, owner = _make_attempt(harness_workspace)
    HarnessRunRepository.model.objects.filter(id=owner.run_id).update(
        heartbeat_at=timezone.now() - timedelta(minutes=10)
    )
    assert await HarnessRunService().prepare_session(session.id)
    row = HarnessRunRepository.get_by_id(owner.run_id)
    assistant.refresh_from_db()
    assert row is not None and row.status == HarnessRunStatus.INTERRUPTED
    assert assistant.error == "Run interrupted after worker ownership expired."


@pytest.mark.django_db(transaction=True)
def test_owner_fenced_part_persistence_is_not_accidentally_native(harness_workspace):
    """Run ownership records explicit engine state without touching native turns."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    native_message = HarnessMessageRepository.create(
        session_id=session.id, role=HarnessMessageRole.ASSISTANT
    )
    assert native_message.harness_id == "native"
    assert not HarnessRunRepository.model.objects.filter(
        assistant_message=native_message
    ).exists()
    assert HarnessPart.objects.filter(message_id=native_message.id).count() == 0
