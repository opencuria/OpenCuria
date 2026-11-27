"""Signed provider requests and error classification, all mocked."""

from __future__ import annotations

import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from push_proxy.providers import APNsClient, FCMClient


def run(coroutine):
    return asyncio.run(coroutine)


async def apns_send(settings, handler, **overrides):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = APNsClient(settings.apns, http)
        arguments = {
            "token": "ab" * 32,
            "environment": "production",
            "topic": "com.opencuria.mobile",
            "push_type": "alert",
            "priority": 10,
            "expiration": 0,
            "payload": b"{}",
            **overrides,
        }
        return [await client.send(**arguments), await client.send(**arguments)]


def test_apns_signs_with_the_team_key_and_reuses_the_token(settings, provider_keys):
    observed = []

    def handle(request):
        observed.append(request)
        return httpx.Response(200)

    results = run(apns_send(settings, handle))
    assert [result.status for result in results] == ["sent", "sent"]
    authorization = observed[0].headers["authorization"].split(" ")[1]
    key = serialization.load_pem_private_key(provider_keys["apns"].encode(), None)
    claims = jwt.decode(authorization, key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == "TEAM123456"
    assert jwt.get_unverified_header(authorization)["kid"] == "KEY1234567"
    assert observed[1].headers["authorization"] == observed[0].headers["authorization"]
    assert "apns-collapse-id" not in observed[0].headers


def test_apns_refreshes_an_expired_provider_token(settings):
    responses = iter(
        [
            httpx.Response(403, json={"reason": "ExpiredProviderToken"}),
            httpx.Response(200),
        ]
    )
    observed = []

    def handle(request):
        observed.append(request.headers["authorization"])
        return next(responses)

    first, second = run(apns_send(settings, handle))
    assert first.status == "retry"
    assert second.status == "sent"


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (410, "ExpiredToken", "unregistered"),
        (400, "DeviceTokenNotForTopic", "unregistered"),
        (413, "PayloadTooLarge", "failed"),
        (500, "InternalServerError", "retry"),
    ],
)
def test_apns_classification(settings, status, reason, expected):
    result, _ = run(
        apns_send(settings, lambda r: httpx.Response(status, json={"reason": reason}))
    )
    assert result.status == expected


async def fcm_send(settings, handler):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = FCMClient(settings.fcm, http)
        arguments = {
            "token": "fcm",
            "app_id": "com.opencuria.mobile",
            "data": {},
            "ttl": 1,
        }
        return [await client.send(**arguments), await client.send(**arguments)]


def test_fcm_caches_the_oauth_token(settings):
    observed = []

    def handle(request):
        observed.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "oauth", "expires_in": 3600}
            )
        return httpx.Response(200, json={})

    assert [r.status for r in run(fcm_send(settings, handle))] == ["sent", "sent"]
    assert [r.url.host for r in observed].count("oauth2.googleapis.com") == 1
    assert (
        str(observed[1].url)
        == "https://fcm.googleapis.com/v1/projects/opencuria-test/messages:send"
    )
    assert observed[1].headers["authorization"] == "Bearer oauth"


@pytest.mark.parametrize(
    "status,error,expected",
    [
        (404, {"details": [{"errorCode": "UNREGISTERED"}]}, "unregistered"),
        (
            400,
            {"details": [{"fieldViolations": [{"field": "message.token"}]}]},
            "unregistered",
        ),
        (400, {"details": [{"errorCode": "INVALID_ARGUMENT"}]}, "failed"),
        (403, {"details": [{"errorCode": "SENDER_ID_MISMATCH"}]}, "failed"),
        (429, {"details": [{"errorCode": "QUOTA_EXCEEDED"}]}, "retry"),
        (503, {}, "retry"),
    ],
)
def test_fcm_never_drops_devices_for_configuration_errors(
    settings, status, error, expected
):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "oauth"})
        return httpx.Response(
            status, json={"error": error}, headers={"retry-after": "7"}
        )

    result, _ = run(fcm_send(settings, handle))
    assert result.status == expected
    if expected == "retry":
        assert result.retry_after == 7


def test_fcm_renews_a_rejected_oauth_token_on_retry(settings):
    tokens = []

    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            tokens.append(f"oauth-{len(tokens)}")
            return httpx.Response(200, json={"access_token": tokens[-1]})
        if request.headers["authorization"] == "Bearer oauth-0":
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        return httpx.Response(200, json={})

    first, second = run(fcm_send(settings, handle))
    assert (first.status, first.code) == ("retry", "fcm_401")
    assert second.status == "sent"
    assert tokens == ["oauth-0", "oauth-1"]


def test_fcm_auth_failures_are_not_device_errors(settings):
    def handle(request):
        return httpx.Response(400, content=json.dumps({"error": "invalid_grant"}))

    result, _ = run(fcm_send(settings, handle))
    assert (result.status, result.code) == ("failed", "fcm_auth_failed")
