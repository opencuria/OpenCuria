"""Characterize provider snapshot authorization and its atomic admission fence."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from apps.harness.models import HarnessMessage, ProviderConnection, ProviderTurn
from apps.harness.provider_turn_repository import ProviderTurnRepository
from apps.harness.provider_turns import ProviderTurnService
from apps.harness.repositories import HarnessSessionRepository
from apps.organizations.models import Membership


@pytest.fixture
def admission(harness_workspace: Any) -> dict[str, Any]:
    """Supply an assistant shell, active member and personal provider account."""
    member = harness_workspace.created_by
    organization_id = harness_workspace.runner.organization_id
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id, organization_id=organization_id
    )
    assistant = HarnessMessage.objects.create(
        session=session, role="assistant", position=0
    )
    account = ProviderConnection.objects.create(
        organization_id=organization_id, user=member, provider="openrouter"
    )
    return {
        "assistant_message_id": assistant.id,
        "organization_id": organization_id,
        "user_id": member.id,
        "account_ids": {"openrouter": str(account.id)},
    }


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["personal", "shared", "no_user", "empty"])
def test_admission_persists_exact_snapshot_and_one_holder(
    admission: dict[str, Any], scope: str
) -> None:
    """Snapshot IDs remain secret-free and unnormalized, including absent users."""
    if scope == "shared":
        ProviderConnection.objects.update(user_id=None)
    elif scope == "no_user":
        admission["user_id"] = None
    elif scope == "empty":
        admission["account_ids"] = {}
    # Validation accepts UUID spelling variants without rewriting the snapshot.
    admission["account_ids"] = {
        provider: value.upper() for provider, value in admission["account_ids"].items()
    }
    turn = ProviderTurnService().admit(**admission)
    turn.refresh_from_db()
    assert turn.account_ids == admission["account_ids"]
    assert turn.organization_id == admission["organization_id"]
    assert turn.initiated_by_id == admission["user_id"]
    assert turn.assistant_message_id == admission["assistant_message_id"]
    assert turn.active and not turn.cancel_requested
    assert turn.holds == len(turn.holder_tokens) == 1
    assert str(uuid.UUID(turn.holder_tokens[0])) == turn.holder_tokens[0]
    assert turn.used_account_ids == turn.dependency_account_ids == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("invalid", "message"),
    [
        (
            "membership",
            "The initiating user no longer has active organization membership",
        ),
        (
            "inactive",
            "The initiating user no longer has active organization membership",
        ),
        ("duplicate", "A provider snapshot contains duplicate accounts"),
        ("missing", "A selected provider account no longer exists"),
        ("revoked", "A selected provider account was revoked"),
        ("organization", "A selected provider account is out of scope"),
        ("provider", "A selected provider account changed provider"),
        ("owner", "A selected provider account is no longer visible"),
        ("malformed", "badly formed hexadecimal UUID string"),
    ],
)
def test_admission_rejects_stale_or_invalid_snapshots_without_writes(
    admission: dict[str, Any], invalid: str, message: str
) -> None:
    """Each authorization failure keeps its error and leaves no turn sidecar."""
    account_id = admission["account_ids"]["openrouter"]
    if invalid == "membership":
        Membership.objects.filter(user_id=admission["user_id"]).delete()
    elif invalid == "inactive":
        get_user_model().objects.filter(pk=admission["user_id"]).update(is_active=False)
    elif invalid == "duplicate":
        admission["account_ids"]["chatgpt"] = account_id.upper()
    elif invalid == "missing":
        admission["account_ids"]["openrouter"] = str(uuid.uuid4())
    elif invalid == "revoked":
        ProviderConnection.objects.update(deleted_at=timezone.now())
    elif invalid == "organization":
        admission["organization_id"] = uuid.uuid4()
        admission["user_id"] = None  # Isolate account scope from membership.
    elif invalid == "provider":
        ProviderConnection.objects.update(provider="chatgpt")
    elif invalid == "owner":
        other = get_user_model().objects.create_user(email="other-turn@example.test")
        ProviderConnection.objects.update(user=other)
    elif invalid == "malformed":
        admission["account_ids"]["openrouter"] = "invalid"
    with pytest.raises(ValueError) as error:
        ProviderTurnService().admit(**admission)
    assert str(error.value) == message
    assert not ProviderTurn.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["capture", "admit"])
def test_snapshot_reads_and_writes_hold_the_revocation_fence(
    admission: dict[str, Any], monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Acquire admission's mutex before account/membership reads until commit."""
    fenced = []
    reads = []
    lock = ProviderTurnRepository.lock_mutex

    def fence() -> None:
        assert connection.in_atomic_block
        lock()
        fenced.append(True)

    def observe(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        if any(
            table in sql
            for table in (Membership._meta.db_table, ProviderConnection._meta.db_table)
        ):
            assert connection.in_atomic_block
            assert fenced
            reads.append(sql)
        if sql.startswith(f'INSERT INTO "{ProviderTurn._meta.db_table}"'):
            assert connection.in_atomic_block
            assert fenced
        return execute(sql, params, many, context)

    monkeypatch.setattr(ProviderTurnRepository, "lock_mutex", fence)
    service = ProviderTurnService()
    assert not connection.in_atomic_block
    with connection.execute_wrapper(observe):
        if operation == "admit":
            service.admit(**admission)
        else:
            assert (
                service.capture_account_ids(
                    admission["organization_id"], admission["user_id"]
                )
                == admission["account_ids"]
            )
    assert reads
    assert not connection.in_atomic_block


@pytest.mark.django_db(transaction=True)
def test_admission_rolls_back_a_failure_after_snapshot_creation(
    admission: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write cannot leave an active turn holding provider accounts."""
    create = ProviderTurn.objects.create

    def fail_after_write(**values: Any) -> ProviderTurn:
        turn = create(**values)
        assert connection.in_atomic_block
        assert ProviderTurn.objects.filter(pk=turn.pk).exists()
        raise RuntimeError("snapshot write failed")

    monkeypatch.setattr(ProviderTurn.objects, "create", fail_after_write)
    with pytest.raises(RuntimeError, match="snapshot write failed"):
        ProviderTurnService().admit(**admission)
    assert not ProviderTurn.objects.exists()


def test_capture_without_a_user_does_not_touch_persistence() -> None:
    """Anonymous/internal callers retain the empty-snapshot fast path."""
    assert ProviderTurnService().capture_account_ids(uuid.uuid4(), None) == {}
