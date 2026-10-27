"""Persistence operations for isolated ChatGPT OAuth flow state."""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from common.utils import decrypt_value, encrypt_value

from .provider_oauth_models import ProviderOAuthFlow


class ProviderOAuthRepository:
    """Database boundary for OAuth flow state and encrypted device codes."""

    @staticmethod
    def create(
        *,
        organization_id: uuid.UUID,
        user_id: int,
        target_account_id: uuid.UUID | None,
        label: str,
        shared: bool,
        device_auth_id: str,
        user_code: str,
        interval: int,
    ) -> ProviderOAuthFlow:
        """Persist a pending OAuth flow, encrypting device-code material."""
        from .provider_turn_repository import ProviderTurnRepository

        with transaction.atomic():
            ProviderTurnRepository.lock_mutex()
            if target_account_id is None:
                # Reserve the label with the flow, after network I/O and under
                # the account fence. Concurrent default-name logins must not
                # both reserve the same label and fail on completion.
                used = {
                    value.casefold()
                    for value in ProviderOAuthRepository.labels_in_use(
                        organization_id, user_id, shared
                    )
                }
                base_label = label
                suffix = 2
                while label.casefold() in used:
                    tail = f" {suffix}"
                    label = f"{base_label[:255 - len(tail)]}{tail}"
                    suffix += 1
            return ProviderOAuthFlow.objects.create(
                organization_id=organization_id,
                user_id=user_id,
                target_account_id=target_account_id,
                label=label,
                shared=shared,
                device_auth_id_encrypted=encrypt_value(device_auth_id),
                user_code_encrypted=encrypt_value(user_code),
                interval=interval,
            )

    @staticmethod
    def get_owned(
        flow_id: uuid.UUID | str, organization_id: uuid.UUID, user_id: int
    ) -> ProviderOAuthFlow | None:
        """Find a flow only within the exact user and organization scope."""
        return ProviderOAuthFlow.objects.filter(
            id=flow_id, organization_id=organization_id, user_id=user_id
        ).first()

    @staticmethod
    def labels_in_use(
        organization_id: uuid.UUID, user_id: int, shared: bool
    ) -> list[str]:
        """List labels reserved by live accounts and pending flows in one scope."""
        from .models import ProviderConnection

        account_rows = ProviderConnection.objects.filter(
            organization_id=organization_id,
            provider="chatgpt",
            deleted_at__isnull=True,
            user__isnull=shared,
        )
        if not shared:
            account_rows = account_rows.filter(user_id=user_id)
        account_labels = list(account_rows.values_list("label", flat=True))
        pending_flows = ProviderOAuthFlow.objects.filter(
            organization_id=organization_id,
            shared=shared,
            status__in=[
                ProviderOAuthFlow.Status.PENDING,
                ProviderOAuthFlow.Status.POLLING,
            ],
        )
        if not shared:
            pending_flows = pending_flows.filter(user_id=user_id)
        pending_labels = list(pending_flows.values_list("label", flat=True))
        return [*account_labels, *pending_labels]

    @staticmethod
    def device_codes(flow: ProviderOAuthFlow) -> tuple[str, str]:
        """Decrypt the server-only device authorization identifiers."""
        return (
            decrypt_value(flow.device_auth_id_encrypted),
            decrypt_value(flow.user_code_encrypted),
        )

    @staticmethod
    def claim(flow_id: uuid.UUID, organization_id: uuid.UUID, user_id: int):
        """Atomically claim a pending flow using a conditional DB update."""
        now = timezone.now()
        cutoff = now - timedelta(seconds=600)
        expired = ProviderOAuthFlow.objects.filter(
            id=flow_id,
            organization_id=organization_id,
            user_id=user_id,
            status__in=[
                ProviderOAuthFlow.Status.PENDING,
                ProviderOAuthFlow.Status.POLLING,
            ],
            created_at__lt=cutoff,
        ).update(status=ProviderOAuthFlow.Status.EXPIRED, updated_at=now)
        if expired:
            return ProviderOAuthFlow.objects.get(id=flow_id), "expired"
        # Recover only this expired polling lease, never another flow/tab.
        ProviderOAuthFlow.objects.filter(
            id=flow_id,
            organization_id=organization_id,
            user_id=user_id,
            status=ProviderOAuthFlow.Status.POLLING,
            updated_at__lt=now - timedelta(seconds=120),
        ).update(status=ProviderOAuthFlow.Status.PENDING, poll_token=None)
        poll_token = uuid.uuid4()
        claimed = ProviderOAuthFlow.objects.filter(
            id=flow_id,
            organization_id=organization_id,
            user_id=user_id,
            status=ProviderOAuthFlow.Status.PENDING,
            created_at__gte=cutoff,
        ).update(
            status=ProviderOAuthFlow.Status.POLLING,
            updated_at=now,
            poll_token=poll_token,
        )
        flow = ProviderOAuthFlow.objects.filter(
            id=flow_id, organization_id=organization_id, user_id=user_id
        ).first()
        if claimed and flow is not None:
            return flow, "claimed"
        if flow is None:
            return None, "no_flow"
        return flow, flow.status

    @staticmethod
    def release_to_pending(flow_id: uuid.UUID, poll_token: uuid.UUID) -> None:
        """Make an unfinished flow available for a later poll."""
        ProviderOAuthFlow.objects.filter(
            id=flow_id, poll_token=poll_token, status=ProviderOAuthFlow.Status.POLLING
        ).update(
            status=ProviderOAuthFlow.Status.PENDING,
            poll_token=None,
            updated_at=timezone.now(),
        )

    @staticmethod
    def set_terminal(flow_id: uuid.UUID, status: str, poll_token: uuid.UUID) -> None:
        """Persist a terminal flow state unless it was cancelled."""
        ProviderOAuthFlow.objects.filter(
            id=flow_id, poll_token=poll_token, status=ProviderOAuthFlow.Status.POLLING
        ).update(
            status=status,
            poll_token=None,
            device_auth_id_encrypted="",
            user_code_encrypted="",
            updated_at=timezone.now(),
        )

    @staticmethod
    def cancel(
        flow_id: uuid.UUID | str, organization_id: uuid.UUID, user_id: int
    ) -> bool:
        """Cancel only the caller's flow, including a poll in flight."""
        return bool(
            ProviderOAuthFlow.objects.filter(
                id=flow_id,
                organization_id=organization_id,
                user_id=user_id,
                status__in=[
                    ProviderOAuthFlow.Status.PENDING,
                    ProviderOAuthFlow.Status.POLLING,
                ],
            ).update(
                status=ProviderOAuthFlow.Status.CANCELLED,
                poll_token=None,
                device_auth_id_encrypted="",
                user_code_encrypted="",
                updated_at=timezone.now(),
            )
        )

    @staticmethod
    def claim_completion(
        flow_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: int,
        poll_token: uuid.UUID,
    ) -> ProviderOAuthFlow | None:
        """Conditionally consume the polling claim for a single completion."""
        now = timezone.now()
        updated = ProviderOAuthFlow.objects.filter(
            id=flow_id,
            organization_id=organization_id,
            user_id=user_id,
            status=ProviderOAuthFlow.Status.POLLING,
            poll_token=poll_token,
            created_at__gte=now - timedelta(seconds=600),
        ).update(status=ProviderOAuthFlow.Status.CONSUMED, updated_at=now)
        if not updated:
            return None
        return ProviderOAuthFlow.objects.get(
            id=flow_id, organization_id=organization_id, user_id=user_id
        )

    @staticmethod
    def mark_completed(
        flow: ProviderOAuthFlow, account_id: str, connected_account_id: str
    ) -> None:
        """Consume one flow and erase its device authorization material."""
        flow.status = ProviderOAuthFlow.Status.CONSUMED
        flow.poll_token = None
        flow.connected_account_id = connected_account_id
        flow.device_auth_id_encrypted = ""
        flow.user_code_encrypted = ""
        flow.save(
            update_fields=[
                "status",
                "poll_token",
                "connected_account_id",
                "device_auth_id_encrypted",
                "user_code_encrypted",
                "updated_at",
            ]
        )

    @staticmethod
    def cleanup_expired() -> int:
        """Delete old terminal OAuth state, retaining active and recent flows."""
        cutoff = timezone.now() - timedelta(days=1)
        deleted, _ = ProviderOAuthFlow.objects.filter(created_at__lt=cutoff).delete()
        return deleted
