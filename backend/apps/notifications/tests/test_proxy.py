"""Encryption (RFC 8291 vectors), VAPID identity and the push proxy client."""

import asyncio
import base64
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from ..configuration import get_push_settings
from ..encryption import (
    ALERT_PADDED_SIZE,
    encrypt,
    encrypt_with,
    validate_receiver_key,
)
from ..proxy import ProxyMessage, PushProxyClient
from .conftest import PROXY_URL, unb64url

pytestmark = pytest.mark.usefixtures("push_settings")


def private_key(value: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(unb64url(value), "big"), ec.SECP256R1())


def test_encryption_matches_the_rfc_8291_example():
    """RFC 8291 section 5 and appendix A, byte for byte."""
    body = encrypt_with(
        b"When I grow up, I want to be a watermelon",
        receiver_key=unb64url(
            "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZ"
            "GH6SRpkNtoIAiw4"
        ),
        auth_secret=unb64url("BTBZMqHH6r4Tts7J_aSIgg"),
        padded_size=41,
        sender_key=private_key("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        salt=unb64url("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert base64.urlsafe_b64encode(body).rstrip(b"=").decode() == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
        "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVB"
        "St2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_ciphertext_length_hides_the_message_length(device_keys):
    short = encrypt(
        b"{}",
        receiver_key=device_keys.public,
        auth_secret=device_keys.auth_secret,
        padded_size=ALERT_PADDED_SIZE,
    )
    long = encrypt(
        b"x" * 900,
        receiver_key=device_keys.public,
        auth_secret=device_keys.auth_secret,
        padded_size=ALERT_PADDED_SIZE,
    )
    assert len(short) == len(long)
    assert device_keys.decrypt(short) == b"{}"
    assert device_keys.decrypt(long) == b"x" * 900
    with pytest.raises(ValueError):
        encrypt(
            b"x" * (ALERT_PADDED_SIZE + 1),
            receiver_key=device_keys.public,
            auth_secret=device_keys.auth_secret,
            padded_size=ALERT_PADDED_SIZE,
        )


@pytest.mark.parametrize("value", ["", "BAAA", "A" * 87, "B" + "A" * 86])
def test_receiver_keys_must_be_p256_points(value):
    with pytest.raises(ValueError):
        validate_receiver_key(value)


def test_identity_is_derived_from_the_secret_key(settings):
    first = get_push_settings().identity.public_key
    assert get_push_settings().identity.public_key == first
    assert len(unb64url(first)) == 65
    settings.SECRET_KEY = "another-secret-key"
    assert get_push_settings().identity.public_key != first


def send(message, handler, **client_options):
    async def run():
        client = PushProxyClient(
            httpx.AsyncClient(transport=httpx.MockTransport(handler)), **client_options
        )
        results = [await client.send(message), await client.send(message)]
        await client.close()
        return results

    return asyncio.run(run())


def test_client_signs_with_vapid_and_reuses_the_token():
    observed = []

    def handle(request):
        observed.append(request)
        return httpx.Response(200, json={"status": "sent"})

    message = ProxyMessage(handle="h", type="alert", payload="p", collapse_id="c")
    assert [r.status for r in send(message, handle)] == ["sent", "sent"]
    request = observed[0]
    assert str(request.url) == f"{PROXY_URL}/v1/push"
    assert json.loads(request.content) == {
        "handle": "h",
        "type": "alert",
        "payload": "p",
        "ttl": 900,
        "collapse_id": "c",
    }
    scheme, params = request.headers["authorization"].split(" ", 1)
    values = dict(part.strip().split("=", 1) for part in params.split(","))
    assert scheme == "vapid"
    identity = get_push_settings().identity
    assert values["k"] == identity.public_key
    claims = jwt.decode(
        values["t"],
        identity.private_key.public_key(),
        algorithms=["ES256"],
        audience=PROXY_URL,
    )
    assert "sub" not in claims
    assert observed[1].headers["authorization"] == request.headers["authorization"]


@pytest.mark.parametrize(
    "status,body,expected,code",
    [
        (
            410,
            {"error": "unregistered", "provider_code": "apns_unregistered"},
            "invalid",
            "proxy_410_unregistered:apns_unregistered",
        ),
        (410, {"error": "invalid_handle"}, "invalid", "proxy_410_invalid_handle"),
        (429, {"error": "rate_limited"}, "retry", "proxy_429_rate_limited"),
        (
            503,
            {"error": "provider_unavailable"},
            "retry",
            "proxy_503_provider_unavailable",
        ),
        (500, None, "retry", "proxy_500"),
        (
            502,
            {"error": "provider_rejected", "provider_code": "apns_403"},
            "failed",
            "proxy_502_provider_rejected:apns_403",
        ),
        (401, {"error": "unauthorized"}, "failed", "proxy_401_unauthorized"),
        (400, {"error": "<script>"}, "failed", "proxy_400_script"),
    ],
)
def test_proxy_responses_map_to_outbox_outcomes(status, body, expected, code):
    message = ProxyMessage(handle="h", type="alert", payload="p")
    result, _ = send(
        message,
        lambda request: (
            httpx.Response(status, json=body)
            if body is not None
            else httpx.Response(status, text="oops")
        ),
    )
    assert (result.status, result.code) == (expected, code)


def test_unreachable_or_disabled_proxy(settings):
    def fail(request):
        raise httpx.ConnectError("down")

    message = ProxyMessage(handle="h", type="alert", payload="p")
    assert send(message, fail)[0].code == "proxy_unreachable"
    settings.PUSH_PROXY_URL = ""
    assert send(message, fail)[0].code == "proxy_disabled"
