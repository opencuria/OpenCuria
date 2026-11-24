"""Backend authentication with VAPID (RFC 8292) ES256 tokens."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from .encoding import decode_server_key, server_key_thumbprint

MAX_TOKEN_LIFETIME = 24 * 60 * 60
_LEEWAY = 60
_PARAM = re.compile(r"\s*([a-z]+)\s*=\s*([^,\s]+)\s*(?:,|$)")


class VapidError(Exception):
    """The request is not signed by the key it claims."""


@dataclass(frozen=True)
class Sender:
    """An authenticated backend, identified only by its public key."""

    thumbprint: str


def authenticate(header: str | None, audience: str, now: float | None = None) -> Sender:
    """Verify `Authorization: vapid t=<jwt>, k=<key>` for this proxy's origin."""
    if not header:
        raise VapidError("Missing VAPID authorization")
    scheme, _, params = header.strip().partition(" ")
    if scheme.lower() != "vapid":
        raise VapidError("Use the vapid authorization scheme")
    values = {}
    position = 0
    while position < len(params):
        match = _PARAM.match(params, position)
        if not match or match.end() == position:
            raise VapidError("Malformed VAPID authorization")
        values[match.group(1)] = match.group(2)
        position = match.end()
    if set(values) != {"t", "k"}:
        raise VapidError("VAPID authorization needs t and k")
    try:
        raw_key = decode_server_key(values["k"])
    except ValueError:
        raise VapidError("Invalid VAPID key") from None
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw_key)
    current = time.time() if now is None else now
    try:
        claims = jwt.decode(
            values["t"],
            key,
            algorithms=["ES256"],
            audience=audience,
            leeway=_LEEWAY,
            options={"require": ["exp", "aud"], "verify_exp": False},
        )
    except jwt.PyJWTError:
        raise VapidError("Invalid VAPID token") from None
    expires = claims["exp"]
    if not isinstance(expires, int) or isinstance(expires, bool):
        raise VapidError("Invalid VAPID expiry")
    if expires < current - _LEEWAY or expires > current + MAX_TOKEN_LIFETIME + _LEEWAY:
        raise VapidError("VAPID token expired or valid for too long")
    return Sender(thumbprint=server_key_thumbprint(raw_key))
