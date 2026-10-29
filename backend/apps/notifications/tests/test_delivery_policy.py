"""Characterize notification eligibility and background maintenance contracts."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from django.utils import timezone

from apps.accounts.models import User
from apps.organizations.models import Membership, Organization

from ..models import MobileDevice, PushDelivery
from ..providers import DeliveryResult
from ..services import NotificationService
from ..worker import NotificationWorker

pytestmark = pytest.mark.django_db(transaction=True)

RECIPIENT_STATES = [
    ("active", True),
    ("inactive", False),
    ("membership_removed", False),
    ("other_org_membership", False),
    ("freshness_boundary", True),
    ("stale", False),
]


def change_recipient(
    user: User, device: MobileDevice, state: str, now: datetime
) -> None:
    """Change durable eligibility without updating the caller's loaded device."""
    if state == "inactive":
        User.objects.filter(id=user.id).update(is_active=False)
    elif state in {"membership_removed", "other_org_membership"}:
        Membership.objects.filter(user=user).delete()
        if state == "other_org_membership":
            other = Organization.objects.create(name="Other", slug="other")
            Membership.objects.create(user=user, organization=other)
    elif state in {"freshness_boundary", "stale"}:
        last_seen = now - timedelta(days=30)
        if state == "stale":
            last_seen -= timedelta(microseconds=1)
        MobileDevice.objects.filter(id=device.id).update(last_seen_at=last_seen)


@pytest.mark.parametrize("state,eligible", RECIPIENT_STATES)
def test_enqueue_requires_active_owner_membership_and_recent_registration(
    setup: Any,
    device: MobileDevice,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    eligible: bool,
) -> None:
    """Apply the same recipient scope to both alerts and live activities."""
    _, user, _, _, message, _ = setup
    now = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: now)
    MobileDevice.objects.filter(id=device.id).update(encrypted_activity_token="token")
    change_recipient(user, device, state, now)
    message.completed_at = now
    message.save(update_fields=["completed_at"])

    NotificationService().record_finished(message.id)

    kinds = set(PushDelivery.objects.values_list("kind", flat=True))
    assert kinds == ({"alert", "activity"} if eligible else set())


@pytest.mark.parametrize("state,eligible", RECIPIENT_STATES)
def test_delivery_rechecks_recipient_after_enqueue(
    setup: Any,
    device: MobileDevice,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    eligible: bool,
) -> None:
    """Queued work never grants delivery to a revoked or expired recipient."""
    _, user, _, _, message, _ = setup
    now = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: now)
    message.completed_at = now
    message.save(update_fields=["completed_at"])
    transport = SimpleNamespace(
        send_apns=AsyncMock(return_value=DeliveryResult("sent"))
    )
    service = NotificationService(providers=transport)
    service.record_finished(message.id)
    job = PushDelivery.objects.get(kind="alert")
    change_recipient(user, device, state, now)

    result = asyncio.run(service.deliver(job))

    assert result == (
        DeliveryResult("sent")
        if eligible
        else DeliveryResult("failed", "registration_expired")
    )
    assert transport.send_apns.call_count == int(eligible)


def test_activity_without_owner_does_not_enqueue_notifications(
    setup: Any, device: MobileDevice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An activity with no observed owner cannot select another member's devices."""
    message = setup[4]
    message.completed_at = timezone.now()
    message.save(update_fields=["completed_at"])
    service = NotificationService()
    conversation, assistant = service.activity.message_source(message.id)
    monkeypatch.setattr(
        service.activity,
        "message_source",
        lambda _: (replace(conversation, workspace_owner_id=None), assistant),
    )

    service.record_finished(message.id)

    assert not PushDelivery.objects.exists()


def test_worker_preserves_heartbeat_selection_and_retention_boundaries(
    setup: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repair expires old rows; heartbeat uses only recent iOS activity tokens."""
    org, user, *_ = setup
    now = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: now)
    monkeypatch.setattr("apps.notifications.worker.time.monotonic", lambda: 1000.0)
    monkeypatch.setattr("apps.notifications.worker.time.time", lambda: 1200.0)
    devices = {}
    for name, fields in {
        "fresh": {},
        "boundary": {"last_seen_at": now - timedelta(days=30)},
        "stale": {
            "last_seen_at": now - timedelta(days=30, microseconds=1),
        },
        "android": {"platform": "android"},
        "disabled": {"live_activities_enabled": False},
        "no_token": {"encrypted_activity_token": ""},
    }.items():
        devices[name] = MobileDevice.objects.create(
            organization=org,
            user=user,
            app_id="com.opencuria.mobile.dev",
            **{
                "platform": "ios",
                "live_activities_enabled": True,
                "encrypted_activity_token": "token",
                "last_seen_at": now,
                **fields,
            },
        )
    retained = PushDelivery.objects.create(
        device=devices["fresh"], event_key="retained", kind="activity", payload={}
    )
    expired = PushDelivery.objects.create(
        device=devices["fresh"], event_key="expired", kind="activity", payload={}
    )
    PushDelivery.objects.filter(id=retained.id).update(
        created_at=now - timedelta(days=2)
    )
    PushDelivery.objects.filter(id=expired.id).update(
        created_at=now - timedelta(days=2, microseconds=1)
    )
    service = NotificationService()
    service.process_one = AsyncMock(side_effect=asyncio.CancelledError)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(NotificationWorker(service=service)._run())

    assert not MobileDevice.objects.filter(id=devices["stale"].id).exists()
    assert MobileDevice.objects.filter(id=devices["boundary"].id).exists()
    assert not PushDelivery.objects.filter(id=expired.id).exists()
    assert PushDelivery.objects.filter(id=retained.id).exists()
    heartbeats = list(PushDelivery.objects.filter(event_key="heartbeat:4"))
    assert {job.device_id for job in heartbeats} == {
        devices["fresh"].id,
        devices["boundary"].id,
    }
    assert all(
        job.kind == "activity" and job.payload == {"organization_id": str(org.id)}
        for job in heartbeats
    )


def test_worker_schedules_only_notification_service_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ASGI worker needs no persistence or harness activity ports."""
    monkeypatch.setattr("apps.notifications.worker.time.monotonic", lambda: 1000.0)
    monkeypatch.setattr("apps.notifications.worker.time.time", lambda: 1200.0)
    monkeypatch.setattr(
        "apps.notifications.worker.asyncio.sleep",
        AsyncMock(side_effect=AssertionError("Unexpected worker error or idle wait")),
    )
    service = SimpleNamespace(
        repair=AsyncMock(),
        record_heartbeat=AsyncMock(),
        process_one=AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(NotificationWorker(service=service)._run())

    service.repair.assert_awaited_once_with()
    service.record_heartbeat.assert_awaited_once_with()
    service.process_one.assert_awaited_once_with()
