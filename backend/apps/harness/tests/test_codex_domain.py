"""Codex account-scoped discovery, immutable routing, children and admission."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.test import Client

from apps.harness.conversation_service import ConversationService
from apps.harness.engines.codex.auth import resolve_codex_auth
from apps.harness.engines.codex.catalog import (
    DEFAULT_CODEX_MODEL,
    chatgpt_models,
    normalize_codex_effort,
    normalize_codex_model,
)
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.engines.runs import HarnessRunService
from apps.harness.engines.service import EngineCatalogService
from apps.harness.harness_service import HarnessService
from apps.harness.model_catalog_service import clear_models_cache
from apps.harness.models import HarnessSession
from apps.harness.provider_accounts import ProviderAccountService
from apps.harness.repositories import HarnessMessageRepository
from apps.harness.session_access_service import SessionAccessService
from apps.organizations.models import Membership
from apps.scheduled_tasks.services import ScheduledTaskService
from common.exceptions import AuthenticationError, NotFoundError


@pytest.fixture
def codex_setup(harness_workspace):
    workspace = harness_workspace
    owner = workspace.created_by
    org = workspace.runner.organization_id
    accounts = ProviderAccountService()
    account = accounts.save_account(
        organization_id=org,
        user=owner,
        provider="chatgpt",
        payload={
            "label": "Subscription",
            "access": "access-secret",
            "refresh": "refresh-secret",
            "account_id": "chatgpt-account",
            "expires": int(time.time() * 1000) + 3600_000,
        },
    )
    service = HarnessService()
    service.conversation_events.emit_sync = lambda *_: None
    clear_models_cache()
    return SimpleNamespace(
        workspace=workspace,
        org=org,
        owner=owner,
        account=account,
        accounts=accounts,
        service=service,
    )


def create_session(data, **kwargs):
    return data.service.create_session(
        workspace_id=data.workspace.id,
        organization_id=data.org,
        user_id=data.owner.id,
        prompt="Review this repository",
        harness_id="codex",
        **kwargs,
    )


def test_pinned_default_and_per_model_ultra_allowlist():
    specs = {m.id: m for m in chatgpt_models()}
    assert DEFAULT_CODEX_MODEL == "chatgpt/gpt-6.1-sol"
    assert normalize_codex_effort("ultra", specs[DEFAULT_CODEX_MODEL]) == "ultra"
    assert normalize_codex_effort("", specs[DEFAULT_CODEX_MODEL]) == "high"
    assert normalize_codex_effort("low", specs[DEFAULT_CODEX_MODEL]) == "low"
    assert normalize_codex_model("chatgpt/gpt-6.1-terra") == "chatgpt/gpt-6.1-terra"
    assert normalize_codex_effort("ultra", specs["chatgpt/gpt-6.1-terra"]) == "ultra"
    assert all(model.default_effort == "high" for model in specs.values())
    for model in ("chatgpt/gpt-6-luna", "chatgpt/gpt-5.6-luna", "chatgpt/gpt-5.5"):
        with pytest.raises(ValueError, match="Invalid Codex reasoning effort"):
            normalize_codex_effort("ultra", specs[model])
    for model in (
        "openai/gpt-6.1-sol",
        "gpt-6.1-sol",
        "openai-compatible/test",
        "chatgpt/invented",
    ):
        with pytest.raises(ValueError):
            normalize_codex_model(model)


@pytest.mark.django_db(transaction=True)
def test_discovery_uses_account_scoped_catalog(codex_setup):
    data = codex_setup
    from apps.accounts.models import APIKey, APIKeyPermission
    from common.utils import generate_api_token, hash_token

    token = generate_api_token()
    APIKey.objects.create(
        user=data.owner,
        name="Codex test",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    client = Client(HTTP_X_ORGANIZATION_ID=str(data.org), HTTP_X_API_KEY=token)
    response = client.get("/api/v1/harness/engines/codex/models/")
    assert response.status_code == 200, response.content
    assert {m["id"] for m in response.json()} == {m.id for m in chatgpt_models()}
    engines = client.get("/api/v1/harness/engines/").json()
    assert engines[-1]["connected"] is True
    assert client.get("/api/v1/harness/engines/unknown/models/").status_code == 404
    assert "access-secret" not in response.content.decode()
    data.accounts.select_account(data.org, data.owner.id, "chatgpt", None)
    assert client.get("/api/v1/harness/engines/codex/models/").json() == []
    assert client.get("/api/v1/harness/engines/").json()[-1]["connected"] is False


@pytest.mark.django_db(transaction=True)
def test_create_setters_and_direct_run_reject_provider_switch(codex_setup):
    data = codex_setup
    session = create_session(data)
    assert session.harness_id == "codex"
    assert session.engine_state == {"provider_id": "chatgpt"}
    assert session.created_by_id == data.owner.id
    assert session.model == DEFAULT_CODEX_MODEL
    assert session.reasoning_effort == "high"
    data.service.session_settings.set_reasoning_effort(session.id, "ultra")
    data.service.session_settings.set_model(session.id, "chatgpt/gpt-6-astra")
    for invalid in (
        "openrouter/openai/test",
        "openai-compatible/test",
        "chatgpt/invented",
    ):
        with pytest.raises(ValueError):
            data.service.session_settings.set_model(session.id, invalid)
    # Direct mutation followed by admission cannot bypass immutable state binding.
    session.model = "openrouter/openai/test"
    with pytest.raises(ValueError, match="provider cannot be changed"):
        data.service.validate_provider_for_run(data.org, session, user_id=data.owner.id)
    session.refresh_from_db()
    assert session.model == "chatgpt/gpt-6-astra"
    assert session.reasoning_effort == "ultra"
    updated = data.service.session_settings.set_model(session.id, "chatgpt/gpt-6-luna")
    assert updated.reasoning_effort == "high"
    with pytest.raises(ValueError, match="selected provider account"):
        create_session(data, connection_id=data.account.id)


@pytest.mark.django_db(transaction=True)
async def test_edit_rejects_cross_provider_before_timeline_mutation(codex_setup):
    data = codex_setup
    session = await sync_to_async(create_session)(data)
    message = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="user", content="original"
    )
    with pytest.raises(ValueError, match="provider cannot be changed"):
        await data.service.edit_user_message(
            session.id,
            message_id=message.id,
            prompt="edited",
            model="openrouter/openai/test",
            user_id=data.owner.id,
        )
    await sync_to_async(message.refresh_from_db)()
    assert message.content == "original"
    assert await sync_to_async(HarnessMessageRepository.list_for_session)(
        session.id
    ) == [message]


@pytest.mark.django_db(transaction=True)
def test_auth_snapshot_acl_explicit_empty_and_secrets(codex_setup):
    data = codex_setup
    snapshot = {"chatgpt": str(data.account.id)}
    data.accounts.select_account(data.org, data.owner.id, "chatgpt", None)
    auth = resolve_codex_auth(data.org, data.owner.id, snapshot, DEFAULT_CODEX_MODEL)
    assert auth.connection_id == data.account.id
    assert auth.secret_env == {}
    assert auth.login_data == {
        "type": "chatgptAuthTokens",
        "accessToken": "access-secret",
        "chatgptAccountId": "chatgpt-account",
    }
    assert "secret" not in repr(auth)
    with pytest.raises(AuthenticationError):
        resolve_codex_auth(data.org, data.owner.id, {}, DEFAULT_CODEX_MODEL)
    with pytest.raises(NotFoundError):
        resolve_codex_auth(data.org, data.owner.id, None, DEFAULT_CODEX_MODEL)
    stranger = get_user_model().objects.create_user(email=f"{uuid.uuid4()}@test.com")
    Membership.objects.create(user=stranger, organization_id=data.org)
    with pytest.raises(NotFoundError):
        resolve_codex_auth(data.org, stranger.id, snapshot, DEFAULT_CODEX_MODEL)
    session = create_session_after_reselect(data)
    with pytest.raises(AuthenticationError):
        SessionAccessService.require_external_owner(
            engine_id=session.harness_id,
            owner_id=session.created_by_id,
            user_id=stranger.id,
        )


def create_session_after_reselect(data):
    data.accounts.select_account(data.org, data.owner.id, "chatgpt", data.account.id)
    return create_session(data)


@pytest.mark.django_db(transaction=True)
def test_children_use_parent_owner_provider_and_captured_account(codex_setup):
    data = codex_setup
    root = create_session(data)
    data.service._runs[str(root.id)] = {
        "provider_turn_id": str(uuid.uuid4()),
        "initiated_by_id": data.owner.id,
        "account_ids": {"chatgpt": str(data.account.id)},
    }
    data.accounts.select_account(data.org, data.owner.id, "chatgpt", None)
    child = create_session(data, parent_id=root.id, agent_name="explore")
    assert child.harness_id == "codex"
    assert child.agent_name == "explore"
    assert child.created_by_id == root.created_by_id
    assert child.model == root.model
    assert child.engine_state == root.engine_state
    with pytest.raises(ValueError, match="provider cannot be changed"):
        create_session(data, parent_id=root.id, model="openrouter/openai/test")
    computer = create_session(data, parent_id=root.id, agent_name="computeruse")
    assert computer.harness_id == "codex"
    assert computer.model == root.model
    # Follow-up root validation resolves selection afresh; children stay snapshotted.
    with pytest.raises(AuthenticationError):
        data.service.validate_provider_for_run(data.org, root, user_id=data.owner.id)
    assert (
        data.service.validate_provider_for_run(
            data.org,
            child,
            user_id=data.owner.id,
            account_ids={"chatgpt": str(data.account.id)},
        )
        == root.model
    )


@pytest.mark.django_db(transaction=True)
async def test_fork_and_binding_preserve_provider_and_nonsecret_state(codex_setup):
    data = codex_setup
    root = await sync_to_async(create_session)(data)
    root.engine_state["protocol_version"] = "test"
    await sync_to_async(root.save)()
    fork = await ConversationService().fork_session(root.id)
    assert fork.harness_id == "codex"
    assert fork.external_session_id == ""
    assert fork.engine_state == {"provider_id": "chatgpt"}
    assistant = await sync_to_async(HarnessMessageRepository.create)(
        session_id=root.id, role="assistant", harness_id="codex"
    )
    owner = await sync_to_async(HarnessRunService().create_attempt)(
        session_id=root.id,
        assistant_message_id=assistant.id,
        user_id=data.owner.id,
        harness_id="codex",
    )
    assert await sync_to_async(HarnessEngineRepository.bind_external_session_if_owner)(
        owner.run_id,
        owner.owner_token,
        root.id,
        external_session_id="thread-test",
        engine_state={"cwd": "/workspace"},
    )
    await sync_to_async(root.refresh_from_db)()
    assert root.engine_state == {
        "provider_id": "chatgpt",
        "protocol_version": "test",
        "cwd": "/workspace",
    }
    with pytest.raises(ValueError, match="provider cannot be changed"):
        await sync_to_async(HarnessEngineRepository.bind_external_session_if_owner)(
            owner.run_id,
            owner.owner_token,
            root.id,
            external_session_id="thread-test",
            engine_state={"provider_id": "openrouter"},
        )


@pytest.mark.django_db(transaction=True)
def test_schedule_uses_codex_validation_and_never_persists_account(codex_setup):
    data = codex_setup
    service = ScheduledTaskService()
    fields = service._validate_engine_config(
        organization_id=data.org,
        owner_id=data.owner.id,
        workspace_id=data.workspace.id,
        mode="plan",
        harness_id="codex",
        model="",
        reasoning_effort="ultra",
        skill_ids=[],
    )
    assert fields == ("codex", DEFAULT_CODEX_MODEL, "ultra", [])
    data.accounts.select_account(data.org, data.owner.id, "chatgpt", None)
    with pytest.raises(ValueError, match="Connect your selected"):
        service._validate_engine_config(
            organization_id=data.org,
            owner_id=data.owner.id,
            workspace_id=data.workspace.id,
            mode="build",
            harness_id="codex",
            model="",
            reasoning_effort="",
            skill_ids=[],
        )
    assert HarnessSession.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_openrouter_models_are_normalized_scoped_and_metadata_validated(
    codex_setup, monkeypatch
):
    from apps.harness.providers.models_catalog import normalize_openrouter_catalog

    data = codex_setup
    account = data.accounts.save_account(
        organization_id=data.org,
        user=data.owner,
        provider="openrouter",
        payload={"api_key": "router-secret"},
    )
    fetched = []

    def fetch(**kwargs):
        fetched.append(kwargs["api_key"])
        return normalize_openrouter_catalog(
            {
                "data": [
                    {
                        "id": "vendor/coder",
                        "supported_parameters": ["tools"],
                        "reasoning": {
                            "supported_efforts": ["low", "high", "ultra"],
                            "default_effort": "high",
                        },
                    },
                    {"id": "vendor/no-tools", "supported_parameters": []},
                ]
            }
        )

    monkeypatch.setattr(
        "apps.harness.providers.models_catalog.fetch_openrouter_models", fetch
    )
    service = EngineCatalogService()
    rows = service.list_models("codex", data.org, data.owner.id)
    coder = next(m for m in rows if m.id == "openrouter/vendor/coder")
    assert coder.reasoning_efforts == ("low", "high", "ultra")
    assert "openrouter/vendor/no-tools" not in {m.id for m in rows}
    assert service.validate_codex_config(
        data.org, data.owner.id, coder.id, "ultra"
    ) == (coder.id, "ultra")
    assert fetched == ["router-secret"]  # account+revision-scoped cache hit
    auth = resolve_codex_auth(
        data.org, data.owner.id, {"openrouter": str(account.id)}, coder.id
    )
    assert auth.secret_env == {"OPENCURIA_CODEX_API_KEY": "router-secret"}
    assert auth.login_data == {}
    with pytest.raises(ValueError, match="Invalid Codex reasoning effort"):
        service.validate_codex_config(data.org, data.owner.id, coder.id, "medium")
    data.accounts.select_account(data.org, data.owner.id, "openrouter", None)
    assert all(
        m.provider == "chatgpt"
        for m in service.list_models("codex", data.org, data.owner.id)
    )
    # Unrelated broken router account cannot block ChatGPT admission.
    assert (
        service.validate_codex_config(data.org, data.owner.id, "", "")[0]
        == DEFAULT_CODEX_MODEL
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("engine_id", ["native", "claude", "codex"])
def test_generic_catalog_provider_failures_do_not_leak_secrets(
    codex_setup, monkeypatch, engine_id
):
    from apps.accounts.models import APIKey, APIKeyPermission
    from apps.harness.providers.base import ProviderAuthError
    from common.utils import generate_api_token, hash_token

    data = codex_setup
    token = generate_api_token()
    APIKey.objects.create(
        user=data.owner,
        name="Read models",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[APIKeyPermission.HARNESS_READ.value],
    )
    client = Client(HTTP_X_ORGANIZATION_ID=str(data.org), HTTP_X_API_KEY=token)

    def fail(*args, **kwargs):
        raise ProviderAuthError("upstream echoed access-secret", provider="openrouter")

    monkeypatch.setattr(EngineCatalogService, "list_models", fail)
    response = client.get(f"/api/v1/harness/engines/{engine_id}/models/")
    assert response.status_code == 502
    assert "access-secret" not in response.content.decode()
