"""APNs (HTTP/2, token auth) and FCM (HTTP v1) clients.

Results carry only allowlisted codes; provider response bodies, tokens and
signing material never reach logs or callers.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx
import jwt

from .config import APNsCredentials, FCMCredentials

APNS_HOSTS = {
    "production": "https://api.push.apple.com",
    "sandbox": "https://api.sandbox.push.apple.com",
}
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Apple rejects provider tokens older than an hour and refreshes more often than
# every 20 minutes (TooManyProviderTokenUpdates).
APNS_TOKEN_LIFETIME = 50 * 60
_APNS_UNREGISTERED = {"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"}


@dataclass(frozen=True)
class ProviderResult:
    """Outcome of one provider request."""

    status: Literal["sent", "unregistered", "retry", "failed"]
    code: str = ""
    retry_after: int | None = None


def _retry_after(response: httpx.Response) -> int | None:
    try:
        return max(1, min(3600, int(response.headers["retry-after"])))
    except (KeyError, ValueError):
        return None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


class APNsClient:
    """Send alerts and Live Activity updates with one cached provider token."""

    def __init__(self, credentials: APNsCredentials, client: httpx.AsyncClient) -> None:
        self.credentials = credentials
        self.client = client
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    async def _authorization(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token is None or self._token[1] <= now:
                token = await asyncio.to_thread(
                    jwt.encode,
                    {"iss": self.credentials.team_id, "iat": int(now)},
                    self.credentials.private_key,
                    algorithm="ES256",
                    headers={"kid": self.credentials.key_id},
                )
                self._token = (token, now + APNS_TOKEN_LIFETIME)
            return self._token[0]

    async def send(
        self,
        *,
        token: str,
        environment: str,
        topic: str,
        push_type: Literal["alert", "liveactivity"],
        priority: int,
        expiration: int,
        payload: bytes,
        collapse_id: str | None = None,
    ) -> ProviderResult:
        """POST one notification; `payload` is the final JSON body."""
        headers = {
            "authorization": f"bearer {await self._authorization()}",
            "apns-topic": topic,
            "apns-push-type": push_type,
            "apns-priority": str(priority),
            "apns-expiration": str(expiration),
            "content-type": "application/json",
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id
        response = await self.client.post(
            f"{APNS_HOSTS[environment]}/3/device/{token}",
            headers=headers,
            content=payload,
        )
        if response.status_code == 200:
            return ProviderResult("sent")
        reason = _json(response).get("reason")
        if response.status_code == 410 or reason in _APNS_UNREGISTERED:
            return ProviderResult("unregistered", "apns_unregistered")
        if reason == "ExpiredProviderToken":
            self._token = None
            return ProviderResult("retry", "apns_expired_provider_token")
        if response.status_code == 429 or response.status_code >= 500:
            return ProviderResult(
                "retry", f"apns_{response.status_code}", _retry_after(response)
            )
        return ProviderResult("failed", f"apns_{response.status_code}")


class FCMClient:
    """Send data messages with a cached Google OAuth access token."""

    def __init__(self, credentials: FCMCredentials, client: httpx.AsyncClient) -> None:
        self.credentials = credentials
        self.client = client
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    async def _authorization(self) -> str | ProviderResult:
        async with self._lock:
            now = time.time()
            if self._token is not None and self._token[1] > now:
                return self._token[0]
            assertion = await asyncio.to_thread(
                jwt.encode,
                {
                    "iss": self.credentials.client_email,
                    "scope": FCM_SCOPE,
                    "aud": GOOGLE_TOKEN_URL,
                    "iat": int(now),
                    "exp": int(now) + 3600,
                },
                self.credentials.private_key,
                algorithm="RS256",
            )
            response = await self.client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
            )
            if response.status_code != 200:
                retry = response.status_code == 429 or response.status_code >= 500
                return ProviderResult("retry" if retry else "failed", "fcm_auth_failed")
            body = _json(response)
            token = body.get("access_token")
            try:
                expires_in = min(int(body.get("expires_in", 3600)), 3600)
            except (TypeError, ValueError):
                expires_in = 0
            if not isinstance(token, str) or not token or expires_in <= 60:
                return ProviderResult("retry", "fcm_invalid_auth_response")
            self._token = (token, now + expires_in - 60)
            return token

    async def send(
        self, *, token: str, app_id: str, data: dict[str, str], ttl: int
    ) -> ProviderResult:
        """Send a high-priority data message; the app renders the notification."""
        authorization = await self._authorization()
        if isinstance(authorization, ProviderResult):
            return authorization
        response = await self.client.post(
            "https://fcm.googleapis.com/v1/projects/"
            f"{self.credentials.project_id}/messages:send",
            headers={"authorization": f"Bearer {authorization}"},
            json={
                "message": {
                    "token": token,
                    "data": data,
                    "android": {
                        "priority": "high",
                        "ttl": f"{ttl}s",
                        "restricted_package_name": app_id,
                    },
                }
            },
        )
        if response.is_success:
            return ProviderResult("sent")
        if response.status_code == 401:
            # An expired or revoked OAuth token: the next attempt fetches a new one.
            self._token = None
            return ProviderResult("retry", "fcm_401")
        if response.status_code == 429 or response.status_code >= 500:
            return ProviderResult(
                "retry", f"fcm_{response.status_code}", _retry_after(response)
            )
        if response.status_code == 404 or self._token_rejected(response):
            return ProviderResult("unregistered", "fcm_unregistered")
        # SENDER_ID_MISMATCH and auth errors point at proxy configuration; never
        # make backends forget devices because of them.
        return ProviderResult("failed", f"fcm_{response.status_code}")

    @staticmethod
    def _token_rejected(response: httpx.Response) -> bool:
        details = _json(response).get("error", {})
        details = details.get("details", []) if isinstance(details, dict) else []
        for item in details if isinstance(details, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("errorCode") == "UNREGISTERED":
                return True
            violations = item.get("fieldViolations", [])
            if isinstance(violations, list) and any(
                isinstance(v, dict) and v.get("field") == "message.token"
                for v in violations
            ):
                return True
        return False
