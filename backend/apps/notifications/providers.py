"""Direct APNs/FCM delivery using instance-wide hosting settings."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import httpx
import jwt

from .configuration import get_push_settings


@dataclass(frozen=True)
class DeliveryResult:
    """Provider outcomes contain only allowlisted error codes."""

    status: Literal["sent", "retry", "invalid", "failed"]
    code: str = ""


class PushProviders:
    """Reuse HTTP/2 connections and short-lived provider authentication tokens."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(http2=True, timeout=15.0)
        self.settings = get_push_settings()
        self._tokens: dict[str, tuple[str, float]] = {}

    async def close(self) -> None:
        """Release connections at ASGI shutdown."""
        await self.client.aclose()

    async def send_apns(
        self,
        *,
        token: str,
        app_id: str,
        environment: str,
        payload: dict,
        collapse_id: str,
        live_activity: bool = False,
    ) -> DeliveryResult:
        """Send alert or ActivityKit updates to the correct signing environment."""
        secret = self.settings.apns
        if secret is None:
            return DeliveryResult("failed", "provider_not_configured")
        cached = self._tokens.get("apns")
        now = int(time.time())
        if cached is None or cached[1] <= now:
            authorization = await asyncio.to_thread(
                jwt.encode,
                {"iss": secret.team_id, "iat": now},
                secret.private_key.get_secret_value(),
                algorithm="ES256",
                headers={"kid": secret.key_id},
            )
            self._tokens["apns"] = (authorization, now + 50 * 60)
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
        token: str,
        title: str,
        body: str,
        data: dict[str, str],
        collapse_id: str,
        app_id: str,
    ) -> DeliveryResult:
        """Use FCM HTTP v1 with fixed Google OAuth and messaging endpoints."""
        secret = self.settings.fcm
        if secret is None:
            return DeliveryResult("failed", "provider_not_configured")
        cached = self._tokens.get("fcm")
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
            self._tokens["fcm"] = (authorization, now + max(0, expires_in - 60))
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
