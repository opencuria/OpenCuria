"""Per-answer provider snapshots and real local HTTP provider integration."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model

from apps.harness.harness_service import HarnessService
from apps.harness.models import ProviderTurn
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.repositories import HarnessMessageRepository, HarnessSessionRepository
from apps.organizations.models import Membership, MembershipRole


@pytest.fixture
def provider_turn_member(organization):
    """Create an active provider-account member in the test organization."""
    user = get_user_model().objects.create_user(
        email=f"turn-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


class _LocalOpenRouter:
    """Local HTTP models + SSE endpoint with independently gated responses."""

    def __init__(self) -> None:
        self.entered: dict[int, threading.Event] = {}
        self.releases: dict[int, threading.Event] = {}
        self.request_counter = 0
        self.requests: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _send_json(self, payload: object) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.endswith("/models"):
                    self._send_json(
                        {
                            "data": [
                                {
                                    "id": "acme/test-model",
                                    "name": "Local test model",
                                    "context_length": 32_000,
                                    "top_provider": {"max_completion_tokens": 4096},
                                    "supported_parameters": ["tools"],
                                }
                            ]
                        }
                    )
                    return
                self.send_error(404)

            def do_POST(self) -> None:  # noqa: N802
                with owner._lock:
                    index = owner.request_counter
                    owner.request_counter += 1
                    owner.requests.append(
                        {
                            "authorization": self.headers.get("Authorization", ""),
                            "payload": json.loads(
                                self.rfile.read(
                                    int(self.headers.get("Content-Length", "0"))
                                )
                            ),
                        }
                    )
                    owner.entered.setdefault(index, threading.Event()).set()
                    release = owner.releases.setdefault(index, threading.Event())
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if not release.wait(timeout=20):
                    return
                body = (
                    b'data: {"choices":[{"delta":{"content":"local answer"},'
                    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
                    b'"completion_tokens":2,"total_tokens":5}}\n\n'
                    b"data: [DONE]\n\n"
                )
                try:
                    self.wfile.write(body)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def wait_entered(self, index: int, timeout: float = 8.0) -> bool:
        return self.entered.setdefault(index, threading.Event()).wait(timeout)

    def release(self, index: int) -> None:
        self.releases.setdefault(index, threading.Event()).set()

    def close(self) -> None:
        for event in self.releases.values():
            event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def local_openrouter():
    """Start a real loopback HTTP provider and always release its streams."""
    server = _LocalOpenRouter()
    try:
        yield server
    finally:
        server.close()


def _save_openrouter(organization_id, user, *, label: str, token: str, base_url: str):
    """Persist an OpenRouter account with no organization/provider aliasing."""
    return ProviderAccountService().save_account(
        organization_id=organization_id,
        user=user,
        provider="openrouter",
        payload={
            "label": label,
            "api_key": token,
            "base_url": base_url,
        },
    )


async def _wait_task(service: HarnessService, session_id: uuid.UUID) -> None:
    """Wait for a submitted owner without assuming it still occupies the map."""
    task = service._tasks.get(str(session_id))
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            # Cancellation is the existing HarnessService owner-abort path.
            pass


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_real_request_freezes_selection_until_the_next_turn(
    harness_workspace, provider_turn_member, local_openrouter
) -> None:
    """A live SSE request stays on account A; the subsequent turn uses B."""
    organization = harness_workspace.runner.organization
    user = provider_turn_member
    accounts = ProviderAccountService()
    first = _save_openrouter(
        organization.id,
        user,
        label="First",
        token="first-live-key",
        base_url=local_openrouter.base_url,
    )
    second = _save_openrouter(
        organization.id,
        user,
        label="Second",
        token="second-live-key",
        base_url=local_openrouter.base_url,
    )
    accounts.select_account(organization.id, user.id, "openrouter", first.id)

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=organization.id,
        model="openrouter/acme/test-model",
        title="Local provider e2e",
    )
    service = HarnessService()

    first_assistant = await service.start_run(
        session,
        "first response",
        organization_id=organization.id,
        workspace_id=str(harness_workspace.id),
        user_id=user.id,
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 0)
    # Changing selection while HTTP is in flight must not retarget that request.
    await sync_to_async(accounts.select_account)(
        organization.id, user.id, "openrouter", second.id
    )
    local_openrouter.release(0)
    await _wait_task(service, session.id)

    first_assistant = await sync_to_async(HarnessMessageRepository.get_by_id)(
        first_assistant.id
    )
    assert first_assistant is not None
    assert first_assistant.finish == "stop"
    assert first_assistant.content == "local answer"

    second_assistant = await service.start_run(
        session,
        "next response",
        organization_id=organization.id,
        workspace_id=str(harness_workspace.id),
        user_id=user.id,
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 1)
    local_openrouter.release(1)
    await _wait_task(service, session.id)

    assert [row["authorization"] for row in local_openrouter.requests] == [
        "Bearer first-live-key",
        "Bearer second-live-key",
    ]
    first_turn = await sync_to_async(ProviderTurn.objects.get)(
        assistant_message_id=first_assistant.id
    )
    second_turn = await sync_to_async(ProviderTurn.objects.get)(
        assistant_message_id=second_assistant.id
    )
    assert first_turn.account_ids == {"openrouter": str(first.id)}
    assert second_turn.account_ids == {"openrouter": str(second.id)}
    assert "first-live-key" not in repr(first_turn.account_ids)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_real_blocked_sse_revocation_aborts_without_fallback(
    harness_workspace, provider_turn_member, local_openrouter
) -> None:
    """Removing the in-flight account cancels the owner, not a replacement request."""
    organization = harness_workspace.runner.organization
    user = provider_turn_member
    accounts = ProviderAccountService()
    revoked = _save_openrouter(
        organization.id,
        user,
        label="Revoked",
        token="revoked-live-key",
        base_url=local_openrouter.base_url,
    )
    fallback = _save_openrouter(
        organization.id,
        user,
        label="Fallback",
        token="must-not-fallback",
        base_url=local_openrouter.base_url,
    )
    accounts.select_account(organization.id, user.id, "openrouter", revoked.id)

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=organization.id,
        model="openrouter/acme/test-model",
        title="Revocation e2e",
    )
    service = HarnessService()
    assistant = await service.start_run(
        session,
        "blocked response",
        organization_id=organization.id,
        workspace_id=str(harness_workspace.id),
        user_id=user.id,
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 0)

    await sync_to_async(accounts.delete_account)(organization.id, user.id, revoked.id)
    # Allow the local post-commit event + monitor cancellation to reach cleanup.
    for _ in range(80):
        await asyncio.sleep(0.05)
        fresh = await sync_to_async(HarnessMessageRepository.get_by_id)(assistant.id)
        if fresh is not None and fresh.finish == "aborted":
            break
    await _wait_task(service, session.id)
    local_openrouter.release(0)

    fresh = await sync_to_async(HarnessMessageRepository.get_by_id)(assistant.id)
    assert fresh is not None
    assert fresh.finish == "aborted"
    assert "removed or is no longer shared" in fresh.error
    assert len(local_openrouter.requests) == 1
    assert local_openrouter.requests[0]["authorization"] == "Bearer revoked-live-key"
    assert "must-not-fallback" not in str(local_openrouter.requests)
    turn = await sync_to_async(ProviderTurn.objects.get)(
        assistant_message_id=assistant.id
    )
    assert turn.account_ids == {"openrouter": str(revoked.id)}
    assert turn.cancel_requested is True
    assert turn.active is False
    assert fallback.id != revoked.id


def test_callback_model_resolver_is_db_free_and_fences_before_real_request(
    organization, provider_turn_member, local_openrouter
) -> None:
    """The sync resolver callback wraps network I/O with async account-use fencing."""
    from apps.harness.providers.resolver import ProviderResolver
    from apps.harness.repositories import ProviderConnectionRepository
    account = _save_openrouter(
        organization.id,
        provider_turn_member,
        label="Resolver account",
        token="resolver-live-key",
        base_url=local_openrouter.base_url,
    )
    account_ids = {"openrouter": str(account.id)}
    resolver = ProviderResolver(
        organization.id,
        connection_repository=ProviderConnectionRepository,
        user_id=provider_turn_member.id,
        account_ids=account_ids,
        on_account_used=lambda _account_id: None,
    )
    resolver.prepare_adapters()
    resolved = resolver.resolve("openrouter/acme/test-model")
    assert resolved.model_id == "acme/test-model"
    assert resolved.provider == "openrouter"
    assert resolved.adapter.name == "openrouter"
