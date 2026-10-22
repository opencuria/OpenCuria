"""Cross-worker OAuth refresh, authority, fencing and adapter contracts."""

from __future__ import annotations

import asyncio
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import pytest
from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.utils import timezone

from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.provider_refresh_models import ProviderRefreshLease
from apps.harness.providers import chatgpt_credentials as module
from apps.harness.providers.base import ProviderAuthError, ProviderTimeoutError
from apps.harness.providers.chatgpt import ChatGPTAdapter
from apps.harness.providers.chatgpt_credentials import (
    ChatGPTCredentialService,
    get_fresh_chatgpt_credentials,
)
from apps.harness.providers.chatgpt_oauth import OAuthTokens
from apps.harness.providers.resolver import ProviderResolver
from apps.organizations.models import Membership, MembershipRole
from common.exceptions import AuthenticationError, NotFoundError

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def refresh_account(organization):
    """Create an exact personal account and its initiating member."""
    user = get_user_model().objects.create_user(email="refresh@example.com")
    Membership.objects.create(
        organization=organization, user=user, role=MembershipRole.ADMIN
    )
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=organization.id,
        user=user,
        provider="chatgpt",
        payload={"access": "access-old", "refresh": "refresh-old", "expires": 1},
    )
    return organization, user, account, accounts


def _service(context):
    org, user, account, _ = context
    return ChatGPTCredentialService(org.id, user.id, account.id)


def _tokens():
    return OAuthTokens(
        "access-new", "refresh-new", int((time.time() + 3600) * 1000), "acct", None
    )


async def test_many_services_share_one_exchange(refresh_account, monkeypatch):
    """Many service instances/coroutines converge on one lease and exchange."""
    calls = []

    async def exchange(refresh, client=None):
        calls.append(refresh)
        await asyncio.sleep(0.1)
        return _tokens()

    monkeypatch.setattr(module, "refresh_tokens", exchange)
    results = await asyncio.gather(
        *[_service(refresh_account).get_fresh_credentials() for _ in range(10)]
    )
    assert calls == ["refresh-old"]
    assert all(result["access"] == "access-new" for result in results)
    org, user, account, accounts = refresh_account

    def check_saved():
        live = accounts.get_account(org.id, user.id, account.id)
        assert accounts.get_credentials(live)["refresh"] == "refresh-new"
        assert "access-new" not in live.credential.encrypted_value
        lease = ProviderRefreshLease.objects.get(account_id=account.id)
        assert lease.owner is None and lease.generation == 1
        assert "access" not in repr(lease.__dict__)

    await sync_to_async(check_saved)()


def test_independent_worker_threads_coordinate(refresh_account, monkeypatch):
    """Separate event loops/DB connections prove no in-memory lock is needed."""
    import threading

    barrier = threading.Barrier(4)
    calls = []

    async def exchange(refresh, client=None):
        calls.append(refresh)
        await asyncio.sleep(0.1)
        return _tokens()

    monkeypatch.setattr(module, "refresh_tokens", exchange)

    def worker():
        close_old_connections()
        barrier.wait(timeout=5)

        async def independent_context():
            async with ThreadSensitiveContext():
                return await _service(refresh_account).get_fresh_credentials()

        try:
            return asyncio.run(independent_context())
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: worker(), range(4)))
    assert calls == ["refresh-old"]
    assert all(result["access"] == "access-new" for result in results)


async def test_fresh_margin_and_rejected_token(refresh_account, monkeypatch):
    """Refresh near expiry, but never re-exchange a newer rejected generation."""
    org, user, account, accounts = refresh_account
    calls = []

    async def exchange(refresh, client=None):
        calls.append(refresh)
        return _tokens()

    monkeypatch.setattr(module, "refresh_tokens", exchange)
    await sync_to_async(accounts.update_credentials)(
        account.id, {"access": "current", "expires": int((time.time() + 3600) * 1000)}
    )
    service = _service(refresh_account)
    assert (await service.get_fresh_credentials())["access"] == "current"
    assert (
        await service.get_fresh_credentials(rejected_access_token="older", force=True)
    )["access"] == "current"
    assert not calls
    await service.get_fresh_credentials(rejected_access_token="current", force=True)
    await service.get_fresh_credentials(rejected_access_token="current", force=True)
    assert calls == ["refresh-old"]
    await sync_to_async(accounts.update_credentials)(
        account.id, {"expires": int((time.time() + 20) * 1000)}
    )
    await service.get_fresh_credentials()
    assert calls == ["refresh-old", "refresh-new"]
    assert (await get_fresh_chatgpt_credentials(org.id, user.id, account.id))["access"]


def test_stale_owner_cannot_save_or_release_successor(refresh_account):
    """Expired ownership is fenced from persistence and release."""
    service = _service(refresh_account)
    first = service._prepare(None, False)
    ProviderRefreshLease.objects.filter(account_id=service.account_id).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    second = service._prepare(None, False)
    assert second.generation == first.generation + 1
    with pytest.raises(ProviderAuthError):
        service._save(first, _tokens())
    service.repository.release(service.account_id, first.owner, first.generation)
    assert service.repository.owns(service.account_id, second.owner, second.generation)
    assert service._save(second, _tokens())["access"] == "access-new"


@pytest.mark.parametrize("mutation", ["delete", "reconnect", "unshare", "membership"])
async def test_changes_during_exchange_prevent_save(
    refresh_account, monkeypatch, mutation
):
    """Lifecycle changes and revoked ACL must not overwrite newer state."""
    org, user, account, accounts = refresh_account
    if mutation == "unshare":
        await sync_to_async(accounts.save_account)(
            organization_id=org.id,
            user=user,
            provider="chatgpt",
            account_id=account.id,
            payload={"shared": True},
        )

    def change():
        if mutation == "delete":
            accounts.delete_account(org.id, user.id, account.id)
        elif mutation == "membership":
            Membership.objects.filter(organization=org, user=user).delete()
        else:
            accounts.save_account(
                organization_id=org.id,
                user=user,
                provider="chatgpt",
                account_id=account.id,
                payload={"shared": False}
                if mutation == "unshare"
                else {"access": "reconnected", "refresh": "new-login"},
            )

    async def exchange(refresh, client=None):
        await sync_to_async(change)()
        return _tokens()

    monkeypatch.setattr(module, "refresh_tokens", exchange)
    with pytest.raises((ProviderAuthError, NotFoundError, AuthenticationError)):
        await _service(refresh_account).get_fresh_credentials()
    live = await sync_to_async(accounts.repository.get_by_id)(account.id)
    if live is not None and mutation != "membership":
        assert (await sync_to_async(accounts.get_credentials)(live))["access"] != (
            "access-new"
        )


async def test_fresh_preflight_revalidates_acl_and_exact_account(refresh_account):
    """Fresh tokens do not bypass ACL and an absent ID never falls back."""
    org, user, account, accounts = refresh_account
    await sync_to_async(accounts.update_credentials)(
        account.id, {"expires": int((time.time() + 3600) * 1000)}
    )
    with pytest.raises(NotFoundError):
        await get_fresh_chatgpt_credentials(org.id, user.id, uuid.uuid4())
    await sync_to_async(Membership.objects.filter(user=user).delete)()
    with pytest.raises(AuthenticationError):
        await _service(refresh_account).get_fresh_credentials()


async def test_oauth_error_sanitized_and_lease_released(refresh_account, monkeypatch):
    """OAuth response body/exception strings must not escape to logs."""

    async def exchange(refresh, client=None):
        raise RuntimeError("secret-access-and-refresh-in-response")

    monkeypatch.setattr(module, "refresh_tokens", exchange)
    with pytest.raises(ProviderAuthError) as caught:
        await _service(refresh_account).get_fresh_credentials()
    assert "secret-access" not in str(caught.value)
    assert caught.value.__suppress_context__ and caught.value.__cause__ is None
    lease = await sync_to_async(ProviderRefreshLease.objects.get)(
        account_id=refresh_account[2].id
    )
    assert lease.owner is None


async def test_busy_lease_has_bounded_wait(refresh_account, monkeypatch):
    """A crashed worker cannot hold an engine refresh RPC indefinitely."""
    await sync_to_async(_service(refresh_account)._prepare)(None, False)
    monkeypatch.setattr(module, "WAIT_TIMEOUT_SECONDS", 0.1)
    with pytest.raises(ProviderTimeoutError):
        await asyncio.wait_for(_service(refresh_account).get_fresh_credentials(), 1)


async def test_network_total_timeout_sanitized(refresh_account, monkeypatch):
    """Network duration is bounded independently of transport timeouts."""

    async def exchange(refresh, client=None):
        await asyncio.sleep(10)

    monkeypatch.setattr(module, "refresh_tokens", exchange)
    monkeypatch.setattr(module, "REFRESH_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(ProviderAuthError):
        await asyncio.wait_for(_service(refresh_account).get_fresh_credentials(), 1)


async def test_native_401_passes_rejected_token_and_retries_once():
    """Native callbacks receive preflight and the exact HTTP rejected token."""
    callbacks, requests = [], []

    async def fresh(**kwargs):
        callbacks.append(kwargs)
        return {"access": "new" if kwargs else "old"}

    def handle(request):
        requests.append(request.headers["authorization"])
        return (
            httpx.Response(401)
            if len(requests) == 1
            else httpx.Response(200, text="data: [DONE]\n\n")
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = ChatGPTAdapter({}, client=client, fresh_credentials=fresh)
        _ = [delta async for delta in adapter.chat_stream("gpt", [], [])]
    assert requests == ["Bearer old", "Bearer new"]
    assert callbacks == [{}, {"rejected_access_token": "old", "force": True}]


async def test_resolver_captures_exact_authority(refresh_account, monkeypatch):
    """Resolver native adapter invokes the same service entry point as Codex."""
    org, user, account, _ = refresh_account
    resolver = await sync_to_async(ProviderResolver)(
        org.id, user_id=user.id, account_ids={"chatgpt": str(account.id)}
    )
    resolved = await sync_to_async(resolver.resolve)("chatgpt/gpt")
    calls = []

    async def fresh(*args, **kwargs):
        calls.append((args, kwargs))
        return {"access": "live"}

    monkeypatch.setattr(
        "apps.harness.providers.resolver.get_fresh_chatgpt_credentials", fresh
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="data: [DONE]\n\n")
        )
    ) as client:
        resolved.adapter._client = client
        _ = [delta async for delta in resolved.adapter.chat_stream("gpt", [], [])]
    assert calls == [((org.id, user.id, account.id), {})]


async def test_forced_callers_with_same_rejected_token_converge(
    refresh_account, monkeypatch
):
    """Codex and native 401 callbacks share a single rotating exchange."""
    calls = []

    async def exchange(refresh, client=None):
        calls.append(refresh)
        await asyncio.sleep(0.1)
        return _tokens()

    monkeypatch.setattr(module, "refresh_tokens", exchange)
    org, user, account, accounts = refresh_account
    await sync_to_async(accounts.update_credentials)(
        account.id, {"expires": int((time.time() + 3600) * 1000)}
    )
    results = await asyncio.gather(
        *[
            get_fresh_chatgpt_credentials(
                org.id,
                user.id,
                account.id,
                rejected_access_token="access-old",
                force=True,
            )
            for _ in range(5)
        ]
    )
    assert calls == ["refresh-old"]
    assert all(result["access"] == "access-new" for result in results)


def test_same_plaintext_reconnect_still_fences_save(refresh_account):
    """A reconnect must invalidate ownership even when tokens happen to match."""
    org, user, account, accounts = refresh_account
    service = _service(refresh_account)
    claim = service._prepare(None, False)
    accounts.save_account(
        organization_id=org.id,
        user=user,
        account_id=account.id,
        provider="chatgpt",
        payload={"access": "access-old", "refresh": "refresh-old"},
    )
    with pytest.raises(ProviderAuthError):
        service._save(claim, _tokens())
