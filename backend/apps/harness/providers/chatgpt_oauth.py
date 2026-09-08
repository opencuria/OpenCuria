"""
OAuth device-code flow and token management for ChatGPT Codex.

Replicates OpenCode's headless Codex authorization: device code polling,
authorization-code exchange, refresh, and JWT claim extraction.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from .base import ProviderError, ProviderTimeoutError

log = structlog.get_logger(__name__)

CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
ISSUER = "https://auth.openai.com"
VERIFICATION_URL = "https://auth.openai.com/codex/device"
CODEX_API_ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
REDIRECT_URI = "https://auth.openai.com/deviceauth/callback"
OAUTH_POLLING_SAFETY_MARGIN_SECONDS = 3.0
USER_AGENT = "opencuria"

DEFAULT_DEVICE_FLOW_TIMEOUT_SECONDS = 300.0


class OAuthError(RuntimeError):
    """Raised for OAuth flow failures (timeout, denial, unexpected state)."""


@dataclass(frozen=True)
class DeviceFlowStart:
    """Result of initiating the device authorization flow."""

    device_auth_id: str
    user_code: str
    verification_url: str
    interval: int


@dataclass(frozen=True)
class OAuthTokens:
    """OAuth tokens and metadata extracted from the token response."""

    access: str
    refresh: str
    expires: int
    account_id: str | None
    residency: str | None


def _http_error(
    message: str,
    response: httpx.Response,
    *,
    exc: Exception | None = None,
) -> None:
    """Raise :class:`ProviderError` from a non-success HTTP response."""
    detail = response.text[:500] if response.text else ""
    body = f"{message} ({response.status_code}): {detail}".strip()
    if exc is not None:
        raise ProviderError(body) from exc
    raise ProviderError(body)


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json_body: dict[str, Any] | None = None,
    form_body: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Send a request and return the parsed JSON body or raise."""
    headers: dict[str, str] = {"User-Agent": USER_AGENT}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        response = await client.request(method, url, headers=headers, json=json_body)
    else:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        response = await client.request(method, url, headers=headers, data=form_body)
    if not response.is_success:
        raise _http_error(f"OAuth request failed for {url}", response)
    data = response.json()
    if not isinstance(data, dict):
        raise ProviderError(f"Unexpected OAuth response type: {type(data).__name__}")
    return data


def parse_jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode JWT payload claims without signature verification."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(payload.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def extract_account_id_from_claims(claims: dict[str, Any]) -> str | None:
    """Return ChatGPT account id from JWT claims."""
    root = claims.get("chatgpt_account_id")
    if isinstance(root, str) and root:
        return root
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        nested = auth.get("chatgpt_account_id")
        if isinstance(nested, str) and nested:
            return nested
    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, dict):
            org_id = first.get("id")
            if isinstance(org_id, str) and org_id:
                return org_id
    return None


def extract_account_id(
    *,
    id_token: str | None = None,
    access_token: str | None = None,
) -> str | None:
    """Extract account id from id_token first, then access_token."""
    if id_token:
        claims = parse_jwt_claims(id_token)
        if claims:
            account_id = extract_account_id_from_claims(claims)
            if account_id:
                return account_id
    if access_token:
        claims = parse_jwt_claims(access_token)
        if claims:
            return extract_account_id_from_claims(claims)
    return None


def extract_residency(token: str) -> str | None:
    """Return compute residency from JWT claims, ignoring no_constraint."""
    claims = parse_jwt_claims(token)
    if not claims:
        return None
    auth = claims.get("https://api.openai.com/auth")
    residency: str | None = None
    if isinstance(auth, dict):
        value = auth.get("chatgpt_compute_residency")
        if isinstance(value, str):
            residency = value
    if residency is None:
        root = claims.get("chatgpt_compute_residency")
        if isinstance(root, str):
            residency = root
    if not residency or residency == "no_constraint":
        return None
    return residency


def _tokens_from_response(data: dict[str, Any]) -> OAuthTokens:
    """Build :class:`OAuthTokens` from an OAuth token endpoint response."""
    access = str(data.get("access_token", "") or "")
    refresh = str(data.get("refresh_token", "") or "")
    if not access or not refresh:
        raise ProviderError("OAuth token response missing access or refresh token")
    expires_in = int(data.get("expires_in") or 3600)
    expires = int(time.time() * 1000) + expires_in * 1000
    id_token = str(data.get("id_token", "") or "") or None
    account_id = extract_account_id(id_token=id_token, access_token=access)
    residency = extract_residency(access) or extract_residency(id_token or "")
    return OAuthTokens(
        access=access,
        refresh=refresh,
        expires=expires,
        account_id=account_id,
        residency=residency,
    )


async def start_device_flow(
    client: httpx.AsyncClient | None = None,
) -> DeviceFlowStart:
    """Start the headless device authorization flow.

    POST ``{ISSUER}/api/accounts/deviceauth/usercode`` with the client id.

    Returns:
        Device flow metadata including the user-facing verification code.
    """
    url = f"{ISSUER}/api/accounts/deviceauth/usercode"
    owns_client = client is None
    http = client or httpx.AsyncClient()
    try:
        data = await _request_json(
            http,
            "POST",
            url,
            json_body={"client_id": CLIENT_ID},
        )
    finally:
        if owns_client:
            await http.aclose()
    device_auth_id = str(data.get("device_auth_id", "") or "")
    user_code = str(data.get("user_code", "") or "")
    if not device_auth_id or not user_code:
        raise ProviderError("Device authorization response missing required fields")
    raw_interval = data.get("interval", "5")
    try:
        interval = max(int(str(raw_interval)), 1)
    except ValueError:
        interval = 5
    return DeviceFlowStart(
        device_auth_id=device_auth_id,
        user_code=user_code,
        verification_url=VERIFICATION_URL,
        interval=interval,
    )


async def exchange_code(
    code: str,
    code_verifier: str,
    client: httpx.AsyncClient | None = None,
) -> OAuthTokens:
    """Exchange an authorization code for OAuth tokens."""
    url = f"{ISSUER}/oauth/token"
    owns_client = client is None
    http = client or httpx.AsyncClient()
    try:
        data = await _request_json(
            http,
            "POST",
            url,
            form_body={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
                "code_verifier": code_verifier,
            },
        )
    finally:
        if owns_client:
            await http.aclose()
    return _tokens_from_response(data)


async def refresh_tokens(
    refresh_token: str,
    client: httpx.AsyncClient | None = None,
) -> OAuthTokens:
    """Refresh OAuth tokens using a refresh token."""
    url = f"{ISSUER}/oauth/token"
    owns_client = client is None
    http = client or httpx.AsyncClient()
    try:
        data = await _request_json(
            http,
            "POST",
            url,
            form_body={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
        )
    finally:
        if owns_client:
            await http.aclose()
    return _tokens_from_response(data)


@dataclass(frozen=True)
class DeviceFlowPollResult:
    """Result of a single non-blocking device-flow poll attempt."""

    status: str  # pending | complete | expired | denied
    tokens: OAuthTokens | None = None


async def poll_device_flow_once(
    device_auth_id: str,
    user_code: str,
    client: httpx.AsyncClient | None = None,
) -> DeviceFlowPollResult:
    """Perform one device-authorization poll (no sleep/retry loop).

    Returns ``pending`` on HTTP 403/404 (user has not completed auth yet),
    ``complete`` with tokens on success, or ``expired``/``denied`` for
    terminal failure responses.
    """
    url = f"{ISSUER}/api/accounts/deviceauth/token"
    owns_client = client is None
    http = client or httpx.AsyncClient()
    try:
        response = await http.post(
            url,
            headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            json={
                "device_auth_id": device_auth_id,
                "user_code": user_code,
            },
        )
        if response.is_success:
            data = response.json()
            if not isinstance(data, dict):
                raise OAuthError("Unexpected device token response type")
            code = str(data.get("authorization_code", "") or "")
            verifier = str(data.get("code_verifier", "") or "")
            if not code or not verifier:
                raise OAuthError("Device token response missing code fields")
            tokens = await exchange_code(code, verifier, client=http)
            return DeviceFlowPollResult(status="complete", tokens=tokens)
        if response.status_code in (403, 404):
            return DeviceFlowPollResult(status="pending")
        if response.status_code in (400, 410):
            return DeviceFlowPollResult(status="expired")
        if response.status_code in (401, 429):
            return DeviceFlowPollResult(status="denied")
        detail = response.text[:500] if response.text else ""
        raise OAuthError(
            f"Device authorization failed ({response.status_code}): {detail}"
        )
    finally:
        if owns_client:
            await http.aclose()


async def poll_device_flow(
    device_auth_id: str,
    user_code: str,
    interval: int,
    timeout: float = DEFAULT_DEVICE_FLOW_TIMEOUT_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> OAuthTokens:
    """Poll until the user completes device authorization or *timeout* expires.

    On HTTP 403/404 the poll continues every ``interval`` plus a safety margin.
    Other non-success statuses raise :class:`OAuthError`.
    """
    url = f"{ISSUER}/api/accounts/deviceauth/token"
    owns_client = client is None
    http = client or httpx.AsyncClient()
    deadline = time.monotonic() + timeout
    poll_interval = max(interval, 1) + OAUTH_POLLING_SAFETY_MARGIN_SECONDS
    try:
        while True:
            if time.monotonic() >= deadline:
                raise ProviderTimeoutError(
                    "Device authorization timed out",
                    provider="chatgpt",
                )
            response = await http.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
                json={
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                },
            )
            if response.is_success:
                data = response.json()
                if not isinstance(data, dict):
                    raise OAuthError("Unexpected device token response type")
                code = str(data.get("authorization_code", "") or "")
                verifier = str(data.get("code_verifier", "") or "")
                if not code or not verifier:
                    raise OAuthError("Device token response missing code fields")
                return await exchange_code(code, verifier, client=http)
            if response.status_code in (403, 404):
                await asyncio.sleep(poll_interval)
                continue
            detail = response.text[:500] if response.text else ""
            raise OAuthError(
                f"Device authorization failed ({response.status_code}): {detail}"
            )
    finally:
        if owns_client:
            await http.aclose()
