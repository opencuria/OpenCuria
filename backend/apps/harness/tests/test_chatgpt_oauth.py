"""Tests for ChatGPT OAuth helpers (mocked httpx)."""

from __future__ import annotations

import base64
import json
import time

import httpx
import pytest

from apps.harness.providers.chatgpt_oauth import (
    CLIENT_ID,
    ISSUER,
    VERIFICATION_URL,
    OAuthError,
    exchange_code,
    extract_account_id,
    extract_account_id_from_claims,
    extract_residency,
    parse_jwt_claims,
    poll_device_flow_once,
    refresh_tokens,
    start_device_flow,
)


def _jwt(payload: dict) -> str:
    header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode())
        .decode()
        .rstrip("=")
    )
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"{header}.{body}.sig"


def _token_response(
    *,
    access_payload: dict | None = None,
    refresh: str = "refresh-token",
) -> dict:
    access = _jwt(access_payload or {"chatgpt_account_id": "acc-123"})
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 3600,
        "id_token": _jwt({"chatgpt_account_id": "acc-123"}),
    }


async def test_start_device_flow_happy_path() -> None:
    """Device flow start returns codes and verification URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{ISSUER}/api/accounts/deviceauth/usercode"
        assert request.method == "POST"
        body = json.loads(request.content.decode())
        assert body == {"client_id": CLIENT_ID}
        return httpx.Response(
            200,
            json={
                "device_auth_id": "auth-1",
                "user_code": "ABCD-1234",
                "interval": "5",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await start_device_flow(client=client)
    assert result.device_auth_id == "auth-1"
    assert result.user_code == "ABCD-1234"
    assert result.verification_url == VERIFICATION_URL
    assert result.interval == 5


async def test_poll_device_flow_happy_path() -> None:
    """Each request polls once; completion exchanges the code for tokens."""
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.url.path == "/api/accounts/deviceauth/token":
            poll_count += 1
            if poll_count < 2:
                return httpx.Response(403)
            return httpx.Response(
                200,
                json={
                    "authorization_code": "auth-code",
                    "code_verifier": "verifier",
                },
            )
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json=_token_response())
        raise AssertionError(f"unexpected url {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pending = await poll_device_flow_once("auth-1", "ABCD-1234", client=client)
    assert pending.status == "pending" and pending.tokens is None
    assert poll_count == 1
    result = await poll_device_flow_once("auth-1", "ABCD-1234", client=client)
    assert poll_count == 2
    assert result.status == "complete"
    tokens = result.tokens
    assert tokens is not None
    assert tokens.access
    assert tokens.refresh == "refresh-token"
    assert tokens.account_id == "acc-123"
    assert tokens.expires > int(time.time() * 1000)


@pytest.mark.parametrize(
    ("status_code", "status"),
    [
        (403, "pending"),
        (404, "pending"),
        (400, "expired"),
        (410, "expired"),
        (401, "denied"),
        (429, "denied"),
    ],
)
async def test_poll_device_flow_status(status_code: int, status: str) -> None:
    """Pending and terminal responses return without retrying or exchanging."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await poll_device_flow_once("auth-1", "ABCD-1234", client=client)
    assert result.status == status and result.tokens is None
    assert len(requests) == 1


async def test_exchange_code_and_refresh_tokens() -> None:
    """Code exchange and refresh return OAuthTokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/token"
        body = request.content.decode()
        if "authorization_code" in body:
            assert "code_verifier=verifier" in body
            assert f"client_id={CLIENT_ID}" in body
        if "refresh_token" in body:
            assert "refresh-old" in body
        return httpx.Response(
            200,
            json=_token_response(
                access_payload={
                    "https://api.openai.com/auth": {
                        "chatgpt_compute_residency": "eu",
                    }
                },
                refresh="refresh-new",
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    exchanged = await exchange_code("code", "verifier", client=client)
    assert exchanged.account_id == "acc-123"
    assert exchanged.residency == "eu"

    refreshed = await refresh_tokens("refresh-old", client=client)
    assert refreshed.refresh == "refresh-new"
    assert refreshed.residency == "eu"


def test_parse_jwt_claims_variants() -> None:
    """JWT helpers handle valid, invalid, and nested claim shapes."""
    token = _jwt(
        {
            "chatgpt_account_id": "root-id",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "nested-id",
                "chatgpt_compute_residency": "no_constraint",
            },
            "organizations": [{"id": "org-id"}],
        }
    )
    claims = parse_jwt_claims(token)
    assert claims is not None
    assert extract_account_id_from_claims(claims) == "root-id"
    assert extract_account_id(access_token=token) == "root-id"
    assert extract_residency(token) is None

    nested = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "nested-only",
                "chatgpt_compute_residency": "us",
            }
        }
    )
    assert extract_account_id(id_token=nested) == "nested-only"
    assert extract_residency(nested) == "us"

    org_only = _jwt({"organizations": [{"id": "org-fallback"}]})
    assert (
        extract_account_id_from_claims(parse_jwt_claims(org_only) or {})
        == "org-fallback"
    )

    assert parse_jwt_claims("bad.token") is None
    assert parse_jwt_claims("a.!!!invalid!!!.b") is None


async def test_poll_device_flow_unexpected_status_raises_oauth_error() -> None:
    """Non-pending failures raise OAuthError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(OAuthError, match="500"):
        await poll_device_flow_once("auth-1", "ABCD-1234", client=client)
