"""Verify signed provider requests without contacting Apple or Google."""

import asyncio
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization

from ..providers import PushProviders

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "environment,host",
    [("sandbox", "api.sandbox.push.apple.com"), ("production", "api.push.apple.com")],
)
def test_apns_headers_jwt_and_activity_payload(setup, push_keys, environment, host):
    observed = []

    def handle(request):
        observed.append(request)
        return httpx.Response(200)

    async def run():
        provider = PushProviders(
            httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        for _ in range(2):
            result = await provider.send_apns(
                credential=setup[5]["apns"],
                token="ab" * 32,
                app_id="com.opencuria.mobile",
                environment=environment,
                payload={"aps": {"event": "update"}},
                collapse_id="activity",
                live_activity=True,
            )
            assert result.status == "sent"
        await provider.close()

    asyncio.run(run())
    request = observed[0]
    assert request.url.host == host
    assert (
        request.headers["apns-topic"] == "com.opencuria.mobile.push-type.liveactivity"
    )
    assert request.headers["apns-push-type"] == "liveactivity"
    assert request.headers["apns-priority"] == "5"
    authorization = request.headers["authorization"].split(" ")[1]
    key = serialization.load_pem_private_key(
        push_keys["apns"]["private_key"].encode(), password=None
    )
    claims = jwt.decode(authorization, key.public_key(), algorithms=["ES256"])
    assert claims["iss"] == push_keys["apns"]["team_id"]
    assert (
        jwt.get_unverified_header(authorization)["kid"] == push_keys["apns"]["key_id"]
    )
    assert observed[1].headers["authorization"] == request.headers["authorization"]


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        (410, "Unregistered", "invalid"),
        (400, "BadDeviceToken", "invalid"),
        (429, "TooManyRequests", "retry"),
        (503, "ServiceUnavailable", "retry"),
        (403, "InvalidProviderToken", "failed"),
    ],
)
def test_apns_error_classification(setup, status, reason, expected):
    async def run():
        provider = PushProviders(
            httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json={"reason": reason})
                )
            )
        )
        result = await provider.send_apns(
            credential=setup[5]["apns"],
            token="ab" * 32,
            app_id="com.opencuria.mobile",
            environment="production",
            payload={"aps": {}},
            collapse_id="notification",
        )
        await provider.close()
        return result

    assert asyncio.run(run()).status == expected


def test_fcm_uses_fixed_oauth_and_http_v1_and_caches_auth(setup, push_keys):
    observed = []

    def handle(request):
        observed.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "test-oauth-token", "expires_in": 3600}
            )
        return httpx.Response(200, json={"name": "projects/opencuria-test/messages/1"})

    async def run():
        provider = PushProviders(
            httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        for _ in range(2):
            result = await provider.send_fcm(
                credential=setup[5]["fcm"],
                token="fcm-device-token",
                title="Agent finished",
                body="Conversation",
                data={"sessionId": "session"},
                collapse_id="collapse",
                app_id="com.opencuria.mobile",
            )
            assert result.status == "sent"
        await provider.close()

    asyncio.run(run())
    assert len(observed) == 3
    assert str(observed[0].url) == "https://oauth2.googleapis.com/token"
    assert (
        str(observed[1].url)
        == "https://fcm.googleapis.com/v1/projects/opencuria-test/messages:send"
    )
    request = json.loads(observed[1].content)["message"]
    assert request["token"] == "fcm-device-token"
    assert request["data"] == {"sessionId": "session"}
    assert request["android"]["notification"]["channel_id"] == "agent-activity"
    assert request["android"]["restricted_package_name"] == "com.opencuria.mobile"


@pytest.mark.parametrize(
    "code,expected",
    [
        ("UNREGISTERED", "invalid"),
        ("INVALID_ARGUMENT", "failed"),
        ("SENDER_ID_MISMATCH", "failed"),
    ],
)
def test_fcm_does_not_delete_tokens_for_configuration_errors(setup, code, expected):
    def handle(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "test-token"})
        return httpx.Response(400, json={"error": {"details": [{"errorCode": code}]}})

    async def run():
        provider = PushProviders(
            httpx.AsyncClient(transport=httpx.MockTransport(handle))
        )
        result = await provider.send_fcm(
            credential=setup[5]["fcm"],
            token="fcm-device-token",
            title="Finished",
            body="Test",
            data={},
            collapse_id="collapse",
            app_id="com.opencuria.mobile",
        )
        await provider.close()
        return result

    assert asyncio.run(run()).status == expected
