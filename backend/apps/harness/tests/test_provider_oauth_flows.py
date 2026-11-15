"""Durable ChatGPT OAuth flow isolation and reconnect tests."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from django.contrib.auth import get_user_model

from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.provider_oauth import ProviderOAuthService
from apps.harness.provider_oauth_models import ProviderOAuthFlow
from apps.harness.providers.chatgpt_oauth import (
    DeviceFlowPollResult,
    DeviceFlowStart,
    OAuthTokens,
)
from apps.organizations.models import Membership, MembershipRole
from common.exceptions import AuthenticationError


@pytest.fixture
def oauth_users(organization):
    """Create independent users with active org memberships."""
    users = get_user_model()
    admin = users.objects.create_user(
        email=f"oa-{uuid.uuid4().hex[:8]}@example.com", password="x"
    )
    member = users.objects.create_user(
        email=f"om-{uuid.uuid4().hex[:8]}@example.com", password="x"
    )
    other = users.objects.create_user(
        email=f"oo-{uuid.uuid4().hex[:8]}@example.com", password="x"
    )
    Membership.objects.create(
        user=admin, organization=organization, role=MembershipRole.ADMIN
    )
    Membership.objects.create(
        user=member, organization=organization, role=MembershipRole.MEMBER
    )
    Membership.objects.create(
        user=other, organization=organization, role=MembershipRole.MEMBER
    )
    return organization, admin, member, other


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_oauth_flow_ids_are_user_scoped_and_reconnect_reuses_account(oauth_users):
    """Flows are isolated per user and reconnect updates the exact account."""
    org, _admin, member, other = oauth_users
    service = ProviderOAuthService()
    flow_start = DeviceFlowStart(
        "dev-code", "USER-CODE", "https://auth.openai.com/codex/device", 5
    )
    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
        return_value=flow_start,
    ):
        first = await service.start(
            organization_id=org.id, user=member, label="My ChatGPT"
        )
        second = await service.start(
            organization_id=org.id, user=other, label="Their ChatGPT"
        )
    assert first["flow_id"] != second["flow_id"]
    row = ProviderOAuthFlow.objects.get(id=first["flow_id"])
    assert "dev-code" not in row.device_auth_id_encrypted
    assert "USER-CODE" not in row.user_code_encrypted
    assert await service.status(
        organization_id=org.id, user=other, flow_id=first["flow_id"]
    ) == {"status": "no_flow"}
    assert not service.cancel(
        organization_id=org.id, user=other, flow_id=first["flow_id"]
    )

    tokens = OAuthTokens("access-secret", "refresh-secret", 9999999999, "acct-1", None)
    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
        return_value=DeviceFlowPollResult("complete", tokens),
    ):
        connected = await service.status(
            organization_id=org.id, user=member, flow_id=first["flow_id"]
        )
    assert connected == {"status": "connected", "account_id": "acct-1"}
    accounts = ProviderAccountService().list_accounts(org.id, member.id, "chatgpt")
    account_id = accounts[0]["id"]
    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
        return_value=flow_start,
    ):
        reconnect = await service.start(
            organization_id=org.id, user=member, account_id=account_id
        )
    with patch(
        "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
        new_callable=AsyncMock,
        return_value=DeviceFlowPollResult("complete", tokens),
    ):
        await service.status(
            organization_id=org.id, user=member, flow_id=reconnect["flow_id"]
        )
    assert (
        len(ProviderAccountService().list_accounts(org.id, member.id, "chatgpt")) == 1
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_oauth_shared_flow_requires_admin(oauth_users):
    """A member cannot start a shared OAuth flow."""
    org, _admin, member, _other = oauth_users
    with pytest.raises(AuthenticationError):
        await ProviderOAuthService().start(
            organization_id=org.id, user=member, shared=True
        )


@pytest.mark.django_db(transaction=True)
async def test_poll_claims_are_independent_and_cancellation_wins(oauth_users):
    """Parallel flows never release each other's claims or save cancelled tokens."""
    from apps.harness.provider_oauth_repository import ProviderOAuthRepository

    org, _admin, member, _other = oauth_users
    service = ProviderOAuthService()
    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
        return_value=DeviceFlowStart("device", "code", "https://example.test", 5),
    ):
        a = await service.start(organization_id=org.id, user=member, label="Account A")
        b = await service.start(organization_id=org.id, user=member, label="Account B")
    first, state = ProviderOAuthRepository.claim(a["flow_id"], org.id, member.id)
    assert state == "claimed"
    second, state = ProviderOAuthRepository.claim(b["flow_id"], org.id, member.id)
    assert state == "claimed"
    assert (
        ProviderOAuthRepository.claim(a["flow_id"], org.id, member.id)[1] == "polling"
    )
    assert first.poll_token != second.poll_token
    assert service.cancel(organization_id=org.id, user=member, flow_id=a["flow_id"])
    assert not service._complete(
        first.id,
        org.id,
        member,
        {"access": "must-not-save", "refresh": "private-refresh", "expires": 99},
        first.poll_token,
    )
    assert ProviderAccountService().list_accounts(org.id, member.id, "chatgpt") == []


@pytest.mark.django_db(transaction=True)
async def test_poll_recovery_fences_old_generation(oauth_users):
    """A late response cannot consume a recovered newer polling lease."""
    from datetime import timedelta

    from django.utils import timezone

    from apps.harness.provider_oauth_repository import ProviderOAuthRepository

    org, _admin, member, _other = oauth_users
    service = ProviderOAuthService()
    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        new_callable=AsyncMock,
        return_value=DeviceFlowStart("device", "code", "https://example.test", 5),
    ):
        result = await service.start(organization_id=org.id, user=member)
    old, _ = ProviderOAuthRepository.claim(result["flow_id"], org.id, member.id)
    ProviderOAuthFlow.objects.filter(id=old.id).update(
        updated_at=timezone.now() - timedelta(seconds=121)
    )
    current, state = ProviderOAuthRepository.claim(old.id, org.id, member.id)
    assert state == "claimed" and current.poll_token != old.poll_token
    assert not service._complete(
        old.id, org.id, member, {"access": "late"}, old.poll_token
    )
    ProviderOAuthRepository.release_to_pending(old.id, old.poll_token)
    current.refresh_from_db()
    assert current.status == "polling" and current.poll_token is not None


@pytest.mark.django_db(transaction=True)
async def test_parallel_default_named_oauth_flows_create_distinct_accounts(oauth_users):
    """Overlapping provider requests reserve unique labels before completion."""
    import asyncio

    org, _admin, member, _other = oauth_users
    service = ProviderOAuthService()
    both_started = asyncio.Event()
    entered = 0

    async def start_device():
        nonlocal entered
        entered += 1
        index = entered
        if entered == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=5)
        return DeviceFlowStart(
            f"device-{index}", f"code-{index}", "https://example.test", 5
        )

    with patch(
        "apps.harness.providers.chatgpt_oauth.start_device_flow",
        side_effect=start_device,
    ):
        first, second = await asyncio.gather(
            service.start(organization_id=org.id, user=member),
            service.start(organization_id=org.id, user=member),
        )
    rows = list(
        ProviderOAuthFlow.objects.filter(id__in=[first["flow_id"], second["flow_id"]])
    )
    assert {row.label for row in rows} == {"ChatGPT", "ChatGPT 2"}
    for row in rows:
        tokens = OAuthTokens(
            f"access-{row.id}", f"refresh-{row.id}", 9999999999, str(row.id), None
        )
        with patch(
            "apps.harness.providers.chatgpt_oauth.poll_device_flow_once",
            new_callable=AsyncMock,
            return_value=DeviceFlowPollResult("complete", tokens),
        ):
            result = await service.status(
                organization_id=org.id, user=member, flow_id=row.id
            )
        assert result["status"] == "connected"
    accounts = ProviderAccountService().list_accounts(org.id, member.id, "chatgpt")
    assert {row["label"] for row in accounts} == {"ChatGPT", "ChatGPT 2"}
    assert len({row["account_id"] for row in accounts}) == 2
    assert sum(row["is_active"] for row in accounts) == 1
