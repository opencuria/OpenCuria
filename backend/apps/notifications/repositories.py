"""All ORM operations for mobile destinations, snapshots and outbox claims."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.harness.models import HarnessMessage, HarnessSession, QuestionRequest
from apps.harness.permissions.models import PermissionRequest
from apps.organizations.models import Membership
from common.exceptions import NotFoundError

from .models import MobileDevice, PushConfiguration, PushDelivery


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
    def configuration(org_id: uuid.UUID) -> PushConfiguration | None:
        """Load provider credential references, including scope for revalidation."""
        return (
            PushConfiguration.objects.select_related(
                "apns_credential__service", "fcm_credential__service"
            )
            .filter(organization_id=org_id)
            .first()
        )

    @staticmethod
    def save_configuration(org_id: uuid.UUID, **fields: Any) -> PushConfiguration:
        """Replace only provider references; vault contents remain separate."""
        return PushConfiguration.objects.update_or_create(
            organization_id=org_id, defaults=fields
        )[0]

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
    def source(
        session_id: uuid.UUID,
    ) -> tuple[HarnessSession, HarnessMessage | None] | None:
        """Return a root conversation and its most recent assistant turn."""
        session = (
            HarnessSession.objects.select_related("workspace")
            .filter(
                id=session_id, workspace__runner__organization_id=F("organization_id")
            )
            .first()
        )
        if session is None or session.parent_id is not None:
            return None
        message = (
            HarnessMessage.objects.filter(session=session, role="assistant")
            .order_by("-position")
            .first()
        )
        return session, message

    @staticmethod
    def message_source(
        message_id: uuid.UUID,
    ) -> tuple[HarnessSession, HarnessMessage] | None:
        """Resolve completion signals without exposing child-agent completions."""
        message = (
            HarnessMessage.objects.select_related("session__workspace")
            .filter(
                id=message_id,
                role="assistant",
                session__parent__isnull=True,
                session__workspace__runner__organization_id=F(
                    "session__organization_id"
                ),
            )
            .first()
        )
        return (message.session, message) if message else None

    @staticmethod
    def root_id(session_id: uuid.UUID) -> uuid.UUID | None:
        """Resolve child gates to their owning conversation without a depth cap."""
        seen: set[uuid.UUID] = set()
        while session_id not in seen:
            seen.add(session_id)
            row = (
                HarnessSession.objects.filter(id=session_id)
                .values("id", "parent_id")
                .first()
            )
            if row is None:
                return None
            if row["parent_id"] is None:
                return row["id"]
            session_id = row["parent_id"]
        return None

    @staticmethod
    def devices_for_session(session: HarnessSession) -> list[MobileDevice]:
        """Match REST's workspace-owner boundary and current organization membership."""
        return list(
            MobileDevice.objects.filter(
                organization_id=session.organization_id,
                user_id=session.workspace.created_by_id,
                user__is_active=True,
                last_seen_at__gte=timezone.now() - timedelta(days=30),
                user__memberships__organization_id=session.organization_id,
            ).distinct()
        )

    @staticmethod
    def is_authorized(device: MobileDevice) -> bool:
        """Recheck revocation, membership and registration age at delivery time."""
        return (
            MobileDevice.objects.filter(id=device.id, user__is_active=True).exists()
            and device.last_seen_at >= timezone.now() - timedelta(days=30)
            and Membership.objects.filter(
                user_id=device.user_id, organization_id=device.organization_id
            ).exists()
        )

    @staticmethod
    def pending_gate(
        request_id: str, phase: str, org_id: uuid.UUID, workspace_id: str
    ) -> bool:
        """Validate the exact gate, even when more than three runs are active."""
        model = (
            PermissionRequest if phase == "waiting_for_approval" else QuestionRequest
        )
        try:
            return model.objects.filter(
                id=uuid.UUID(request_id),
                organization_id=org_id,
                workspace_id=uuid.UUID(workspace_id),
                status="pending",
            ).exists()
        except (ValueError, TypeError):
            return False

    @staticmethod
    def active_rows(device: MobileDevice) -> list[tuple[HarnessSession, str]]:
        """Derive status from durable sessions and outstanding gates."""
        sessions = list(
            HarnessSession.objects.select_related("workspace")
            .filter(
                organization_id=device.organization_id,
                workspace__runner__organization_id=device.organization_id,
                workspace__created_by_id=device.user_id,
                parent__isnull=True,
                status="busy",
            )
            .order_by("-updated_at")
        )
        if not sessions:
            return []
        # Several root conversations can run in one workspace. Attribute child
        # gates to their actual root so the card opens the conversation in need.
        busy_ids = HarnessSession.objects.filter(
            organization_id=device.organization_id,
            workspace_id__in=[s.workspace_id for s in sessions],
            status="busy",
        ).values_list("id", flat=True)

        def pending_roots(model: type[PermissionRequest] | type[QuestionRequest]):
            return {
                NotificationRepository.root_id(session_id)
                for session_id in model.objects.filter(
                    organization_id=device.organization_id,
                    session_id__in=busy_ids,
                    status="pending",
                )
                .values_list("session_id", flat=True)
                .distinct()
            }

        permission_roots = pending_roots(PermissionRequest)
        question_roots = pending_roots(QuestionRequest)
        result = []
        for session in sessions:
            latest = (
                HarnessMessage.objects.filter(session=session, role="assistant")
                .order_by("-position")
                .first()
            )
            if latest and latest.completed_at is not None:
                continue
            phase = "running"
            if session.id in permission_roots:
                phase = "waiting_for_approval"
            elif session.id in question_roots:
                phase = "waiting_for_input"
            result.append((session, phase))
        return result

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
    def recent_completions() -> list[uuid.UUID]:
        """Repair missed completion signals after an interrupted server process."""
        return list(
            HarnessMessage.objects.filter(
                role="assistant",
                session__parent__isnull=True,
                completed_at__gte=timezone.now() - timedelta(minutes=15),
            ).values_list("id", flat=True)
        )

    @staticmethod
    def recent_attention_events() -> list[tuple[str, dict[str, str]]]:
        """Repair fresh unanswered gates if their original signal was missed."""
        busy = HarnessSession.objects.filter(status="busy").values("id")
        result = []
        for model, event in (
            (PermissionRequest, "harness.permission_required"),
            (QuestionRequest, "harness.question_required"),
        ):
            for request in model.objects.filter(
                status="pending",
                session_id__in=busy,
                created_at__gte=timezone.now() - timedelta(minutes=15),
            ).values("id", "session_id"):
                result.append(
                    (
                        event,
                        {
                            "request_id": str(request["id"]),
                            "session_id": str(request["session_id"]),
                        },
                    )
                )
        return result

    @staticmethod
    def activity_devices() -> list[MobileDevice]:
        """Refresh long-running activities without depending on token streaming."""
        return list(
            MobileDevice.objects.filter(
                live_activities_enabled=True,
                platform="ios",
                last_seen_at__gte=timezone.now() - timedelta(days=30),
            ).exclude(encrypted_activity_token="")
        )

    @staticmethod
    def prune() -> None:
        """Bound destination and outbox retention; stale registrations stop delivery."""
        now = timezone.now()
        PushDelivery.objects.filter(created_at__lt=now - timedelta(days=2)).delete()
        MobileDevice.objects.filter(last_seen_at__lt=now - timedelta(days=30)).delete()
