"""Persistence boundary for per-answer provider snapshots and revocation."""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .provider_turn_models import ProviderTurn, ProviderTurnMutex


class ProviderTurnRepository:
    """Serialize provider snapshot admission and account revocation."""

    @staticmethod
    def lock_mutex() -> None:
        """Acquire the database admission/revocation fence in a transaction."""
        ProviderTurnMutex.objects.get_or_create(id=1)
        # An UPDATE obtains a real row/write lock on both PostgreSQL and SQLite;
        # select_for_update alone is a no-op on SQLite.
        ProviderTurnMutex.objects.filter(id=1).update(id=F("id"))
        ProviderTurnMutex.objects.select_for_update().get(id=1)

    @classmethod
    def admit(
        cls,
        *,
        assistant_message_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: int | None,
        account_ids: dict[str, str],
    ) -> ProviderTurn:
        """Persist an active turn and recheck that its accounts are live."""
        from .models import ProviderConnection

        with transaction.atomic():
            cls.lock_mutex()
            if user_id is not None:
                from apps.organizations.models import Membership

                if not Membership.objects.filter(
                    organization_id=organization_id,
                    user_id=user_id,
                    user__is_active=True,
                ).exists():
                    raise ValueError(
                        "The initiating user no longer has active "
                        "organization membership"
                    )
            parsed = {
                provider: uuid.UUID(str(value))
                for provider, value in account_ids.items()
            }
            ids = list(parsed.values())
            if len(ids) != len(set(ids)):
                raise ValueError("A provider snapshot contains duplicate accounts")
            if ids:
                accounts = ProviderConnection.objects.filter(id__in=ids)
                rows = {str(row.id): row for row in accounts}
                if len(rows) != len(set(ids)):
                    raise ValueError("A selected provider account no longer exists")
                for provider, account_id in parsed.items():
                    account = rows[str(account_id)]
                    if account.deleted_at is not None:
                        raise ValueError("A selected provider account was revoked")
                    if account.organization_id != organization_id:
                        raise ValueError("A selected provider account is out of scope")
                    if account.provider != provider:
                        raise ValueError("A selected provider account changed provider")
                    if user_id is not None and account.user_id not in (None, user_id):
                        raise ValueError(
                            "A selected provider account is no longer visible"
                        )
            return ProviderTurn.objects.create(
                assistant_message_id=assistant_message_id,
                organization_id=organization_id,
                initiated_by_id=user_id,
                account_ids=dict(account_ids),
                dependency_account_ids=[],
                holder_tokens=[str(uuid.uuid4())],
                holds=1,
            )

    @classmethod
    def register_use(
        cls,
        turn_id: uuid.UUID | str,
        account_id: uuid.UUID | str | None,
        provider: str | None = None,
    ) -> None:
        """Record an account dependency before use and reject revocation races."""
        if account_id is None:
            return
        parsed_id = uuid.UUID(str(account_id))
        from .models import ProviderConnection

        rejected = False
        with transaction.atomic():
            cls.lock_mutex()
            turn = ProviderTurn.objects.select_for_update().get(id=turn_id)
            account = ProviderConnection.objects.filter(id=parsed_id).first()
            actual_provider = provider or (account.provider if account else "")
            snapshot = turn.account_ids or {}
            if provider is None:
                actual_provider = next(
                    (key for key, value in snapshot.items() if value == str(parsed_id)),
                    actual_provider,
                )
            expected = snapshot.get(actual_provider)
            if expected != str(parsed_id):
                raise ValueError("The provider account is outside this answer snapshot")
            rejected = (
                not turn.active
                or turn.cancel_requested
                or account is None
                or account.deleted_at is not None
                or account.organization_id != turn.organization_id
                or account.provider != actual_provider
                or (
                    account.user_id is not None
                    and account.user_id != turn.initiated_by_id
                )
            )
            if rejected:
                turn.cancel_requested = True
                turn.revoked_account_id = parsed_id
                turn.save(update_fields=["cancel_requested", "revoked_account_id"])
            else:
                used = list(turn.used_account_ids or [])
                dependencies = list(turn.dependency_account_ids or [])
                token = str(parsed_id)
                changed = False
                if token not in used:
                    used.append(token)
                    turn.used_account_ids = used
                    changed = True
                if token not in dependencies:
                    dependencies.append(token)
                    turn.dependency_account_ids = dependencies
                    changed = True
                if changed:
                    turn.save(
                        update_fields=["used_account_ids", "dependency_account_ids"]
                    )
        if rejected:
            raise ValueError(
                "The selected provider account was removed or access revoked"
            )

    @classmethod
    def cancellation_reason(cls, turn_id: uuid.UUID | str) -> str:
        """Return the account-revoked reason while an answer remains active."""
        turn = (
            ProviderTurn.objects.filter(id=turn_id, active=True)
            .values("cancel_requested")
            .first()
        )
        return "account_revoked" if turn and turn["cancel_requested"] else ""

    @classmethod
    def revoke_account(
        cls, account_id: uuid.UUID, *, allowed_user_id: int | None = None
    ) -> int:
        """Flag active turn snapshots that already depend on a revoked account."""
        # Domainservice calls this inside its transaction; keep a defensive
        # atomic scope for the compatibility path too.
        with transaction.atomic():
            cls.lock_mutex()
            query = ProviderTurn.objects.filter(active=True)
            if allowed_user_id is not None:
                query = query.exclude(initiated_by_id=allowed_user_id)
            # JSONField __contains is not supported by SQLite. Active sidecars
            # are small, so filter the bounded candidate set in Python.
            matching_ids = [
                row.id
                for row in query.only(
                    "id", "account_ids", "dependency_account_ids"
                ).iterator()
                if str(account_id)
                in {
                    *list((row.account_ids or {}).values()),
                    *list(row.dependency_account_ids or []),
                }
            ]
            if not matching_ids:
                return 0
            return ProviderTurn.objects.filter(id__in=matching_ids).update(
                cancel_requested=True,
                revoked_account_id=account_id,
            )

    @classmethod
    def revoke_account_for_user(cls, account_id: uuid.UUID, user_id: int) -> int:
        """Revoke active snapshots for one member losing account access."""
        with transaction.atomic():
            cls.lock_mutex()
            matching_ids = [
                row.id
                for row in ProviderTurn.objects.filter(
                    active=True, initiated_by_id=user_id
                )
                .only("id", "account_ids", "dependency_account_ids")
                .iterator()
                if str(account_id)
                in {
                    *list((row.account_ids or {}).values()),
                    *list(row.dependency_account_ids or []),
                }
            ]
            if not matching_ids:
                return 0
            return ProviderTurn.objects.filter(id__in=matching_ids).update(
                cancel_requested=True,
                revoked_account_id=account_id,
            )

    @classmethod
    def revoke_member_turns(
        cls, organization_id: uuid.UUID, user_id: int
    ) -> set[uuid.UUID]:
        """Revoke every active answer when a member loses org access.

        Membership removal invalidates personal accounts too: the credentials
        remain owned by the user, but an in-flight answer must not outlive the
        organization authorization under which it was admitted.
        """
        with transaction.atomic():
            cls.lock_mutex()
            turns = list(
                ProviderTurn.objects.filter(
                    active=True,
                    organization_id=organization_id,
                    initiated_by_id=user_id,
                ).values("id", "account_ids", "dependency_account_ids")
            )
            account_ids: set[uuid.UUID] = set()
            for turn in turns:
                references = [
                    *list((turn["account_ids"] or {}).values()),
                    *list(turn["dependency_account_ids"] or []),
                ]
                parsed_references: list[uuid.UUID] = []
                for reference in references:
                    try:
                        account_id = uuid.UUID(str(reference))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    account_ids.add(account_id)
                    parsed_references.append(account_id)
                ProviderTurn.objects.filter(id=turn["id"], active=True).update(
                    cancel_requested=True,
                    revoked_account_id=(
                        parsed_references[0] if parsed_references else None
                    ),
                )
            return account_ids

    @classmethod
    def finish_once(cls, turn_id: uuid.UUID | str, holder: str) -> bool:
        """Idempotently release a named holder, retaining all other work."""
        with transaction.atomic():
            cls.lock_mutex()
            turn = (
                ProviderTurn.objects.select_for_update()
                .filter(id=turn_id, active=True)
                .first()
            )
            if turn is None:
                return False
            holders = list(turn.holder_tokens or [])
            if holder not in holders:
                return False
            holders.remove(holder)
            turn.holder_tokens = holders
            turn.holds = len(holders)
            fields = ["holder_tokens", "holds"]
            if not holders and not turn.monitor_required:
                turn.active = False
                turn.completed_at = timezone.now()
                fields.extend(["active", "completed_at"])
            turn.save(update_fields=fields)
            return True

    @classmethod
    def finish(cls, turn_id: uuid.UUID | str) -> None:
        """Release one legacy anonymous holder."""
        turn = cls.get(turn_id)
        if turn is None:
            return
        token = str((turn.holder_tokens or [""])[0]) if turn.holder_tokens else ""
        if token:
            cls.finish_once(turn_id, token)

    @classmethod
    def set_monitor_required(cls, turn_id: uuid.UUID | str) -> None:
        """Mark turn cancellation as retained until local followers drain."""
        with transaction.atomic():
            cls.lock_mutex()
            ProviderTurn.objects.filter(id=turn_id, active=True).update(
                monitor_required=True
            )

    @staticmethod
    def monitor_required(turn_id: uuid.UUID | str) -> bool:
        """Return the monitor-retention flag for one live turn."""
        return bool(
            ProviderTurn.objects.filter(id=turn_id, active=True)
            .values_list("monitor_required", flat=True)
            .first()
        )

    @classmethod
    def clear_monitor_required(cls, turn_id: uuid.UUID | str) -> None:
        """Release the monitor hold after its follower tree drains."""
        with transaction.atomic():
            cls.lock_mutex()
            turn = (
                ProviderTurn.objects.select_for_update()
                .filter(id=turn_id, active=True)
                .first()
            )
            if turn is None:
                return
            turn.monitor_required = False
            fields = ["monitor_required"]
            if not turn.holder_tokens:
                turn.active = False
                turn.completed_at = timezone.now()
                fields.extend(["active", "completed_at"])
            turn.save(update_fields=fields)

    @classmethod
    def add_hold(cls, turn_id: uuid.UUID | str, holder: str | None = None) -> bool:
        """Keep a turn snapshot active for one unique follow-up holder."""
        with transaction.atomic():
            cls.lock_mutex()
            turn = (
                ProviderTurn.objects.select_for_update()
                .filter(id=turn_id, active=True)
                .first()
            )
            if turn is None or turn.cancel_requested:
                return False
            holders = list(turn.holder_tokens or [])
            token = str(holder or uuid.uuid4())
            if token in holders:
                return True
            holders.append(token)
            turn.holder_tokens = holders
            turn.holds = len(holders)
            turn.save(update_fields=["holder_tokens", "holds"])
            return True

    @staticmethod
    def get(turn_id: uuid.UUID | str) -> ProviderTurn | None:
        """Return one durable turn sidecar."""
        return ProviderTurn.objects.filter(id=turn_id).first()

    @staticmethod
    def account_map(turn_id: uuid.UUID | str) -> dict[str, str]:
        """Return immutable provider account IDs captured at admission."""
        row = (
            ProviderTurn.objects.filter(id=turn_id)
            .values_list("account_ids", flat=True)
            .first()
        )
        return dict(row or {})

    @staticmethod
    def dependent_active_turn_ids(account_id: uuid.UUID) -> list[uuid.UUID]:
        """Find active turn IDs already registered as dependent on an account."""
        return [
            row.id
            for row in ProviderTurn.objects.filter(active=True)
            .only("id", "used_account_ids")
            .iterator()
            if str(account_id) in list(row.used_account_ids or [])
        ]
