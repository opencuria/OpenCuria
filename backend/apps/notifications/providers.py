"""Direct APNs/FCM delivery using organization credentials from the vault."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Literal

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import BaseModel, Field, SecretStr

from apps.credentials.models import Credential
from common.utils import decrypt_value


class APNsSecret(BaseModel):
    """An Apple token-signing key; no client receives this object."""

    team_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    key_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    private_key: SecretStr


class FCMSecret(BaseModel):
    """Only fields used for the fixed Google service-account OAuth flow."""

    project_id: str = Field(pattern=r"^[a-z][a-z0-9-]{4,61}[a-z0-9]$")
    client_email: str = Field(pattern=r"^[^\s@]+@[^\s@]+\.iam\.gserviceaccount\.com$")
    private_key: SecretStr


def load_secret(credential: Credential, provider: str) -> APNsSecret | FCMSecret:
    """Validate encrypted vault contents without including secrets in errors."""
    try:
        data = json.loads(decrypt_value(credential.encrypted_value))
        schema = APNsSecret if provider == "apns" else FCMSecret
        secret = schema.model_validate(data)
        key = serialization.load_pem_private_key(
            secret.private_key.get_secret_value().encode(), password=None
        )
        if provider == "apns":
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
                key.curve, ec.SECP256R1
            ):
                raise ValueError
        elif not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError
        return secret
    except Exception:
        raise ValueError(
            f"Invalid {provider.upper()} credential JSON or private key"
        ) from None


@dataclass(frozen=True)
class DeliveryResult:
    """Provider outcomes contain only allowlisted error codes."""

    status: Literal["sent", "retry", "invalid", "failed"]
    code: str = ""


class PushProviders:
    """Reuse HTTP/2 connections and short-lived provider authentication tokens."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(http2=True, timeout=15.0)
        self._tokens: dict[tuple[str, str], tuple[str, float]] = {}

    async def close(self) -> None:
        """Release connections at ASGI shutdown."""
        await self.client.aclose()

    async def send_apns(
        self,
        *,
        credential: Credential,
        token: str,
        app_id: str,
        environment: str,
        payload: dict,
        collapse_id: str,
        live_activity: bool = False,
    ) -> DeliveryResult:
        """Send alert or ActivityKit updates to the correct signing environment."""
        secret = await asyncio.to_thread(load_secret, credential, "apns")
        cache_key = (str(credential.id), credential.encrypted_value)
        cached = self._tokens.get(cache_key)
        now = int(time.time())
        if cached is None or cached[1] <= now:
            authorization = await asyncio.to_thread(
                jwt.encode,
                {"iss": secret.team_id, "iat": now},
                secret.private_key.get_secret_value(),
                algorithm="ES256",
                headers={"kid": secret.key_id},
            )
            self._remember(cache_key, authorization, now + 50 * 60)
        else:
            authorization = cached[0]
        host = (
            "api.sandbox.push.apple.com"
            if environment == "sandbox"
            else "api.push.apple.com"
        )
        headers = {
            "authorization": f"bearer {authorization}",
            "apns-topic": app_id + (".push-type.liveactivity" if live_activity else ""),
            "apns-push-type": "liveactivity" if live_activity else "alert",
            "apns-priority": "10"
            if not live_activity or payload["aps"]["event"] == "end"
            else "5",
            "apns-expiration": str(now + 15 * 60),
            "apns-collapse-id": collapse_id,
        }
        response = await self.client.post(
            f"https://{host}/3/device/{token}", headers=headers, json=payload
        )
        if response.status_code == 200:
            return DeliveryResult("sent")
        if response.status_code == 410:
            return DeliveryResult("invalid", "apns_unregistered")
        if response.status_code == 429 or response.status_code >= 500:
            return DeliveryResult("retry", f"apns_{response.status_code}")
        try:
            reason = response.json().get("reason")
        except ValueError:
            reason = None
        if reason in {"BadDeviceToken", "DeviceTokenNotForTopic"}:
            return DeliveryResult("invalid", "apns_invalid_token")
        return DeliveryResult("failed", f"apns_{response.status_code}")

    async def send_fcm(
        self,
        *,
        credential: Credential,
        token: str,
        title: str,
        body: str,
        data: dict[str, str],
        collapse_id: str,
        app_id: str,
    ) -> DeliveryResult:
        """Use FCM HTTP v1; never trust a credential-supplied token endpoint."""
        secret = await asyncio.to_thread(load_secret, credential, "fcm")
        cache_key = (str(credential.id), credential.encrypted_value)
        cached = self._tokens.get(cache_key)
        now = int(time.time())
        if cached is None or cached[1] <= now:
            assertion = await asyncio.to_thread(
                jwt.encode,
                {
                    "iss": secret.client_email,
                    "scope": "https://www.googleapis.com/auth/firebase.messaging",
                    "aud": "https://oauth2.googleapis.com/token",
                    "iat": now,
                    "exp": now + 3600,
                },
                secret.private_key.get_secret_value(),
                algorithm="RS256",
            )
            response = await self.client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            if response.status_code != 200:
                return DeliveryResult(
                    "retry"
                    if response.status_code >= 500 or response.status_code == 429
                    else "failed",
                    "fcm_auth_failed",
                )
            try:
                authorization = response.json()["access_token"]
                expires_in = min(int(response.json().get("expires_in", 3600)), 3600)
                if not isinstance(authorization, str) or not authorization:
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                return DeliveryResult("retry", "fcm_invalid_auth_response")
            self._remember(cache_key, authorization, now + max(0, expires_in - 60))
        else:
            authorization = cached[0]
        response = await self.client.post(
            f"https://fcm.googleapis.com/v1/projects/{secret.project_id}/messages:send",
            headers={"authorization": f"Bearer {authorization}"},
            json={
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": data,
                    "android": {
                        "priority": "high",
                        "ttl": "900s",
                        "restricted_package_name": app_id,
                        "notification": {
                            "channel_id": "agent-activity",
                            "tag": collapse_id,
                            "sound": "default",
                        },
                    },
                }
            },
        )
        if response.is_success:
            return DeliveryResult("sent")
        if response.status_code == 429 or response.status_code >= 500:
            return DeliveryResult("retry", f"fcm_{response.status_code}")
        try:
            details = response.json().get("error", {}).get("details", [])
            invalid = any(
                item.get("errorCode") == "UNREGISTERED"
                for item in details
                if isinstance(item, dict)
            )
        except (ValueError, AttributeError, TypeError):
            invalid = False
        return DeliveryResult(
            "invalid" if invalid else "failed",
            "fcm_unregistered" if invalid else f"fcm_{response.status_code}",
        )

    def _remember(self, key: tuple[str, str], token: str, expires: float) -> None:
        # Configuration rotation cannot leave unbounded old secrets in memory.
        self._tokens = {
            k: v
            for k, v in self._tokens.items()
            if k[0] != key[0] and v[1] > time.time()
        }
        self._tokens[key] = (token, expires)
