"""Registration and delivery through the HTTP API, with a mock provider network."""

from __future__ import annotations

import json

import httpx
import pytest

from push_proxy.app import ALERT_FALLBACK, LIVE_ACTIVITY_NAME

from .conftest import ANDROID_APP, APNS_TOKEN, FCM_TOKEN, IOS_APP, Backend, ciphertext


def register(client, backend, **overrides):
    body = {
        "platform": "ios",
        "app_id": IOS_APP,
        "environment": "production",
        "kind": "alert",
        "token": APNS_TOKEN,
        "server_key": backend.server_key,
        **overrides,
    }
    return client.post("/v1/registrations", json=body)


def handle_for(client, backend, **overrides) -> str:
    response = register(client, backend, **overrides)
    assert response.status_code == 201, response.text
    return response.json()["handle"]


def push(client, backend, handle, authorization=None, **overrides):
    body = {"handle": handle, "type": "alert", "payload": ciphertext(), **overrides}
    return client.post(
        "/v1/push",
        json=body,
        headers={"authorization": authorization or backend.authorization()},
    )


def test_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_handles_hide_tokens_and_are_stable_per_backend(client, backend):
    first = handle_for(client, backend)
    assert APNS_TOKEN not in first
    assert handle_for(client, backend) == first
    assert handle_for(client, Backend()) != first
    assert handle_for(client, backend, environment="sandbox") != first
    assert handle_for(client, backend, kind="activity") != first


@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"app_id": "com.attacker.app"}, "unsupported_app"),
        (
            {"platform": "android", "app_id": "com.opencuria.mobile.dev"},
            "unsupported_app",
        ),
    ],
)
def test_registration_requires_an_app_this_proxy_serves(
    client, backend, overrides, code
):
    overrides.setdefault(
        "token", FCM_TOKEN if overrides.get("platform") else APNS_TOKEN
    )
    response = register(client, backend, **overrides)
    assert response.status_code == 422
    assert response.json()["error"] == code


@pytest.mark.parametrize(
    "overrides",
    [
        {"token": "not-hex-token-" * 4},
        {"token": "ab" * 8},
        {"platform": "android", "app_id": ANDROID_APP, "token": "has space"},
        {
            "platform": "android",
            "app_id": ANDROID_APP,
            "token": FCM_TOKEN,
            "kind": "activity",
        },
        {
            "platform": "android",
            "app_id": ANDROID_APP,
            "token": FCM_TOKEN,
            "environment": "sandbox",
        },
        {"server_key": "BAAA"},
        {"server_key": "B" + "A" * 86},
        {"unexpected": True},
    ],
)
def test_invalid_registrations_are_rejected_without_echoing_input(
    client, backend, overrides
):
    response = register(client, backend, **overrides)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert overrides.get("token", "\0") not in response.text


def test_registration_is_rate_limited_per_client(client, backend):
    proxy = client.app.state.proxy
    proxy.registrations.burst = 2
    proxy.registrations.rate = 0.001
    assert register(client, backend).status_code == 201
    assert register(client, backend).status_code == 201
    response = register(client, backend)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_ios_alert_carries_only_ciphertext_and_a_fixed_fallback(
    client, backend, network
):
    handle = handle_for(client, backend)
    payload = ciphertext()
    response = push(client, backend, handle, payload=payload, collapse_id="abc_123")
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "sent"}
    (request,) = network.deliveries
    assert str(request.url) == f"https://api.push.apple.com/3/device/{APNS_TOKEN}"
    assert request.headers["apns-topic"] == IOS_APP
    assert request.headers["apns-push-type"] == "alert"
    assert request.headers["apns-priority"] == "10"
    assert request.headers["apns-collapse-id"] == "abc_123"
    assert request.headers["authorization"].startswith("bearer ")
    assert json.loads(request.content) == {
        "aps": {"alert": ALERT_FALLBACK, "mutable-content": 1, "sound": "default"},
        "enc": payload,
    }


@pytest.mark.parametrize(
    "kind,event,priority",
    [("activity_update", "update", "5"), ("activity_end", "end", "10")],
)
def test_live_activity_updates_use_the_activity_topic(
    client, backend, network, kind, event, priority
):
    handle = handle_for(client, backend, kind="activity", environment="sandbox")
    payload = ciphertext(2000)
    extra = {"dismissal_date": 1_700_000_000} if kind == "activity_end" else {}
    response = push(
        client,
        backend,
        handle,
        type=kind,
        payload=payload,
        timestamp=1_700_000_000,
        stale_date=1_700_000_600,
        **extra,
    )
    assert response.status_code == 200, response.text
    (request,) = network.deliveries
    assert request.url.host == "api.sandbox.push.apple.com"
    assert request.headers["apns-topic"] == f"{IOS_APP}.push-type.liveactivity"
    assert request.headers["apns-push-type"] == "liveactivity"
    assert request.headers["apns-priority"] == priority
    aps = json.loads(request.content)["aps"]
    assert aps["event"] == event
    assert aps["timestamp"] == 1_700_000_000
    assert aps["stale-date"] == 1_700_000_600
    assert aps["content-state"] == {"name": LIVE_ACTIVITY_NAME, "props": payload}
    assert ("dismissal-date" in aps) == (kind == "activity_end")


def test_android_gets_a_high_priority_data_message(client, backend, network):
    handle = handle_for(
        client, backend, platform="android", app_id=ANDROID_APP, token=FCM_TOKEN
    )
    payload = ciphertext()
    response = push(client, backend, handle, payload=payload, ttl=120)
    assert response.status_code == 200, response.text
    (request,) = network.deliveries
    message = json.loads(request.content)["message"]
    assert message == {
        "token": FCM_TOKEN,
        "data": {"enc": payload},
        "android": {
            "priority": "high",
            "ttl": "120s",
            "restricted_package_name": ANDROID_APP,
        },
    }


def test_pushes_need_a_valid_vapid_signature(client, backend, network):
    handle = handle_for(client, backend)
    other = Backend()
    for authorization in (
        "Bearer token",
        "vapid t=abc",
        backend.authorization(audience="https://other.example"),
        backend.authorization(expires_in=-120),
        backend.authorization(expires_in=3 * 24 * 3600),
        backend.authorization(key=other.private_key),
    ):
        response = push(client, backend, handle, authorization=authorization)
        assert response.status_code == 401, authorization
        assert response.json()["error"] == "unauthorized"
    assert not network.deliveries


def test_a_handle_works_only_for_its_backend(client, backend, network):
    handle = handle_for(client, backend)
    other = Backend()
    foreign = push(client, other, handle)
    forged = push(client, backend, handle[:-4] + "AAAA")
    assert foreign.status_code == forged.status_code == 410
    assert foreign.json() == forged.json() == {"error": "invalid_handle"}
    assert not network.deliveries


def test_rotated_handle_keys_keep_old_handles_working(env, backend, network):
    import base64
    import os

    from starlette.testclient import TestClient

    from push_proxy.app import create_app
    from push_proxy.config import load_settings

    def app(keys):
        http = httpx.AsyncClient(transport=httpx.MockTransport(network.handle))
        settings = load_settings({**env, "PUSH_PROXY_HANDLE_KEYS": keys})
        return TestClient(create_app(settings, client=http))

    old_key = env["PUSH_PROXY_HANDLE_KEYS"]
    with app(old_key) as client:
        handle = handle_for(client, backend)
    new_key = base64.b64encode(os.urandom(64)).decode()
    with app(f"{new_key},{old_key}") as client:
        assert push(client, backend, handle).status_code == 200
        assert handle_for(client, backend) != handle
    with app(new_key) as client:
        assert push(client, backend, handle).status_code == 410


def test_handle_kind_must_match_the_message_type(client, backend, network):
    alert = handle_for(client, backend)
    activity = handle_for(client, backend, kind="activity")
    assert (
        push(client, backend, alert, type="activity_update", timestamp=1).status_code
        == 422
    )
    assert push(client, backend, activity).status_code == 422
    assert not network.deliveries


@pytest.mark.parametrize(
    "overrides",
    [
        {"payload": "not base64!"},
        {"payload": "QUJD"},
        {"type": "activity_update"},
        {"timestamp": 1},
        {"type": "activity_update", "timestamp": 1, "dismissal_date": 2},
        {"type": "activity_update", "timestamp": 1, "collapse_id": "x"},
        {"collapse_id": "x" * 65},
        {"collapse_id": "a b"},
        {"ttl": 100_000},
        {"title": "plaintext"},
    ],
)
def test_malformed_messages_are_rejected(client, backend, network, overrides):
    handle = handle_for(client, backend)
    response = push(client, backend, handle, **overrides)
    assert response.status_code == 400, overrides
    assert not network.deliveries


def test_oversized_bodies_and_payloads_are_refused(client, backend, network):
    handle = handle_for(client, backend)
    response = client.post(
        "/v1/push",
        content=b"{" + b" " * 20_000 + b"}",
        headers={
            "authorization": backend.authorization(),
            "content-type": "application/json",
        },
    )
    assert response.status_code == 413
    assert push(client, backend, handle, payload=ciphertext(3500)).status_code in {
        400,
        413,
    }
    assert not network.deliveries


@pytest.mark.parametrize(
    "status,reason,expected,error",
    [
        (410, "Unregistered", 410, "unregistered"),
        (400, "BadDeviceToken", 410, "unregistered"),
        (429, "TooManyRequests", 503, "provider_unavailable"),
        (503, "ServiceUnavailable", 503, "provider_unavailable"),
        (403, "InvalidProviderToken", 502, "provider_rejected"),
        (400, "TopicDisallowed", 502, "provider_rejected"),
    ],
)
def test_provider_outcomes_map_to_stable_errors(
    client, backend, network, status, reason, expected, error
):
    network.apns = lambda request: httpx.Response(status, json={"reason": reason})
    response = push(client, backend, handle_for(client, backend))
    assert response.status_code == expected
    assert response.json()["error"] == error
    if expected == 503:
        assert int(response.headers["retry-after"]) >= 1


def test_unreachable_provider_is_retryable(client, backend, network):
    def fail(request):
        raise httpx.ConnectError("down")

    network.apns = fail
    response = push(client, backend, handle_for(client, backend))
    assert response.status_code == 503
    assert response.json()["provider_code"] == "provider_unreachable"


def test_pushes_are_rate_limited_per_handle(client, backend, network):
    proxy = client.app.state.proxy
    proxy.handles.burst = 1
    proxy.handles.rate = 0.001
    handle = handle_for(client, backend)
    assert push(client, backend, handle).status_code == 200
    response = push(client, backend, handle)
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    other = handle_for(client, backend, kind="activity")
    assert (
        push(client, backend, other, type="activity_update", timestamp=1).status_code
        == 200
    )
