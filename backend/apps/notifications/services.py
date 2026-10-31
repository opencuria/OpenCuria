"""Native notification policy and recoverable delivery."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta

import httpx
import structlog
from asgiref.sync import sync_to_async
from django.utils import timezone

from apps.accounts.models import User
from apps.harness.conversation_activity_service import (
    ConversationActivity,
    ConversationActivityService,
)
from apps.organizations.services import OrganizationService
from common.exceptions import AuthenticationError, NotFoundError
from common.utils import decrypt_value, encrypt_value, hash_token

from .configuration import get_push_settings
from .models import MobileDevice, PushDelivery
from .providers import DeliveryResult, PushProviders
from .repositories import NotificationRepository
from .schemas import ActivityRegistrationIn, DeviceRegistrationIn

log = structlog.get_logger(__name__)
REGISTRATION_MAX_AGE = timedelta(days=30)
OUTBOX_RETENTION = timedelta(days=2)
ALERT_LABELS = {
    "completed": "Agent finished",
    "error": "Agent run failed",
    "waiting_for_approval": "Approval needed",
    "waiting_for_input": "Agent needs your input",
}


def activity_row(session: ConversationActivity, phase: str) -> dict:
    """Use bounded conversation metadata; never include prompts or tool output."""

    def title(value: str, limit: int, fallback: str) -> str:
        clean = " ".join(
            "".join(
                char for char in value if char.isprintable() or char.isspace()
            ).split()
        )
        return clean[:limit] or fallback

    return {
        "sessionId": str(session.id),
        "workspaceId": str(session.workspace_id),
        "title": title(session.title, 100, "Conversation"),
        "workspaceName": title(session.workspace_name, 80, "Workspace"),
        "phase": phase,
    }


class NotificationService:
    """One policy shared by API, lifecycle receivers and the ASGI outbox worker."""

    def __init__(
        self,
        *,
        repository: type[NotificationRepository] | None = None,
        activity: ConversationActivityService | None = None,
        organizations: OrganizationService | None = None,
        providers: PushProviders | None = None,
    ) -> None:
        self.repo = repository or NotificationRepository
        self.activity = activity or ConversationActivityService()
        self.organizations = organizations or OrganizationService()
        self.providers = providers
        self.push_settings = get_push_settings()

    def capabilities(self, user: User, org_id: uuid.UUID) -> dict[str, bool]:
        """Return server-wide availability to authenticated organization members."""
        self.organizations.require_membership(user, org_id)
        return self.push_settings.capabilities()

    def register(
        self,
        user: User,
        org_id: uuid.UUID,
        device_id: uuid.UUID,
        data: DeviceRegistrationIn,
    ) -> MobileDevice:
        """Save opt-in without granting access to another user's runs."""
        capabilities = self.capabilities(user, org_id)
        if data.notifications_enabled and not capabilities[data.platform]:
            raise ValueError("Push notifications are not configured for this platform")
        if data.live_activities_enabled and (
            data.platform != "ios" or not capabilities["live_activities"]
        ):
            raise ValueError("Live Activities are not configured for this platform")
        if data.platform == "ios" and data.push_token:
            try:
                bytes.fromhex(data.push_token)
            except ValueError:
                raise ValueError("Invalid APNs device token") from None
        if data.notifications_enabled and not data.push_token:
            raise ValueError("A push token is required to enable notifications")
        fields = data.model_dump(exclude={"push_token"})
        fields.update(
            encrypted_push_token=encrypt_value(data.push_token)
            if data.push_token
            else "",
            push_token_hash=hash_token(data.push_token) if data.push_token else "",
        )
        return self.repo.register(device_id, user.id, org_id, **fields)

    def register_activity(
        self,
        user: User,
        org_id: uuid.UUID,
        device_id: uuid.UUID,
        data: ActivityRegistrationIn,
    ) -> None:
        """Bind and immediately reconcile each ActivityKit token replacement."""
        self.organizations.require_membership(user, org_id)
        device = self.repo.device(device_id, user.id, org_id)
        if device.platform != "ios" or not device.live_activities_enabled:
            raise ValueError("Live Activities are disabled for this device")
        self.repo.update_activity(
            device, data.activity_id, encrypt_value(data.push_token)
        )
        self.repo.enqueue(
            device,
            f"token:{uuid.uuid4()}",
            "activity",
            {"organization_id": str(org_id)},
        )

    def unregister(self, user: User, org_id: uuid.UUID, device_id: uuid.UUID) -> None:
        """Revoke this installation, even after organization membership removal."""
        # Device ownership suffices for deletion; membership is intentionally not
        # required so a removed member can still revoke their own installation.
        try:
            device = self.repo.device(device_id, user.id, org_id)
        except NotFoundError:
            return
        self.repo.remove(device)

    def snapshot(self, user: User, org_id: uuid.UUID, device_id: uuid.UUID) -> dict:
        """Hydrate foreground activities from durable state."""
        self.organizations.require_membership(user, org_id)
        device = self.repo.device(device_id, user.id, org_id)
        return self._snapshot(device)

    def record_finished(self, message_id: uuid.UUID) -> None:
        """Completion signals and restart repair share the same idempotency key."""
        if not self.repo.has_devices():
            return
        source = self.activity.message_source(message_id)
        if source is None:
            return
        session, message = source
        if message.completed_at is None:
            return
        self._enqueue(
            session,
            f"finished:{message.id}",
            message.phase,
            message_id=str(message.id),
            occurred_at=message.completed_at,
        )

    def record_activity(self, event: str, data: dict) -> None:
        """Consume low-frequency harness status/gate events, including child gates."""
        if not self.repo.has_devices():
            return
        session_id = data.get("root_session_id") or data.get("session_id")
        if not session_id:
            return
        try:
            root_id = self.activity.root_id(uuid.UUID(str(session_id)))
        except (ValueError, TypeError):
            return
        if root_id is None:
            return
        source = self.activity.source(root_id)
        if source is None:
            return
        session, message = source
        if event == "harness.session_status":
            if data.get("status") != "busy":
                if message:
                    self.record_finished(message.id)
                return
            phase = "running"
            key = f"running:{message.id if message else session.id}"
        else:
            if not data.get("request_id"):
                return
            resolved = "decision" in data or data.get("status", "pending") != "pending"
            phase = (
                "running"
                if resolved
                else "waiting_for_approval"
                if event == "harness.permission_required"
                else "waiting_for_input"
            )
            key = f"gate:{data['request_id']}:{phase}"
        self._enqueue(session, key, phase, request_id=data.get("request_id", ""))

    async def repair(self) -> None:
        """Recover missed lifecycle events and enforce notification retention."""
        for message_id in await sync_to_async(self.activity.recent_completions)():
            await sync_to_async(self.record_finished)(message_id)
        now = timezone.now()
        await sync_to_async(self.repo.prune)(
            deliveries_before=now - OUTBOX_RETENTION,
            devices_before=self._registration_cutoff(now),
        )
        for event, data in await sync_to_async(self.activity.recent_attention_events)():
            await sync_to_async(self.record_activity)(event, data)

    async def record_heartbeat(self) -> None:
        """Refresh recent iOS activities with repeat-safe five-minute time keys."""
        for device in await sync_to_async(self.repo.activity_devices)(
            seen_since=self._registration_cutoff()
        ):
            await sync_to_async(self.repo.enqueue)(
                device,
                f"heartbeat:{int(time.time()) // 300}",
                "activity",
                {"organization_id": str(device.organization_id)},
            )

    @staticmethod
    def _registration_cutoff(now: datetime | None = None) -> datetime:
        """Share the registration lifetime across enqueue, delivery and cleanup."""
        return (now if now is not None else timezone.now()) - REGISTRATION_MAX_AGE

    def _is_authorized(self, device: MobileDevice) -> bool:
        """Require a live registration and current active organization membership."""
        return (
            self.repo.device_exists(device.id)
            and device.last_seen_at >= self._registration_cutoff()
            and self.organizations.get_active_membership(
                device.user_id, device.organization_id
            )
            is not None
        )

    def _enqueue(
        self,
        session: ConversationActivity,
        event_key: str,
        phase: str,
        *,
        message_id: str = "",
        request_id: str = "",
        occurred_at: datetime | None = None,
    ) -> None:
        payload = {
            "organization_id": str(session.organization_id),
            "session_id": str(session.id),
            "workspace_id": str(session.workspace_id),
            "phase": phase,
            "message_id": message_id,
            "request_id": request_id,
            "row": activity_row(session, phase),
        }
        owner_id = session.workspace_owner_id
        if (
            owner_id is None
            or self.organizations.get_active_membership(
                owner_id, session.organization_id
            )
            is None
        ):
            return
        for device in self.repo.devices_for_owner(
            session.organization_id,
            owner_id,
            seen_since=self._registration_cutoff(),
        ):
            # Do not replay old completions to newly registered installations.
            if occurred_at and (
                device.created_at > occurred_at
                or device.notifications_enabled_at is not None
                and device.notifications_enabled_at > occurred_at
            ):
                continue
            if device.notifications_enabled and phase in ALERT_LABELS:
                self.repo.enqueue(device, event_key, "alert", payload)
            if device.live_activities_enabled and device.encrypted_activity_token:
                self.repo.enqueue(device, event_key, "activity", payload)

    def _snapshot(self, device: MobileDevice, fallback: dict | None = None) -> dict:
        rows = self.activity.active_rows(device.organization_id, device.user_id)
        priority = {"waiting_for_approval": 0, "waiting_for_input": 1, "running": 2}
        rows.sort(key=lambda item: priority.get(item[1], 2))
        activities = [activity_row(session, phase) for session, phase in rows[:3]]
        if not activities and fallback:
            activities = [fallback]
        return {
            "organizationId": str(device.organization_id),
            "deviceId": str(device.id),
            "activeCount": len(rows),
            "activities": activities,
            "updatedAt": timezone.now().isoformat(),
        }

    async def deliver(self, job: PushDelivery) -> DeliveryResult:
        """Revalidate authorization and preferences immediately before provider I/O."""
        device = await sync_to_async(self.repo.current_device)(job.device_id)
        if device is None:
            return DeliveryResult("failed", "registration_revoked")
        if not await sync_to_async(self._is_authorized)(device):
            return DeliveryResult("failed", "registration_expired")
        if str(device.organization_id) != job.payload.get("organization_id"):
            return DeliveryResult("failed", "organization_changed")
        source = None
        if job.payload.get("session_id"):
            source = await sync_to_async(self.activity.source)(
                uuid.UUID(job.payload["session_id"])
            )
            if (
                source is None
                or source[0].workspace_owner_id != device.user_id
                or source[0].organization_id != device.organization_id
            ):
                return DeliveryResult("failed", "conversation_unavailable")
        if not self.push_settings.capabilities()[device.platform]:
            return DeliveryResult("failed", "provider_not_configured")
        if self.providers is None:
            raise RuntimeError("A push provider client is required for delivery")
        if job.kind == "activity":
            if (
                not device.live_activities_enabled
                or not device.encrypted_activity_token
            ):
                return DeliveryResult("failed", "activity_disabled")
            snapshot = await sync_to_async(self._snapshot)(
                device, job.payload.get("row")
            )

            now = int(timezone.now().timestamp())
            ending = snapshot["activeCount"] == 0
            aps = {
                "timestamp": now,
                "event": "end" if ending else "update",
                "content-state": {
                    "name": "OpenCuriaAgentActivity",
                    "props": json.dumps(
                        snapshot, ensure_ascii=False, separators=(",", ":")
                    ),
                },
                "stale-date": now + 10 * 60,
            }
            if ending:
                aps["dismissal-date"] = now
            result = await self.providers.send_apns(
                token=decrypt_value(device.encrypted_activity_token),
                app_id=device.app_id,
                environment=device.apns_environment,
                payload={"aps": aps},
                collapse_id="opencuria-agent-activity",
                live_activity=True,
            )
            if result.status == "invalid" or ending and result.status == "sent":
                await sync_to_async(self.repo.clear_activity)(
                    device.id, device.encrypted_activity_token
                )
            return result
        if not device.notifications_enabled or not device.encrypted_push_token:
            return DeliveryResult("failed", "notifications_disabled")
        phase = job.payload["phase"]
        # Suppress a gate that was answered on another device before delivery.
        if phase in {"waiting_for_approval", "waiting_for_input"}:
            if source is None or source[0].status != "busy":
                return DeliveryResult("failed", "attention_resolved")
            if not await sync_to_async(self.activity.pending_gate)(
                job.payload["request_id"],
                phase,
                device.organization_id,
                job.payload["workspace_id"],
            ):
                return DeliveryResult("failed", "attention_resolved")
        title = ALERT_LABELS[phase]
        row = job.payload["row"]
        body = f"{row['title']} · {row['workspaceName']}"
        data = {
            "type": "opencuria.agent-activity",
            "deviceId": str(device.id),
            "organizationId": str(device.organization_id),
            "workspaceId": job.payload["workspace_id"],
            "sessionId": job.payload["session_id"],
            "phase": phase,
        }
        collapse_id = hashlib.sha256(job.event_key.encode()).hexdigest()
        token = decrypt_value(device.encrypted_push_token)
        if device.platform == "ios":
            result = await self.providers.send_apns(
                token=token,
                app_id=device.app_id,
                environment=device.apns_environment,
                collapse_id=collapse_id,
                payload={
                    "aps": {
                        "alert": {"title": title, "body": body},
                        "sound": "default",
                        "thread-id": job.payload["session_id"],
                    },
                    # Expo iOS reads custom remote-notification data from body.
                    "body": data,
                },
            )
        else:
            result = await self.providers.send_fcm(
                token=token,
                title=title,
                body=body,
                data=data,
                collapse_id=collapse_id,
                app_id=device.app_id,
            )
        if result.status == "invalid":
            await sync_to_async(self.repo.clear_push_token)(
                device.id, device.encrypted_push_token
            )
        return result

    async def process_one(self) -> bool:
        """Attempt one leased delivery with bounded retries and bounded age."""
        job = await sync_to_async(self.repo.claim)()
        if job is None:
            return False
        if job.created_at < timezone.now() - timedelta(minutes=15):
            await sync_to_async(self.repo.settle)(job, "expired", "delivery_expired")
            return True
        try:
            result = await self.deliver(job)
        except httpx.TransportError:
            result = DeliveryResult("retry", "provider_unreachable")
        except (ValueError, AuthenticationError):
            result = DeliveryResult("failed", "invalid_provider_configuration")
        except Exception:
            # Provider/JSON failures must not leak token-bearing request URLs,
            # signing assertions or provider configuration into logs.
            log.error("mobile_push_delivery_failed", delivery_id=str(job.id))
            result = DeliveryResult("retry", "delivery_failed")
        retry = result.status == "retry" and job.attempts < 6
        await sync_to_async(self.repo.settle)(
            job,
            "pending" if retry else "sent" if result.status == "sent" else "failed",
            result.code,
            min(120, 5 * 2 ** (job.attempts - 1)) if retry else 0,
        )
        return True
