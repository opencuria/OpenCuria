"""The only way out for mobile pushes: the push proxy's /v1/push endpoint."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Literal

import httpx

from .configuration import PushSettings, get_push_settings

_CODE = re.compile(r"[^a-z0-9_:]")
# 502 also comes from a reverse proxy while the push proxy restarts; only the
# proxy's own provider_rejected answer is final.
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_RETRY_AFTER = 15 * 60


@dataclass(frozen=True)
class DeliveryResult:
    """Outcomes contain only bounded error codes."""

    status: Literal["sent", "retry", "invalid", "failed"]
    code: str = ""
    # Seconds the proxy asked to wait before the next attempt (Retry-After).
    retry_after: int = 0


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


def _retry_after(response: httpx.Response) -> int:
    """Delay-seconds only (the proxy never sends HTTP dates), bounded."""
    value = response.headers.get("retry-after", "").strip()
    return min(int(value), _MAX_RETRY_AFTER) if value.isdigit() else 0


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
        error = details.get("error") if isinstance(details, dict) else None
        if response.status_code in _RETRYABLE and error != "provider_rejected":
            return DeliveryResult("retry", code, _retry_after(response))
        return DeliveryResult("failed", code)
