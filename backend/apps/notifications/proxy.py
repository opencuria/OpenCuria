"""The only way out for mobile pushes: the push proxy's /v1/push endpoint."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Literal

import httpx

from .configuration import PushSettings, get_push_settings

_CODE = re.compile(r"[^a-z0-9_:]")
_RETRYABLE = {429, 500, 503, 504}


@dataclass(frozen=True)
class DeliveryResult:
    """Outcomes contain only bounded error codes."""

    status: Literal["sent", "retry", "invalid", "failed"]
    code: str = ""


@dataclass(frozen=True)
class ProxyMessage:
    """One ciphertext for one proxy handle (push proxy protocol v1)."""

    handle: str
    type: Literal["alert", "activity_update", "activity_end"]
    payload: str
    ttl: int = 15 * 60
    collapse_id: str | None = None
    timestamp: int | None = None
    stale_date: int | None = None
    dismissal_date: int | None = None


def _code(prefix: str, body: dict) -> str:
    error = str(body.get("error", "")) if isinstance(body, dict) else ""
    provider = str(body.get("provider_code", "")) if isinstance(body, dict) else ""
    code = "_".join(filter(None, [prefix, error]))
    if provider:
        code += f":{provider}"
    return _CODE.sub("", code.lower())[:64]


class PushProxyClient:
    """Sign requests with this server's VAPID key over one pooled connection."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: PushSettings | None = None,
    ) -> None:
        self.settings = settings or get_push_settings()
        self.client = client or httpx.AsyncClient(
            http2=True, timeout=httpx.Timeout(20.0, connect=5.0)
        )
        self._authorization: tuple[str, int] | None = None

    async def close(self) -> None:
        """Release connections at worker shutdown."""
        await self.client.aclose()

    def _header(self) -> str:
        now = int(time.time())
        if self._authorization is None or self._authorization[1] - 3600 <= now:
            self._authorization = self.settings.identity.authorization(
                self.settings.audience, now
            )
        return self._authorization[0]

    async def send(self, message: ProxyMessage) -> DeliveryResult:
        """Deliver once; the outbox decides about retries."""
        if not self.settings.enabled:
            return DeliveryResult("failed", "proxy_disabled")
        body = {k: v for k, v in asdict(message).items() if v is not None}
        try:
            response = await self.client.post(
                f"{self.settings.proxy_url}/v1/push",
                json=body,
                headers={"authorization": self._header()},
            )
        except httpx.TransportError:
            return DeliveryResult("retry", "proxy_unreachable")
        if response.status_code == 200:
            return DeliveryResult("sent")
        try:
            details = response.json()
        except ValueError:
            details = {}
        code = _code(f"proxy_{response.status_code}", details)
        if response.status_code == 410:
            return DeliveryResult("invalid", code)
        if response.status_code in _RETRYABLE:
            return DeliveryResult("retry", code)
        return DeliveryResult("failed", code)
