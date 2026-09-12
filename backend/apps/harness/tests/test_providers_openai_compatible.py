"""Tests for the generic OpenAI-compatible provider (adapter + wiring)."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from django.contrib.auth import get_user_model

from apps.harness.providers.base import (
    ChatOptions,
    LLMMessage,
    ProviderAuthError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ToolSchema,
)
from apps.harness.providers.model_ref import (
    namespaced_model_id,
    parse_model_ref,
)
from apps.harness.providers.models_catalog import openai_compatible_models
from apps.harness.providers.openai_compatible import OpenAICompatibleAdapter
from apps.harness.services import ProviderConfigService
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token


@pytest.fixture
def provider_setup(db):
    """Org + owner/stranger + runner + owned/foreign workspaces (local copy)."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name=f"Compat API {uuid.uuid4().hex[:6]}",
        slug=f"compat-api-{uuid.uuid4().hex[:10]}",
    )
    owner = user_model.objects.create_user(
        email=f"c-owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"c-stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    Membership.objects.create(
        user=owner, organization=org, role=MembershipRole.MEMBER
    )
    Membership.objects.create(
        user=stranger, organization=org, role=MembershipRole.MEMBER
    )
    runner = Runner.objects.create(
        name="compat-api-runner",
        api_token_hash=hash_token(f"compat-api-{uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"compat-api-{uuid.uuid4().hex[:8]}",
        organization=org,
        available_runtimes=["docker"],
    )
    owned = Workspace.objects.create(
        runner=runner,
        name="Owned",
        status=WorkspaceStatus.RUNNING,
        created_by=owner,
    )
    foreign = Workspace.objects.create(
        runner=runner,
        name="Foreign",
        status=WorkspaceStatus.RUNNING,
        created_by=stranger,
    )
    return {"org": org, "owner": owner, "stranger": stranger,
            "owned": owned, "foreign": foreign}


def _completion_body(text: str = "hello", usage: dict | None = None) -> bytes:
    body: dict = {
        "id": "chatcmpl-1",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text},
             "finish_reason": "stop"}
        ],
    }
    if usage is not None:
        body["usage"] = usage
    return json.dumps(body).encode()


def _mock_client(
    payload: bytes, status_code: int = 200
) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, content=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _messages() -> list[LLMMessage]:
    return [
        LLMMessage(role="system", content="You are helpful."),
        LLMMessage(
            role="user",
            content=[
                {"type": "text", "text": "Click save"},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": "data:image/png;base64,QUJD",
                        "detail": "high",
                    },
                },
            ],
        ),
    ]


@pytest.mark.asyncio
async def test_openai_compatible_payload_text_usage_and_no_temperature() -> None:
    """Payload posts non-streaming; usage maps; None temperature omitted."""
    client, seen = _mock_client(
        _completion_body(
            "done", {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}
        )
    )
    adapter = OpenAICompatibleAdapter(
        base_url="https://grounding.example/v1", client=client
    )
    deltas = [
        delta
        async for delta in adapter.chat_stream("uitars-7b", _messages(), [], None)
    ]
    assert len(deltas) == 1
    assert deltas[0].text == "done"
    assert deltas[0].usage is not None
    assert deltas[0].usage.prompt_tokens == 3
    assert deltas[0].usage.completion_tokens == 4
    assert deltas[0].usage.total_tokens == 7
    assert deltas[0].finish_reason == "stop"

    request = seen[0]
    assert request.url.path == "/v1/chat/completions"
    assert "Authorization" not in request.headers
    sent = json.loads(request.content.decode())
    assert sent["model"] == "uitars-7b"
    assert sent["stream"] is False
    assert "temperature" not in sent
    assert "stream_options" not in sent
    assert sent["messages"][0] == {"role": "system", "content": "You are helpful."}
    user_parts = sent["messages"][1]["content"]
    assert user_parts[0] == {"type": "text", "text": "Click save"}
    assert user_parts[1]["image_url"]["url"] == "data:image/png;base64,QUJD"


@pytest.mark.asyncio
async def test_openai_compatible_temperature_auth_and_tools() -> None:
    """Explicit temperature + api key sent; tools use OpenAI function shape."""
    client, seen = _mock_client(_completion_body("ok"))
    adapter = OpenAICompatibleAdapter(
        base_url="https://grounding.example/v1/",
        api_key="secret-key",
        client=client,
    )
    tools = [
        ToolSchema(
            name="click",
            description="Click",
            parameters={"type": "object", "properties": {"x": {"type": "number"}}},
        )
    ]
    deltas = [
        delta
        async for delta in adapter.chat_stream(
            "uitars-7b",
            [LLMMessage(role="user", content="hi")],
            tools,
            ChatOptions(temperature=0.0),
        )
    ]
    assert deltas[0].text == "ok"
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer secret-key"
    sent = json.loads(request.content.decode())
    assert sent["temperature"] == 0.0
    assert sent["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": "Click",
                "parameters": {
                    "type": "object",
                    "properties": {"x": {"type": "number"}},
                },
            },
        }
    ]


@pytest.mark.asyncio
async def test_openai_compatible_errors() -> None:
    """Auth/rate-limit/HTTP/malformed payloads map to typed errors."""
    client, _ = _mock_client(b"nope", status_code=401)
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    with pytest.raises(ProviderAuthError):
        _ = [
            delta
            async for delta in adapter.chat_stream("m", _messages(), [], None)
        ]

    client, _ = _mock_client(b"slow", status_code=429)
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    with pytest.raises(ProviderRateLimitError):
        _ = [
            delta
            async for delta in adapter.chat_stream("m", _messages(), [], None)
        ]

    client, _ = _mock_client(b"boom", status_code=500)
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    with pytest.raises(ProviderResponseError):
        _ = [
            delta
            async for delta in adapter.chat_stream("m", _messages(), [], None)
        ]

    client, _ = _mock_client(b"not json")
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    with pytest.raises(ProviderResponseError):
        _ = [
            delta
            async for delta in adapter.chat_stream("m", _messages(), [], None)
        ]

    client, _ = _mock_client(json.dumps({"error": {"message": "bad req"}}).encode())
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    with pytest.raises(ProviderResponseError):
        _ = [
            delta
            async for delta in adapter.chat_stream("m", _messages(), [], None)
        ]


@pytest.mark.asyncio
async def test_openai_compatible_timeout() -> None:
    """Transport timeouts surface as ProviderTimeoutError."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    with pytest.raises(ProviderTimeoutError):
        _ = [delta async for delta in adapter.chat_stream("m", _messages(), [], None)]


def test_openai_compatible_requires_base_url() -> None:
    """Empty/schemeless/hostless base_url is rejected at construction."""
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleAdapter(base_url="   ")
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleAdapter(base_url="ftp://x.example/v1")
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleAdapter(base_url="https://")
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleAdapter(base_url="http:///x")
    # localhost/self-hosted stays allowed (admin-configured provider).
    OpenAICompatibleAdapter(base_url="http://localhost:8080/v1")
    OpenAICompatibleAdapter(base_url="http://127.0.0.1:11434")


def test_validate_base_url_reuses_adapter_rule() -> None:
    """Service validation reuses the pure provider helper (single rule)."""
    from apps.harness.providers.openai_compatible import validate_base_url
    from apps.harness.services import ProviderConfigService

    assert (
        ProviderConfigService.validate_openai_compatible_base_url(
            "https://x.example/v1/"
        )
        == "https://x.example/v1"
    )
    assert validate_base_url("http://localhost:8080/v1/") == (
        "http://localhost:8080/v1"
    )
    for bad in ("", "https://", "http:///x", "ftp://x.example/v1"):
        with pytest.raises(ValueError, match="base_url"):
            validate_base_url(bad)
        with pytest.raises(ValueError, match="base_url"):
            ProviderConfigService.validate_openai_compatible_base_url(bad)


@pytest.mark.asyncio
async def test_openai_compatible_bad_response_shapes_rejected() -> None:
    """Malformed choices without usage/finish are rejected, not swallowed."""
    bad_bodies = [
        json.dumps({"choices": []}).encode(),
        json.dumps({"choices": [{"message": {"content": 42}}]}).encode(),
        json.dumps({"choices": [{"message": "not-a-dict"}]}).encode(),
        json.dumps({"choices": "nope"}).encode(),
        json.dumps({"unexpected": True}).encode(),
    ]
    for payload in bad_bodies:
        client, _ = _mock_client(payload)
        adapter = OpenAICompatibleAdapter(
            base_url="https://x.example/v1", client=client
        )
        with pytest.raises(ProviderResponseError):
            _ = [
                delta
                async for delta in adapter.chat_stream("m", _messages(), [], None)
            ]


@pytest.mark.asyncio
async def test_openai_compatible_empty_text_with_usage_accepted() -> None:
    """Empty text + usage stays valid (allowed); tool calls map to Delta."""
    body = json.dumps(
        {
            "id": "chatcmpl-1",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 0,
                "total_tokens": 1,
            },
        }
    ).encode()
    client, _ = _mock_client(body)
    adapter = OpenAICompatibleAdapter(base_url="https://x.example/v1", client=client)
    deltas = [delta async for delta in adapter.chat_stream("m", _messages(), [], None)]
    assert deltas[0].text == ""
    assert deltas[0].usage is not None
    assert deltas[0].usage.total_tokens == 1
    assert deltas[0].usage.cost == 0.0

    tool_body = json.dumps(
        {
            "id": "chatcmpl-2",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "click",
                                    "arguments": '{"x": 1}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
    ).encode()
    tool_client, _ = _mock_client(tool_body)
    tool_adapter = OpenAICompatibleAdapter(
        base_url="https://x.example/v1", client=tool_client
    )
    tool_deltas = [
        delta
        async for delta in tool_adapter.chat_stream("m", _messages(), [], None)
    ]
    assert tool_deltas[0].tool_calls == (
        {"id": "call-1", "name": "click", "arguments": '{"x": 1}'},
    )
    assert tool_deltas[0].finish_reason == "tool_calls"


def test_model_ref_and_catalog_shapes() -> None:
    """Model refs parse; manual catalog ids are namespaced, tools off."""
    assert parse_model_ref("openai-compatible/uitars-7b") == (
        "openai-compatible",
        "uitars-7b",
    )
    assert namespaced_model_id("openai-compatible/uitars-7b") == (
        "openai-compatible/uitars-7b"
    )
    models = openai_compatible_models(["uitars-7b", "uitars-7b ", "  ", "other"])
    assert [model.id for model in models] == [
        "openai-compatible/uitars-7b",
        "openai-compatible/other",
    ]
    assert models[0].name == "uitars-7b"
    assert models[0].provider == "openai-compatible"
    assert models[0].supports_tools is False
    assert models[0].reasoning_efforts == ()
    assert models[0].context_length == 0
    assert models[0].max_output_tokens == 0


@pytest.mark.django_db
def test_openai_compatible_resolver_catalog(organization) -> None:
    """Resolver builds the adapter; catalog exposes manual model ids."""
    service = ProviderConfigService()
    service.save_config(organization_id=organization.id, default_model="m")
    service.save_connection(
        organization_id=organization.id,
        provider="openai-compatible",
        credentials={"api_key": ""},
        config={
            "base_url": "https://grounding.example/v1",
            "models": ["uitars-7b", "uitars-7b"],
        },
    )
    resolver = service.build_resolver(organization.id)
    resolved = resolver.resolve("openai-compatible/uitars-7b")
    assert isinstance(resolved.adapter, OpenAICompatibleAdapter)
    assert resolved.model_id == "uitars-7b"
    assert resolved.provider == "openai-compatible"

    models = service.list_models(organization.id)
    ids = {model.id for model in models}
    assert "openai-compatible/uitars-7b" in ids
    manual = next(
        model for model in models if model.id == "openai-compatible/uitars-7b"
    )
    assert manual.supports_tools is False


@pytest.mark.django_db(transaction=True)
def test_openai_compatible_connection_rest_and_mcp(provider_setup) -> None:
    """REST upsert validates base_url/models; MCP mirrors the same path."""
    import json as _json
    from types import SimpleNamespace

    from django.test import Client as _Client

    from apps.accounts.models import APIKey as _APIKey
    from apps.accounts.models import APIKeyPermission as _Perm
    from apps.mcp_app.server import _call_save_provider_connection

    token = generate_api_token()
    _APIKey.objects.create(
        user=provider_setup["owner"],
        name="compat-rest",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[_Perm.HARNESS_PROVIDERS.value],
    )
    client = _Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(provider_setup["org"].id),
    )
    url = "/api/v1/provider-config/providers/openai-compatible/"

    missing = client.put(
        url, data=_json.dumps({"api_key": "k"}), content_type="application/json"
    )
    assert missing.status_code == 400

    bad_scheme = client.put(
        url,
        data=_json.dumps({"base_url": "ftp://x.example/v1"}),
        content_type="application/json",
    )
    assert bad_scheme.status_code == 400

    created = client.put(
        url,
        data=_json.dumps(
            {
                "base_url": "https://grounding.example/v1",
                "models": ["uitars-7b", "uitars-7b ", "other"],
            }
        ),
        content_type="application/json",
    )
    assert created.status_code == 200, created.content[:500]
    body = created.json()
    assert body["connected"] is True
    assert body["provider"] == "openai-compatible"
    assert body["base_url"] == "https://grounding.example/v1"
    assert body["api_key_hint"] == ""
    assert "secret" not in created.content.decode().lower()

    listed = client.get("/api/v1/provider-config/providers/")
    providers = {row["provider"]: row for row in listed.json()}
    assert "openai-compatible" in providers
    assert providers["openai-compatible"]["connected"] is True

    # Empty api_key on update keeps the previous secret.
    with_key = client.put(
        url,
        data=_json.dumps(
            {
                "api_key": "sk-compat-1",
                "base_url": "https://grounding.example/v1",
            }
        ),
        content_type="application/json",
    )
    assert with_key.status_code == 200
    assert with_key.json()["api_key_hint"] == "••••at-1"
    kept = client.put(
        url,
        data=_json.dumps({"api_key": "", "base_url": "https://grounding.example/v1"}),
        content_type="application/json",
    )
    assert kept.status_code == 200
    assert kept.json()["api_key_hint"] == "••••at-1"

    api_key = SimpleNamespace(user=provider_setup["owner"])
    saved = _call_save_provider_connection(
        api_key,
        provider_setup["org"].id,
        {
            "provider": "openai-compatible",
            "base_url": "https://grounding.example/v1",
            "models": ["mcp-model"],
        },
    )
    payload = _json.loads(saved[0].text)
    assert payload["provider"] == "openai-compatible"
    assert payload["connected"] is True


@pytest.mark.django_db(transaction=True)
def test_openai_compatible_models_dedup_keep_clear(provider_setup) -> None:
    """REST dedups/trims; omitted keeps; explicit [] clears; '' is 400."""
    import json as _json

    from django.test import Client as _Client

    from apps.accounts.models import APIKey as _APIKey
    from apps.accounts.models import APIKeyPermission as _Perm

    token = generate_api_token()
    _APIKey.objects.create(
        user=provider_setup["owner"],
        name="compat-models",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[_Perm.HARNESS_PROVIDERS.value],
    )
    client = _Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(provider_setup["org"].id),
    )
    url = "/api/v1/provider-config/providers/openai-compatible/"

    created = client.put(
        url,
        data=_json.dumps(
            {
                "base_url": "https://grounding.example/v1",
                "models": [" a ", "a", "b"],
            }
        ),
        content_type="application/json",
    )
    assert created.status_code == 200, created.content[:500]
    assert created.json()["models"] == ["a", "b"]

    listed = client.get("/api/v1/provider-config/providers/")
    assert listed.status_code == 200
    rows = {row["provider"]: row for row in listed.json()}
    assert rows["openai-compatible"]["models"] == ["a", "b"]
    assert rows["openai-compatible"]["connected"] is True
    for name in ("openrouter", "chatgpt", "amazon-bedrock"):
        assert rows[name]["connected"] is False
        assert rows[name]["models"] == []

    kept = client.put(
        url,
        data=_json.dumps({"base_url": "https://grounding.example/v1"}),
        content_type="application/json",
    )
    assert kept.status_code == 200, kept.content[:500]
    assert kept.json()["models"] == ["a", "b"]

    cleared = client.put(
        url,
        data=_json.dumps({"base_url": "https://grounding.example/v1", "models": []}),
        content_type="application/json",
    )
    assert cleared.status_code == 200, cleared.content[:500]
    assert cleared.json()["models"] == []

    reseeded = client.put(
        url,
        data=_json.dumps(
            {"base_url": "https://grounding.example/v1", "models": ["a", "b"]}
        ),
        content_type="application/json",
    )
    assert reseeded.status_code == 200
    bad = client.put(
        url,
        data=_json.dumps(
            {"base_url": "https://grounding.example/v1", "models": ["a", ""]}
        ),
        content_type="application/json",
    )
    assert bad.status_code == 400
    assert "models" in bad.json()["detail"]
    current = client.put(
        url,
        data=_json.dumps({"base_url": "https://grounding.example/v1"}),
        content_type="application/json",
    )
    assert current.json()["models"] == ["a", "b"]


@pytest.mark.django_db(transaction=True)
def test_openai_compatible_base_url_and_api_key_keep(provider_setup) -> None:
    """Create needs base_url; update keeps base_url/secret on omitted/empty."""
    import json as _json

    from django.test import Client as _Client

    from apps.accounts.models import APIKey as _APIKey
    from apps.accounts.models import APIKeyPermission as _Perm

    token = generate_api_token()
    _APIKey.objects.create(
        user=provider_setup["owner"],
        name="compat-keep",
        key_hash=hash_token(token),
        key_prefix=token[:12],
        permissions=[_Perm.HARNESS_PROVIDERS.value],
    )
    client = _Client(
        HTTP_X_API_KEY=token,
        HTTP_X_ORGANIZATION_ID=str(provider_setup["org"].id),
    )
    url = "/api/v1/provider-config/providers/openai-compatible/"

    assert (
        client.put(
            url,
            data=_json.dumps({"api_key": "k"}),
            content_type="application/json",
        ).status_code
        == 400
    )
    assert (
        client.put(
            url,
            data=_json.dumps({"api_key": "k", "base_url": ""}),
            content_type="application/json",
        ).status_code
        == 400
    )

    created = client.put(
        url,
        data=_json.dumps(
            {"api_key": "sk-compat-keep", "base_url": "https://keep.example/v1"}
        ),
        content_type="application/json",
    )
    assert created.status_code == 200, created.content[:500]
    assert created.json()["base_url"] == "https://keep.example/v1"
    hint = created.json()["api_key_hint"]
    assert hint == "••••keep"

    omitted = client.put(
        url,
        data=_json.dumps({"api_key": "sk-compat-keep2"}),
        content_type="application/json",
    )
    assert omitted.status_code == 200, omitted.content[:500]
    assert omitted.json()["base_url"] == "https://keep.example/v1"

    blank = client.put(
        url,
        data=_json.dumps({"base_url": "   "}),
        content_type="application/json",
    )
    assert blank.status_code == 200, blank.content[:500]
    assert blank.json()["base_url"] == "https://keep.example/v1"

    kept = client.put(
        url,
        data=_json.dumps({"api_key": ""}),
        content_type="application/json",
    )
    assert kept.status_code == 200, kept.content[:500]
    assert kept.json()["api_key_hint"] == "••••eep2"
    assert kept.json()["base_url"] == "https://keep.example/v1"


@pytest.mark.django_db(transaction=True)
def test_openai_compatible_mcp_models_semantics(provider_setup) -> None:
    """MCP create/omitted/clear matches REST; output carries models."""
    import json as _json
    from types import SimpleNamespace

    from apps.mcp_app.server import _call_save_provider_connection

    api_key = SimpleNamespace(user=provider_setup["owner"])
    org_id = provider_setup["org"].id

    created = _call_save_provider_connection(
        api_key,
        org_id,
        {
            "provider": "openai-compatible",
            "base_url": "https://grounding.example/v1",
            "models": [" a ", "a", "b"],
        },
    )
    assert _json.loads(created[0].text)["models"] == ["a", "b"]

    kept = _call_save_provider_connection(
        api_key,
        org_id,
        {"provider": "openai-compatible", "base_url": "https://grounding.example/v1"},
    )
    assert _json.loads(kept[0].text)["models"] == ["a", "b"]

    cleared = _call_save_provider_connection(
        api_key,
        org_id,
        {
            "provider": "openai-compatible",
            "base_url": "https://grounding.example/v1",
            "models": [],
        },
    )
    assert _json.loads(cleared[0].text)["models"] == []

    _call_save_provider_connection(
        api_key,
        org_id,
        {
            "provider": "openai-compatible",
            "base_url": "https://grounding.example/v1",
            "models": ["a", "b"],
        },
    )
    bad = _call_save_provider_connection(
        api_key,
        org_id,
        {
            "provider": "openai-compatible",
            "base_url": "https://grounding.example/v1",
            "models": ["a", ""],
        },
    )
    assert bad[0].text.startswith("Error:")
    assert "models" in bad[0].text
