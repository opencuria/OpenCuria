"""End-to-end provider snapshot/revocation flows against real local HTTP."""

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


class LocalOpenRouter:
    """Serve a real models endpoint plus a controllable HTTP/SSE completion."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.entered: list[threading.Event] = []
        self.releases: list[threading.Event] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _respond(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_GET(self) -> None:  # noqa: N802
                if self.path.endswith("/models"):
                    self._respond(
                        200,
                        "application/json",
                        json.dumps(
                            {
                                "data": [
                                    {
                                        "id": "acme/test-model",
                                        "name": "Local test model",
                                        "context_length": 32000,
                                        "top_provider": {"max_completion_tokens": 4096},
                                        "supported_parameters": ["tools"],
                                    }
                                ]
                            }
                        ).encode(),
                    )
                    return
                self._respond(404, "application/json", b"{}")

            def do_POST(self) -> None:  # noqa: N802
                with owner.lock:
                    owner.requests.append(
                        {
                            "authorization": self.headers.get("Authorization", ""),
                            "body": json.loads(
                                self.rfile.read(
                                    int(self.headers.get("Content-Length", "0"))
                                )
                            ),
                        }
                    )
                    entered = threading.Event()
                    release = threading.Event()
                    owner.entered.append(entered)
                    owner.releases.append(release)
                    entered.set()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.flush()
                if not release.wait(timeout=20):
                    return
                response = (
                    b'data: {"choices":[{"delta":{"content":"local answer"},'
                    b'"finish_reason":"stop"}],"usage":{"prompt_tokens":3,'
                    b'"completion_tokens":2,"total_tokens":5}}\n\n'
                    b"data: [DONE]\n\n"
                )
                try:
                    self.wfile.write(response)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"

    def wait_entered(self, index: int, timeout: float = 10.0) -> bool:
        """Wait for admission as well as the actual request, without a race."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                event = self.entered[index] if index < len(self.entered) else None
            if event is not None:
                return event.wait(max(0, deadline - time.monotonic()))
            threading.Event().wait(0.02)
        return False

    def release(self, index: int) -> None:
        with self.lock:
            event = self.releases[index] if index < len(self.releases) else None
        if event is not None:
            event.set()

    def close(self) -> None:
        with self.lock:
            releases = list(self.releases)
        for release in releases:
            release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def local_openrouter():
    """Start/stop an isolated real local HTTP provider."""
    server = LocalOpenRouter()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def provider_member(organization):
    """Create an authorized organization member."""
    user = get_user_model().objects.create_user(
        email=f"runtime-{uuid.uuid4().hex[:10]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=user, organization=organization, role=MembershipRole.MEMBER
    )
    return user


def _account(org, user, local_openrouter, *, label: str, key: str):
    return ProviderAccountService().save_account(
        organization_id=org.id,
        user=user,
        provider="openrouter",
        payload={"label": label, "api_key": key, "base_url": local_openrouter.base_url},
    )


async def _wait_run(service: HarnessService, session_id: uuid.UUID) -> None:
    task = service._tasks.get(str(session_id))
    if task is not None:
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_http_sse_selection_is_frozen_then_next_turn_uses_new_account(
    harness_workspace, provider_member, local_openrouter
) -> None:
    """A real request holds A while selected B is only used by the next turn."""
    org = harness_workspace.runner.organization
    service_accounts = ProviderAccountService()
    first = _account(
        org, provider_member, local_openrouter, label="First", key="first-key"
    )
    second = _account(
        org, provider_member, local_openrouter, label="Second", key="second-key"
    )
    service_accounts.select_account(org.id, provider_member.id, "openrouter", first.id)

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org.id,
        model="openrouter/acme/test-model",
        title="Provider snapshot",
    )
    service = HarnessService()
    answer1 = await service.start_run(
        session,
        "first",
        organization_id=org.id,
        workspace_id=str(harness_workspace.id),
        user_id=provider_member.id,
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 0)
    await sync_to_async(service_accounts.select_account)(
        org.id, provider_member.id, "openrouter", second.id
    )
    local_openrouter.release(0)
    await _wait_run(service, session.id)

    answer2 = await service.start_run(
        session,
        "second",
        organization_id=org.id,
        workspace_id=str(harness_workspace.id),
        user_id=provider_member.id,
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 1)
    local_openrouter.release(1)
    await _wait_run(service, session.id)

    messages = [
        await sync_to_async(HarnessMessageRepository.get_by_id)(answer1.id),
        await sync_to_async(HarnessMessageRepository.get_by_id)(answer2.id),
    ]
    assert [message.finish for message in messages] == ["stop", "stop"]
    assert [message.content for message in messages] == ["local answer", "local answer"]
    assert [request["authorization"] for request in local_openrouter.requests] == [
        "Bearer first-key",
        "Bearer second-key",
    ]
    snapshots = [
        await sync_to_async(ProviderTurn.objects.get)(assistant_message_id=answer.id)
        for answer in (answer1, answer2)
    ]
    assert snapshots[0].account_ids == {"openrouter": str(first.id)}
    assert snapshots[1].account_ids == {"openrouter": str(second.id)}
    assert "first-key" not in repr(snapshots[0].account_ids)


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_http_sse_revocation_aborts_blocked_turn_without_account_fallback(
    harness_workspace, provider_member, local_openrouter
) -> None:
    """Deleting the active account stops the running request, never retries on B."""
    org = harness_workspace.runner.organization
    service_accounts = ProviderAccountService()
    selected = _account(
        org, provider_member, local_openrouter, label="Selected", key="selected-key"
    )
    _fallback = _account(
        org, provider_member, local_openrouter, label="Backup", key="backup-key"
    )
    service_accounts.select_account(
        org.id, provider_member.id, "openrouter", selected.id
    )

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org.id,
        model="openrouter/acme/test-model",
        title="Revocation e2e",
    )
    service = HarnessService()
    answer = await service.start_run(
        session,
        "block",
        organization_id=org.id,
        workspace_id=str(harness_workspace.id),
        user_id=provider_member.id,
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 0)
    await sync_to_async(service_accounts.delete_account)(
        org.id, provider_member.id, selected.id
    )
    await _wait_run(service, session.id)
    local_openrouter.release(0)

    fresh = await sync_to_async(HarnessMessageRepository.get_by_id)(answer.id)
    assert fresh is not None and fresh.finish == "aborted"
    assert "removed or is no longer shared" in fresh.error
    assert len(local_openrouter.requests) == 1
    assert local_openrouter.requests[0]["authorization"] == "Bearer selected-key"
    turn = await sync_to_async(ProviderTurn.objects.get)(assistant_message_id=answer.id)
    assert turn.cancel_requested is True
    assert turn.account_ids == {"openrouter": str(selected.id)}
    assert turn.active is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_membership_removal_aborts_personal_account_response(
    harness_workspace, provider_member, local_openrouter
):
    """An in-flight personal account request cannot outlive org membership."""
    org = harness_workspace.runner.organization
    accounts = ProviderAccountService()
    selected = _account(
        org, provider_member, local_openrouter, label="Personal", key="personal-key"
    )
    accounts.select_account(org.id, provider_member.id, "openrouter", selected.id)

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org.id,
        model="openrouter/acme/test-model",
        title="Membership revocation",
    )
    service = HarnessService()
    assistant = await service.start_run(
        session, "wait for membership revocation", user_id=provider_member.id
    )
    assert await asyncio.to_thread(local_openrouter.wait_entered, 0)

    membership = await sync_to_async(Membership.objects.get)(
        user=provider_member, organization=org
    )
    await sync_to_async(membership.delete)()
    await asyncio.wait_for(_wait_run(service, session.id), timeout=8)
    local_openrouter.release(0)

    fresh = await sync_to_async(HarnessMessageRepository.get_by_id)(assistant.id)
    assert fresh.finish == "aborted"
    assert "removed or is no longer shared" in fresh.error
    assert len(local_openrouter.requests) == 1
    turn = await sync_to_async(ProviderTurn.objects.get)(
        assistant_message_id=assistant.id
    )
    assert turn.cancel_requested is True
    assert turn.revoked_account_id == selected.id
    assert turn.active is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_unshare_aborts_members_live_response_and_keeps_admin_account(
    harness_workspace, provider_member, local_openrouter
):
    """Unsharing cancels the member's network stream and clears only their access."""
    org = harness_workspace.runner.organization
    admin = get_user_model().objects.create_user(
        email=f"share-{uuid.uuid4()}@example.test"
    )
    Membership.objects.create(user=admin, organization=org, role=MembershipRole.ADMIN)
    accounts = ProviderAccountService()
    shared = accounts.save_account(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        payload={
            "label": "Team",
            "shared": True,
            "api_key": "team-key",
            "base_url": local_openrouter.base_url,
        },
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org.id,
        model="openrouter/acme/test-model",
        title="Shared test",
    )
    service = HarnessService()
    assistant = await service.start_run(
        session, "wait for revoke", user_id=provider_member.id
    )
    for _ in range(100):
        if local_openrouter.requests:
            break
        await asyncio.sleep(0.05)
    assert local_openrouter.requests
    await sync_to_async(accounts.save_account)(
        organization_id=org.id,
        user=admin,
        provider="openrouter",
        account_id=shared.id,
        payload={"shared": False},
    )
    await asyncio.wait_for(_wait_run(service, session.id), timeout=8)
    local_openrouter.release(0)
    fresh = await sync_to_async(HarnessMessageRepository.get_by_id)(assistant.id)
    assert fresh.finish == "aborted"
    assert "no longer shared" in fresh.error
    assert accounts.selected_accounts(org.id, provider_member.id) == {}
    assert accounts.get_account(org.id, admin.id, shared.id).user_id == admin.id


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("follower", ["title", "summary"])
async def test_follower_inherits_snapshot_and_remains_revocable_after_root_finishes(
    harness_workspace, provider_member, local_openrouter, monkeypatch, follower
):
    """Background titles/recaps retain the answer's accounts and revocation."""
    from apps.harness.models import ProviderTurn
    from apps.harness.providers.base import Delta, ProviderAdapter
    from apps.harness.services import ProviderConfigService

    org = harness_workspace.runner.organization
    accounts = ProviderAccountService()
    account = _account(
        org, provider_member, local_openrouter, label="Initial", key="initial-key"
    )
    other = _account(
        org, provider_member, local_openrouter, label="Other", key="other-key"
    )
    ProviderConfigService().save_config(
        organization_id=org.id, small_model="openrouter/acme/test-model"
    )
    entered = asyncio.Event()
    observed = {}
    service = HarnessService()

    async def generate_follower(**kwargs):
        observed.update(kwargs)
        await service._register_provider_use(
            kwargs["provider_turn_id"], kwargs["account_ids"]["openrouter"]
        )
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service.metadata, f"generate_{follower}", generate_follower)

    # Isolate the follower under test so it is the only remaining snapshot holder.
    async def no_follower(*_args, **_kwargs):
        pass

    if follower == "title":
        monkeypatch.setattr(service, "_schedule_summary", no_follower)
    else:
        monkeypatch.setattr(service.metadata, "generate_title", no_follower)

    class Provider(ProviderAdapter):
        name = "local"

        async def chat_stream(self, *args, **kwargs):
            yield Delta(text="finished")

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org.id,
        model="openrouter/acme/test-model",
    )
    answer = await service.start_run(
        session, "answer", user_id=provider_member.id, provider=Provider()
    )
    await asyncio.wait_for(entered.wait(), timeout=8)
    await _wait_run(service, session.id)
    accounts.select_account(org.id, provider_member.id, "openrouter", other.id)
    assert observed["account_ids"]["openrouter"] == str(account.id)
    turn = ProviderTurn.objects.get(assistant_message_id=answer.id)
    assert turn.active and turn.holds == 1
    await sync_to_async(accounts.delete_account)(org.id, provider_member.id, account.id)
    for _ in range(100):
        turn.refresh_from_db()
        if not turn.active and not service._provider_turn_roots:
            break
        await asyncio.sleep(0.03)
    assert not turn.active and turn.cancel_requested
    assert not service._provider_turn_roots


@pytest.mark.django_db(transaction=True)
async def test_persistent_subagent_inherits_root_accounts_after_settings_change(
    harness_workspace, provider_member, local_openrouter
):
    """A child admitted later in the same answer never reads the new selection."""
    import json

    from apps.harness.models import HarnessSession
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.permissions.service import PermissionService
    from apps.harness.providers.base import Delta, ProviderAdapter

    org = harness_workspace.runner.organization
    accounts = ProviderAccountService()
    first = _account(
        org, provider_member, local_openrouter, label="Root", key="root-key"
    )
    second = _account(
        org, provider_member, local_openrouter, label="Next", key="next-key"
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class DelegatingProvider(ProviderAdapter):
        name = "local"

        async def chat_stream(self, model, messages, tools, opts=None):
            prompt = next(m.content for m in reversed(messages) if m.role == "user")
            if prompt == "parent" and not any(m.role == "tool" for m in messages):
                entered.set()
                await release.wait()
                yield Delta(
                    tool_calls=(
                        {
                            "index": 0,
                            "id": "child",
                            "name": "task",
                            "arguments": json.dumps(
                                {
                                    "description": "Inspect",
                                    "prompt": "child",
                                    "subagent_type": "explore",
                                }
                            ),
                        },
                    )
                )
            else:
                yield Delta(text="done")

    provider = DelegatingProvider()
    service = HarnessService(
        provider_factory=lambda _org: provider,
        permissions=PermissionService(
            evaluator=PermissionEvaluator(global_rules={"*": "allow"})
        ),
    )
    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=org.id,
        model="openrouter/acme/test-model",
    )
    root = await service.start_run(session, "parent", user_id=provider_member.id)
    await asyncio.wait_for(entered.wait(), timeout=8)
    await sync_to_async(accounts.select_account)(
        org.id, provider_member.id, "openrouter", second.id
    )
    release.set()
    await asyncio.wait_for(_wait_run(service, session.id), timeout=10)
    children = list(HarnessSession.objects.filter(parent_id=session.id))
    assert len(children) == 1
    assistant = HarnessMessageRepository.list_for_session(children[0].id)[-1]
    assert assistant.finish == "stop"
    root_turn = ProviderTurn.objects.get(assistant_message_id=root.id)
    assert root_turn.account_ids == {"openrouter": str(first.id)}
    for _ in range(100):
        root_turn.refresh_from_db()
        if not root_turn.active:
            break
        await asyncio.sleep(0.03)
    assert root_turn.holds == 0 and not root_turn.active
