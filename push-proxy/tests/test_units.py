"""Configuration, handle sealing, VAPID parsing and rate limiting."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import jwt
import pytest

from push_proxy.config import ConfigError, load_settings
from push_proxy.encoding import b64url_decode, b64url_encode
from push_proxy.handles import Destination, HandleSealer
from push_proxy.ratelimit import RateLimiter
from push_proxy.vapid import VapidError, authenticate

from .conftest import PUBLIC_URL, Backend


def destination(**overrides) -> Destination:
    fields = {
        "platform": "ios",
        "app_id": "com.opencuria.mobile",
        "environment": "production",
        "kind": "alert",
        "token": "ab" * 32,
        "server": "thumbprint",
        **overrides,
    }
    return Destination(**fields)


def test_settings_load_both_providers(settings):
    assert settings.public_url == PUBLIC_URL
    assert settings.app_ids("ios") == {
        "com.opencuria.mobile",
        "com.opencuria.mobile.dev",
    }
    assert settings.app_ids("android") == {"com.opencuria.mobile"}


@pytest.mark.parametrize(
    "change,message",
    [
        ({"PUSH_PROXY_PUBLIC_URL": ""}, "PUSH_PROXY_PUBLIC_URL"),
        ({"PUSH_PROXY_PUBLIC_URL": "http://push.example.com"}, "PUSH_PROXY_PUBLIC_URL"),
        (
            {"PUSH_PROXY_PUBLIC_URL": "https://push.example.com/v1"},
            "PUSH_PROXY_PUBLIC_URL",
        ),
        (
            {"PUSH_PROXY_PUBLIC_URL": "https://push.example.com:99999"},
            "PUSH_PROXY_PUBLIC_URL",
        ),
        ({"PUSH_PROXY_HANDLE_KEYS": ""}, "PUSH_PROXY_HANDLE_KEYS"),
        ({"PUSH_PROXY_HANDLE_KEYS": "c2hvcnQ="}, "PUSH_PROXY_HANDLE_KEYS"),
        ({"APNS_KEY_ID": ""}, "must be set together"),
        ({"APNS_TEAM_ID": "private-sentinel"}, "10-character"),
        ({"APNS_APP_IDS": "bad id"}, "APNS_APP_IDS"),
        ({"APNS_PRIVATE_KEY_FILE": "relative.p8"}, "absolute"),
        ({"APNS_PRIVATE_KEY_FILE": "/does/not/exist"}, "readable"),
        ({"FCM_APP_IDS": ""}, "must be set together"),
    ],
)
def test_invalid_settings_fail_startup(env, change, message):
    with pytest.raises(ConfigError, match=message) as error:
        load_settings({**env, **change})
    assert "private-sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("HTTPS://Push.Example.test/", PUBLIC_URL),
        ("https://push.example.test:443", PUBLIC_URL),
        ("https://push.example.test:8443", f"{PUBLIC_URL}:8443"),
        ("http://[::1]:80", "http://[::1]"),
    ],
)
def test_public_url_is_normalized_like_the_servers_audience(env, value, expected):
    settings = load_settings({**env, "PUSH_PROXY_PUBLIC_URL": value})
    assert settings.public_url == expected


def test_providers_are_optional_but_not_both(env):
    without_fcm = {k: v for k, v in env.items() if not k.startswith("FCM_")}
    assert load_settings(without_fcm).fcm is None
    without_apns = {k: v for k, v in env.items() if not k.startswith("APNS_")}
    assert load_settings(without_apns).apns is None
    neither = {k: v for k, v in without_fcm.items() if not k.startswith("APNS_")}
    with pytest.raises(ConfigError, match="Configure APNs"):
        load_settings(neither)


@pytest.mark.parametrize("provider", ["apns", "fcm"])
def test_wrong_key_types_fail_without_disclosing_secrets(env, provider_keys, provider):
    if provider == "apns":
        Path(env["APNS_PRIVATE_KEY_FILE"]).write_text(provider_keys["fcm"])
        name = "APNS_PRIVATE_KEY_FILE"
    else:
        account = Path(env["FCM_SERVICE_ACCOUNT_FILE"])
        data = json.loads(account.read_text())
        account.write_text(json.dumps({**data, "private_key": provider_keys["apns"]}))
        name = "FCM_SERVICE_ACCOUNT_FILE"
    with pytest.raises(ConfigError, match=name) as error:
        load_settings(env)
    assert "BEGIN PRIVATE KEY" not in str(error.value)


def test_handles_round_trip_deterministically_and_reject_tampering():
    sealer = HandleSealer([os.urandom(64)])
    handle = sealer.seal(destination())
    assert sealer.seal(destination()) == handle
    assert sealer.open(handle) == destination()
    raw = bytearray(b64url_decode(handle))
    raw[-1] ^= 1
    assert sealer.open(b64url_encode(bytes(raw))) is None
    raw = bytearray(b64url_decode(handle))
    raw[0] = 2
    assert sealer.open(b64url_encode(bytes(raw))) is None
    assert sealer.open("!!") is None
    assert HandleSealer([os.urandom(64)]).open(handle) is None


def test_base64url_is_strict():
    assert b64url_decode(b64url_encode(b"\xff\xfe")) == b"\xff\xfe"
    for value in ("a+b", "ab==", "a", "a b"):
        with pytest.raises(ValueError):
            b64url_decode(value)


def test_vapid_accepts_whitespace_variants_and_rejects_extra_params():
    backend = Backend()
    token = jwt.encode(
        {"aud": PUBLIC_URL, "exp": int(time.time()) + 60},
        backend.private_key,
        algorithm="ES256",
    )
    sender = authenticate(f"vapid  t={token} ,k={backend.server_key}", PUBLIC_URL)
    assert sender.thumbprint
    assert (
        authenticate(f"Vapid k={backend.server_key}, t={token}", PUBLIC_URL) == sender
    )
    for header in (
        f"vapid t={token}, k={backend.server_key}, x=1",
        f"vapid t={token}; k={backend.server_key}",
        f"vapid t={token}",
        None,
    ):
        with pytest.raises(VapidError):
            authenticate(header, PUBLIC_URL)


def test_vapid_requires_expiry_and_audience():
    backend = Backend()
    for claims in ({"aud": PUBLIC_URL}, {"exp": int(time.time()) + 60}):
        token = jwt.encode(claims, backend.private_key, algorithm="ES256")
        with pytest.raises(VapidError):
            authenticate(f"vapid t={token}, k={backend.server_key}", PUBLIC_URL)


def test_rate_limiter_refills_and_bounds_memory():
    limiter = RateLimiter(rate=1, burst=2, max_keys=2)
    assert limiter.acquire("a", now=0) == 0
    assert limiter.acquire("a", now=0) == 0
    assert limiter.acquire("a", now=0) == 1
    assert limiter.acquire("a", now=1.5) == 0
    limiter.acquire("b", now=2)
    limiter.acquire("c", now=2)
    assert "a" not in limiter._buckets
    assert len(limiter._buckets) == 2


def test_handle_keys_accept_standard_and_urlsafe_base64(env):
    key = os.urandom(64)
    standard = base64.b64encode(key).decode()
    urlsafe = base64.urlsafe_b64encode(key).decode().rstrip("=")
    settings = load_settings(
        {**env, "PUSH_PROXY_HANDLE_KEYS": f"{standard}, {urlsafe}"}
    )
    assert settings.handle_keys == (key, key)
