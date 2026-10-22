"""Abort stale external descendants through the common ownership recovery path."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from apps.harness.engines.run_repository import HarnessRunRepository
from apps.harness.engines.runs import HarnessRunService, RunOwnership
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessMessage, HarnessSession
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tests.test_engine_runs import _make_attempt
from common.exceptions import ConflictError

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.asyncio]


def detached_child(
    workspace: Any, engine_id: str
) -> tuple[HarnessSession, HarnessSession, HarnessMessage, RunOwnership]:
    """Build a settled parent with an external child missing from the task map."""
    parent, _, parent_owner = _make_attempt(workspace, harness_id=engine_id)
    assert HarnessRunRepository.complete_if_owner(
        parent_owner.run_id, parent_owner.owner_token, status="interrupted"
    )
    child, assistant, owner = _make_attempt(workspace, harness_id=engine_id)
    child.parent_id = parent.id
    child.save(update_fields=["parent_id"])
    assert HarnessRunRepository.set_identity_if_owner(
        owner.run_id, owner.owner_token, lease_id=uuid.uuid4(), lease_epoch="epoch"
    )
    return parent, child, assistant, owner


@pytest.mark.parametrize("engine_id", ["claude", "codex"])
@pytest.mark.parametrize("status", ["closing", "stale-starting", "stale-running"])
async def test_root_abort_recovers_detached_external_child(
    harness_workspace: Any, engine_id: str, status: str
) -> None:
    """Closing and expired children can be stopped once the runner confirms cleanup."""
    parent, child, assistant, owner = detached_child(harness_workspace, engine_id)
    if status == "closing":
        assert HarnessRunRepository.begin_close_if_owner(
            owner.run_id, owner.owner_token
        )
    else:
        if status == "stale-running":
            assert HarnessRunRepository.start_if_owner(owner.run_id, owner.owner_token)
        HarnessRunRepository.model.objects.filter(id=owner.run_id).update(
            heartbeat_at=timezone.now() - timedelta(minutes=10)
        )
    accessor = FakeAccessor(
        str(child.workspace_id), desktop_result={"ok": True, "lease_state": "released"}
    )
    service = HarnessService(
        run_service=HarnessRunService(), accessor_factory=lambda _: accessor
    )
    result = await service.abort_run(parent.id)
    assert result.status == "idle"
    child.refresh_from_db()
    assistant.refresh_from_db()
    attempt = HarnessRunRepository.get_by_id(owner.run_id)
    assert child.status == "idle"
    assert attempt.status == "interrupted"
    assert assistant.finish == "aborted"
    assert not HarnessRunRepository.is_current_owner(owner.run_id, owner.owner_token)
    assert HarnessRunRepository.get_open_for_session(child.id) is None
    releases = [
        args for action, args, _ in accessor.desktop_calls if action == "release"
    ]
    assert len(releases) == 1
    assert releases[0]["owner_id"] == str(child.id)
    assert releases[0]["lease_id"] == str(attempt.lease_id)
    assert not service._abort_requested


@pytest.mark.parametrize("engine_id", ["claude", "codex"])
@pytest.mark.parametrize("status", ["starting", "running"])
async def test_root_abort_preserves_fresh_foreign_child_ownership(
    harness_workspace: Any, engine_id: str, status: str
) -> None:
    """A healthy remote owner must not be cancelled or have its lease released."""
    parent, child, assistant, owner = detached_child(harness_workspace, engine_id)
    if status == "running":
        assert HarnessRunRepository.start_if_owner(owner.run_id, owner.owner_token)
    accessor = FakeAccessor(str(child.workspace_id))
    service = HarnessService(
        run_service=HarnessRunService(), accessor_factory=lambda _: accessor
    )
    with pytest.raises(ConflictError):
        await service.abort_run(parent.id)
    attempt = HarnessRunRepository.get_by_id(owner.run_id)
    assistant.refresh_from_db()
    assert attempt.status == status
    assert attempt.owner_token == owner.owner_token
    assert assistant.completed_at is None
    assert not accessor.desktop_calls
    assert not service._abort_requested


@pytest.mark.parametrize("engine_id", ["claude", "codex"])
async def test_root_abort_retries_pending_child_cleanup(
    harness_workspace: Any, engine_id: str
) -> None:
    """Unknown cleanup remains fenced and a later stop retries the same child."""
    parent, child, assistant, owner = detached_child(harness_workspace, engine_id)
    assert HarnessRunRepository.begin_close_if_owner(owner.run_id, owner.owner_token)
    accessor = FakeAccessor(
        str(child.workspace_id), desktop_result={"ok": False, "lease_state": "closing"}
    )
    service = HarnessService(
        run_service=HarnessRunService(), accessor_factory=lambda _: accessor
    )
    with pytest.raises(
        ConflictError, match="Previous engine process cleanup is pending"
    ):
        await service.abort_run(parent.id)
    child.refresh_from_db()
    assistant.refresh_from_db()
    assert child.status == "busy"
    assert assistant.completed_at is None
    assert HarnessRunRepository.get_by_id(owner.run_id).status == "closing"
    accessor.desktop_result = {"ok": True, "lease_state": "released"}
    await service.abort_run(parent.id)
    child.refresh_from_db()
    assert child.status == "idle"
    assert HarnessRunRepository.get_open_for_session(child.id) is None
