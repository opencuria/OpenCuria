"""Characterize provider account revocation wakeups across execution loops."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from django.db import transaction

from apps.harness.provider_account_events import (
    notify_account_revoked,
    register_revocation_listener,
    unregister_revocation_listener,
)
from apps.harness.provider_accounts import ProviderAccountService


async def test_revocation_from_another_thread_wakes_only_matching_listeners() -> None:
    """One account can wake several answers without waking unrelated answers."""
    account_id, other_id = uuid.uuid4(), uuid.uuid4()
    events = [asyncio.Event() for _ in range(3)]
    watched = [
        register_revocation_listener(
            [str(identifier)], loop=asyncio.get_running_loop(), event=event
        )
        for identifier, event in zip((account_id, account_id, other_id), events)
    ]
    try:
        await asyncio.to_thread(notify_account_revoked, account_id)
        await asyncio.wait_for(
            asyncio.gather(events[0].wait(), events[1].wait()), timeout=1
        )
        assert not events[2].is_set()
    finally:
        for identifiers, event in zip(watched, events):
            unregister_revocation_listener(identifiers, event=event)


async def test_unregistration_is_idempotent_and_preserves_other_listeners() -> None:
    """Cleanup removes only its answer and ignores duplicate/invalid IDs."""
    account_id = uuid.uuid4()
    removed, remaining = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    watched = register_revocation_listener(
        [str(account_id), str(account_id), "invalid", ""], loop=loop, event=removed
    )
    assert watched == {account_id}
    other = register_revocation_listener([str(account_id)], loop=loop, event=remaining)
    try:
        unregister_revocation_listener(watched, event=removed)
        unregister_revocation_listener(watched, event=removed)
        notify_account_revoked(account_id)
        await asyncio.wait_for(remaining.wait(), timeout=1)
        assert not removed.is_set()
    finally:
        unregister_revocation_listener(watched, event=removed)
        unregister_revocation_listener(other, event=remaining)


def test_notification_ignores_closed_loops() -> None:
    """A stopped execution loop cannot break a committed account mutation."""
    loop = asyncio.new_event_loop()
    event = asyncio.Event()
    account_id = uuid.uuid4()
    watched = register_revocation_listener([str(account_id)], loop=loop, event=event)
    loop.close()
    try:
        notify_account_revoked(account_id)
        assert not event.is_set()
    finally:
        unregister_revocation_listener(watched, event=event)


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", ["delete", "unshare"])
@pytest.mark.parametrize("rollback", [False, True])
def test_account_mutation_notifies_only_after_commit(
    organization: Any,
    provider_account_user: Any,
    django_capture_on_commit_callbacks: Any,
    mutation: str,
    rollback: bool,
) -> None:
    """Nested changes publish on commit, while rolled-back changes stay silent."""
    service = ProviderAccountService()
    account = service.save_account(
        organization_id=organization.id,
        user=provider_account_user,
        provider="openrouter",
        payload={"shared": True, "api_key": "test-key"},
    )
    loop = asyncio.new_event_loop()
    event = asyncio.Event()
    watched = register_revocation_listener([str(account.id)], loop=loop, event=event)
    try:
        with django_capture_on_commit_callbacks(execute=True) as callbacks:
            with transaction.atomic():
                if mutation == "delete":
                    service.delete_account(
                        organization.id, provider_account_user.id, account.id
                    )
                else:
                    service.save_account(
                        organization_id=organization.id,
                        user=provider_account_user,
                        provider="openrouter",
                        account_id=account.id,
                        payload={"shared": False},
                    )
                loop.run_until_complete(asyncio.sleep(0))
                assert not event.is_set()
                if rollback:
                    transaction.set_rollback(True)
        loop.run_until_complete(asyncio.sleep(0))
        assert len(callbacks) == (0 if rollback else 1)
        assert event.is_set() is (not rollback)
        if rollback:
            persisted = service.get_account(
                organization.id, provider_account_user.id, account.id
            )
            assert persisted.user_id is None
    finally:
        unregister_revocation_listener(watched, event=event)
        loop.close()
