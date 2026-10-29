"""Characterize the atomic boundary shared by accounts and provider turns."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from django.db import connection

from apps.credentials.models import Credential
from apps.harness.models import (
    HarnessMessage,
    ProviderConnection,
    ProviderSelection,
    ProviderTurn,
)
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.provider_oauth import ProviderOAuthService
from apps.harness.provider_oauth_models import ProviderOAuthFlow
from apps.harness.provider_oauth_repository import ProviderOAuthRepository
from apps.harness.provider_turn_repository import ProviderTurnRepository
from apps.harness.repositories import HarnessSessionRepository


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "operation", ["create", "update", "list", "selected", "select", "refresh", "delete"]
)
def test_account_operations_fence_before_reads_and_hold_transaction(
    organization: Any,
    provider_account_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    """All account workflows share admission's lock before reading mutable state."""
    service = ProviderAccountService()
    account = service.save_account(
        organization_id=organization.id,
        user=provider_account_user,
        provider="openrouter",
        payload={"api_key": "initial-key"},
    )
    calls: list[str] = []
    lock = ProviderTurnRepository.lock_mutex

    def fence() -> None:
        assert connection.in_atomic_block
        lock()
        calls.append("fence")

    def observed_read(name: str, read: Callable[..., Any]) -> Callable[..., Any]:
        def observe(*args: Any, **kwargs: Any) -> Any:
            assert connection.in_atomic_block
            assert calls and calls[0] == "fence"
            calls.append(name)
            return read(*args, **kwargs)

        return observe

    monkeypatch.setattr(ProviderTurnRepository, "lock_mutex", fence)
    for name in ("get_by_id", "get_visible", "list_visible", "list_selections"):
        monkeypatch.setattr(
            service.repository,
            name,
            observed_read(name, getattr(service.repository, name)),
        )
    monkeypatch.setattr(
        service.organizations,
        "require_active_membership",
        observed_read("membership", service.organizations.require_active_membership),
    )
    mutations = {
        "create": lambda: service.save_account(
            organization_id=organization.id,
            user=provider_account_user,
            provider="openrouter",
            payload={"label": "Another", "api_key": "another-key"},
        ),
        "update": lambda: service.save_account(
            organization_id=organization.id,
            user=provider_account_user,
            provider="openrouter",
            account_id=account.id,
            payload={"label": "Updated"},
        ),
        "list": lambda: service.list_accounts(
            organization.id, provider_account_user.id
        ),
        "selected": lambda: service.selected_accounts(
            organization.id, provider_account_user.id
        ),
        "select": lambda: service.select_account(
            organization.id, provider_account_user.id, "openrouter", None
        ),
        "refresh": lambda: service.update_credentials(
            account.id, {"api_key": "refreshed-key"}
        ),
        "delete": lambda: service.delete_account(
            organization.id, provider_account_user.id, account.id
        ),
    }
    assert not connection.in_atomic_block
    mutations[operation]()
    assert not connection.in_atomic_block
    assert any(call != "fence" for call in calls)


@pytest.mark.django_db
def test_failed_selection_initialization_rolls_back_account_and_secret(
    organization: Any,
    provider_account_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after account creation cannot leave an orphaned secret or account."""
    service = ProviderAccountService()

    def fail_selection(*args: Any, **kwargs: Any) -> None:
        assert ProviderConnection.objects.exists()
        assert Credential.objects.exists()
        raise RuntimeError("selection failed")

    monkeypatch.setattr(service.repository, "initialize_selection", fail_selection)
    with pytest.raises(RuntimeError, match="selection failed"):
        service.save_account(
            organization_id=organization.id,
            user=provider_account_user,
            provider="openrouter",
            payload={"api_key": "test-key"},
        )
    assert not ProviderConnection.objects.exists()
    assert not Credential.objects.exists()
    assert not ProviderSelection.objects.exists()


@pytest.mark.django_db
def test_failed_oauth_completion_rolls_back_flow_account_secret_and_selection(
    organization: Any,
    provider_account_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed completion keeps the polling claim and never saves partial tokens."""
    flow = ProviderOAuthRepository.create(
        organization_id=organization.id,
        user_id=provider_account_user.id,
        target_account_id=None,
        label="ChatGPT",
        shared=False,
        device_auth_id="device-code",
        user_code="user-code",
        interval=5,
    )
    claimed, state = ProviderOAuthRepository.claim(
        flow.id, organization.id, provider_account_user.id
    )
    assert claimed is not None and state == "claimed"
    before = ProviderOAuthFlow.objects.values().get(pk=flow.id)
    complete = ProviderOAuthRepository.mark_completed

    def fail_completion(*args: Any, **kwargs: Any) -> None:
        complete(*args, **kwargs)
        assert ProviderConnection.objects.exists()
        assert Credential.objects.exists()
        assert ProviderSelection.objects.exists()
        raise RuntimeError("completion failed")

    monkeypatch.setattr(ProviderOAuthRepository, "mark_completed", fail_completion)
    with pytest.raises(RuntimeError, match="completion failed"):
        ProviderOAuthService()._complete(
            flow.id,
            organization.id,
            provider_account_user,
            {"access": "access-token", "refresh": "refresh-token", "expires": 99},
            claimed.poll_token,
        )
    assert ProviderOAuthFlow.objects.values().get(pk=flow.id) == before
    assert not ProviderConnection.objects.exists()
    assert not Credential.objects.exists()
    assert not ProviderSelection.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("mutation", ["delete", "unshare"])
def test_failed_revocation_rolls_back_entire_account_graph(
    organization: Any,
    provider_account_user: Any,
    harness_workspace: Any,
    django_capture_on_commit_callbacks: Any,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    """Secrets, selections and turn cancellation roll back with account writes."""
    service = ProviderAccountService()
    account = service.save_account(
        organization_id=organization.id,
        user=provider_account_user,
        provider="openrouter",
        payload={"shared": True, "api_key": "test-key"},
    )
    member_id = harness_workspace.created_by_id
    service.select_account(organization.id, member_id, "openrouter", account.id)
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=organization.id,
    )
    turn = ProviderTurn.objects.create(
        assistant_message=HarnessMessage.objects.create(
            session=session, role="assistant", position=0
        ),
        organization_id=organization.id,
        initiated_by_id=member_id,
        account_ids={"openrouter": str(account.id)},
    )

    def snapshot() -> list[list[dict[str, Any]]]:
        return [
            list(model.objects.order_by("pk").values())
            for model in (
                ProviderConnection,
                Credential,
                ProviderSelection,
                ProviderTurn,
            )
        ]

    before = snapshot()
    last_write = (
        "soft_delete" if mutation == "delete" else "clear_selections_for_others"
    )
    write = getattr(service.repository, last_write)

    def fail_after_write(*args: Any, **kwargs: Any) -> None:
        write(*args, **kwargs)
        assert ProviderTurn.objects.get(pk=turn.id).cancel_requested
        raise RuntimeError("account mutation failed")

    monkeypatch.setattr(service.repository, last_write, fail_after_write)
    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        with pytest.raises(RuntimeError, match="account mutation failed"):
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
    assert callbacks == []
    assert snapshot() == before
