"""ORM operations for mobile destinations and outbox claims."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from common.exceptions import NotFoundError

from .models import MobileDevice, PushDelivery


class NotificationRepository:
    """Persist device ownership and support recoverable event delivery."""

    @staticmethod
    def has_devices() -> bool:
        """Keep unused mobile delivery off the harness's database hot path."""
        return MobileDevice.objects.filter(
            Q(notifications_enabled=True) | Q(live_activities_enabled=True)
        ).exists()

    @staticmethod
    def current_device(device_id: uuid.UUID) -> MobileDevice | None:
        """Read preferences and destinations again after claiming queued work."""
        return MobileDevice.objects.select_related("user").filter(id=device_id).first()

    @staticmethod
    def device_exists(device_id: uuid.UUID) -> bool:
        """Recheck that a loaded registration has not been removed."""
        return MobileDevice.objects.filter(id=device_id).exists()

    @staticmethod
    def device(device_id: uuid.UUID, user_id: int, org_id: uuid.UUID) -> MobileDevice:
        """Hide destinations belonging to other accounts or organizations."""
        device = MobileDevice.objects.filter(
            id=device_id, user_id=user_id, organization_id=org_id
        ).first()
        if device is None:
            raise NotFoundError("Mobile device")
        return device

    @staticmethod
    @transaction.atomic
    def register(
        device_id: uuid.UUID, user_id: int, org_id: uuid.UUID, **fields: Any
    ) -> MobileDevice:
        """Serialize registration and retire a token's previous account binding."""
        existing = MobileDevice.objects.select_for_update().filter(id=device_id).first()
        if existing and (
            existing.user_id != user_id or existing.organization_id != org_id
        ):
            raise NotFoundError("Mobile device")
        token_hash = fields.get("push_token_hash")
        if token_hash:
            previous = MobileDevice.objects.filter(
                push_token_hash=token_hash,
                platform=fields["platform"],
                app_id=fields["app_id"],
            ).exclude(id=device_id)
            PushDelivery.objects.filter(device__in=previous).delete()
            previous.delete()
        if existing and not fields.get("live_activities_enabled"):
            fields.update(
                activity_id="", encrypted_activity_token="", activity_updated_at=None
            )
        fields["notifications_enabled_at"] = (
            (
                existing.notifications_enabled_at
                if existing and existing.notifications_enabled
                else timezone.now()
            )
            if fields.get("notifications_enabled")
            else None
        )
        device, _ = MobileDevice.objects.update_or_create(
            id=device_id,
            defaults={
                "user_id": user_id,
                "organization_id": org_id,
                **fields,
                "last_seen_at": timezone.now(),
            },
        )
        if not device.notifications_enabled:
            PushDelivery.objects.filter(
                device=device, kind="alert", status="pending"
            ).delete()
        return device

    @staticmethod
    def update_activity(
        device: MobileDevice, activity_id: str, encrypted_token: str
    ) -> None:
        """Replace an activity token atomically; never keep old destinations."""
        MobileDevice.objects.filter(id=device.id).update(
            activity_id=activity_id,
            encrypted_activity_token=encrypted_token,
            activity_updated_at=timezone.now(),
            last_seen_at=timezone.now(),
        )

    @staticmethod
    def remove(device: MobileDevice) -> None:
        """Delete encrypted destinations and all queued deliveries on sign-out."""
        device.delete()

    @staticmethod
    def clear_activity(device_id: uuid.UUID, encrypted_token: str) -> None:
        """A stale provider response cannot remove a newly rotated token."""
        MobileDevice.objects.filter(
            id=device_id, encrypted_activity_token=encrypted_token
        ).update(activity_id="", encrypted_activity_token="", activity_updated_at=None)

    @staticmethod
    def clear_push_token(device_id: uuid.UUID, encrypted_token: str) -> None:
        """Invalidate only the destination rejected by the provider."""
        MobileDevice.objects.filter(
            id=device_id, encrypted_push_token=encrypted_token
        ).update(
            encrypted_push_token="", push_token_hash="", notifications_enabled=False
        )

    @staticmethod
    def devices_for_owner(
        org_id: uuid.UUID, user_id: int, *, seen_since: datetime
    ) -> list[MobileDevice]:
        """Read the owner's registrations within the supplied freshness window."""
        return list(
            MobileDevice.objects.filter(
                organization_id=org_id,
                user_id=user_id,
                last_seen_at__gte=seen_since,
            )
        )

    @staticmethod
    def enqueue(device: MobileDevice, event_key: str, kind: str, payload: dict) -> None:
        """Use a database uniqueness constraint for repeat-safe event receivers."""
        PushDelivery.objects.get_or_create(
            device=device, event_key=event_key, kind=kind, defaults={"payload": payload}
        )

    @staticmethod
    @transaction.atomic
    def claim() -> PushDelivery | None:
        """Claim one due job with a recoverable lease and per-device ordering."""
        now = timezone.now()
        leased_devices = PushDelivery.objects.filter(
            locked_until__gt=now, status="pending"
        ).values("device_id")
        job = (
            PushDelivery.objects.filter(status="pending", available_at__lte=now)
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .exclude(device_id__in=leased_devices)
            .order_by("created_at", "id")
            .first()
        )
        if job is None:
            return None
        token = uuid.uuid4()
        claimed = (
            PushDelivery.objects.filter(
                id=job.id, status="pending", available_at__lte=now
            )
            .filter(Q(locked_until__isnull=True) | Q(locked_until__lte=now))
            .update(
                locked_until=now + timedelta(seconds=60),
                claim_token=token,
                attempts=F("attempts") + 1,
            )
        )
        if not claimed:
            return None
        return PushDelivery.objects.select_related("device__user").get(id=job.id)

    @staticmethod
    def settle(job: PushDelivery, status: str, code: str = "", delay: int = 0) -> None:
        """Fence acknowledgements from expired workers after a restart."""
        PushDelivery.objects.filter(id=job.id, claim_token=job.claim_token).update(
            status=status,
            error_code=code[:64],
            locked_until=None,
            claim_token=None,
            available_at=timezone.now() + timedelta(seconds=delay),
        )

    @staticmethod
    def activity_devices(*, seen_since: datetime) -> list[MobileDevice]:
        """Read iOS activity destinations within the supplied freshness window."""
        return list(
            MobileDevice.objects.filter(
                live_activities_enabled=True,
                platform="ios",
                last_seen_at__gte=seen_since,
            ).exclude(encrypted_activity_token="")
        )

    @staticmethod
    def prune(*, deliveries_before: datetime, devices_before: datetime) -> None:
        """Remove outbox rows and registrations older than the supplied cutoffs."""
        PushDelivery.objects.filter(created_at__lt=deliveries_before).delete()
        MobileDevice.objects.filter(last_seen_at__lt=devices_before).delete()
