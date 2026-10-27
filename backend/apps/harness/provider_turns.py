"""Per-answer provider account snapshots and dependency tracking."""

from __future__ import annotations

import uuid

from django.db import transaction

from .provider_accounts import ProviderAccountService
from .provider_turn_models import ProviderTurn
from .provider_turn_repository import ProviderTurnRepository


class ProviderAccountRevokedError(ValueError):
    """A frozen provider account became unavailable while an answer was running."""

    def __init__(self) -> None:
        super().__init__(
            "This response was stopped because its selected provider account was "
            "removed or is no longer shared."
        )


class ProviderTurnService:
    """Create and manage immutable per-answer provider account snapshots."""

    def __init__(
        self,
        *,
        accounts: ProviderAccountService | None = None,
        repository: type[ProviderTurnRepository] | None = None,
    ) -> None:
        self.accounts = accounts or ProviderAccountService()
        self.repository = repository or ProviderTurnRepository

    def capture_account_ids(
        self, organization_id: uuid.UUID, user_id: int | None
    ) -> dict[str, str]:
        """Capture currently selected provider IDs without storing credentials."""
        if user_id is None:
            return {}
        with transaction.atomic():
            self.repository.lock_mutex()
            selected = self.accounts.selected_accounts(organization_id, user_id)
            return {provider: str(account.id) for provider, account in selected.items()}

    def admit(
        self,
        *,
        assistant_message_id: uuid.UUID,
        organization_id: uuid.UUID,
        user_id: int | None,
        account_ids: dict[str, str],
    ) -> ProviderTurn:
        """Persist an active snapshot once the assistant shell exists."""
        return self.repository.admit(
            assistant_message_id=assistant_message_id,
            organization_id=organization_id,
            user_id=user_id,
            account_ids=account_ids,
        )

    def get(self, turn_id: uuid.UUID | str) -> ProviderTurn | None:
        """Read a turn state through the repository boundary."""
        return self.repository.get(turn_id)

    def register_use(
        self,
        turn_id: uuid.UUID | str,
        account_id: uuid.UUID | str | None,
        provider: str | None = None,
    ) -> None:
        """Record the exact account before a provider request begins."""
        try:
            self.repository.register_use(turn_id, account_id, provider)
        except ValueError as exc:
            raise ProviderAccountRevokedError() from exc

    def cancellation_reason(self, turn_id: uuid.UUID | str) -> str:
        """Check durable cancellation state for one live answer."""
        return self.repository.cancellation_reason(turn_id)

    def finish_once(self, turn_id: uuid.UUID | str, holder: str) -> bool:
        """Idempotently release a named local holder for one answer."""
        return self.repository.finish_once(turn_id, holder)

    def add_hold(self, turn_id: uuid.UUID | str, holder: str) -> bool:
        """Keep the snapshot active for one title or other answer task."""
        return self.repository.add_hold(turn_id, holder)

    def set_monitor_required(self, turn_id: uuid.UUID | str) -> None:
        """Keep the durable cancel flag until local followers have drained."""
        self.repository.set_monitor_required(turn_id)

    def monitor_required(self, turn_id: uuid.UUID | str) -> bool:
        """Return whether the turn requires a live local revocation monitor."""
        return self.repository.monitor_required(turn_id)

    def clear_monitor_required(self, turn_id: uuid.UUID | str) -> None:
        """Release the monitor holder after all tracked followers drain."""
        self.repository.clear_monitor_required(turn_id)
