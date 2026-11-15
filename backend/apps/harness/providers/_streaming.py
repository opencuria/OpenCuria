"""Provider-neutral deadlines, HTTP timeouts and timed SSE data framing.

Adapters own authentication, HTTP status handling and wire payload parsing.
This module only controls waits and yields each non-empty SSE data line,
preserving the existing line-oriented framing and DONE-marker behavior.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import httpx

from .base import (
    ChatOptions,
    ProviderHeaderTimeoutError,
    ProviderStreamTimeoutError,
    ProviderTimeoutError,
)

HTTPX_POOL_TIMEOUT_SECONDS = 5.0


def positive_timeout(value: float | None) -> float | None:
    """Return *value* when it is a positive timeout, otherwise ``None``."""
    if value is None or value <= 0:
        return None
    return value


def httpx_stream_timeout(opts: ChatOptions) -> httpx.Timeout:
    """Use the header timeout for connect/write and disable httpx read timeouts.

    Header and SSE idle waits are enforced with ``asyncio.wait_for`` so
    httpx does not treat a long reasoning pause as a read timeout.
    """
    header = positive_timeout(opts.header_timeout_seconds)
    return httpx.Timeout(
        None,
        connect=header,
        read=None,
        write=header,
        pool=HTTPX_POOL_TIMEOUT_SECONDS,
    )


def effective_timeout(
    phase_seconds: float | None,
    deadline: float | None,
) -> float | None:
    """Combine a phase timeout with an optional wall-clock deadline."""
    phase = positive_timeout(phase_seconds)
    if deadline is None:
        return phase
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0.0
    if phase is None:
        return remaining
    return min(phase, remaining)


def stream_deadline(opts: ChatOptions) -> float | None:
    """Return a monotonic deadline when a total timeout is configured."""
    total = positive_timeout(opts.timeout_seconds)
    if total is None:
        return None
    return time.monotonic() + total


def _total_timeout_error(
    *,
    provider: str,
    label: str,
    deadline: float | None,
    total_timeout: float | None,
) -> ProviderTimeoutError | None:
    """Return a provider-labelled timeout only when the total cap has expired."""
    if deadline is not None and (deadline - time.monotonic()) <= 0:
        total = positive_timeout(total_timeout) or 0.0
        return ProviderTimeoutError(
            f"{label} request timed out after {int(total * 1000)}ms",
            provider=provider,
        )
    return None


def header_or_total_timeout(
    *,
    provider: str,
    label: str,
    deadline: float | None,
    total_timeout: float | None,
    waited: float | None,
) -> ProviderTimeoutError:
    """Choose a header or total timeout after a header wait expires."""
    return _total_timeout_error(
        provider=provider,
        label=label,
        deadline=deadline,
        total_timeout=total_timeout,
    ) or ProviderHeaderTimeoutError(
        provider=provider,
        timeout_seconds=waited if waited is not None else 0.0,
    )


async def iter_sse_data(
    response: httpx.Response,
    *,
    provider: str,
    label: str,
    chunk_timeout: float | None,
    deadline: float | None,
    total_timeout: float | None,
) -> AsyncIterator[str]:
    """Yield data payloads with idle/deadline enforcement on each line read.

    Comments, empty data, other fields and ``[DONE]`` are skipped. The stream
    ends at EOF; payload interpretation belongs to the calling adapter.
    Cancellation and transport errors propagate to the response owner.
    """
    iterator = response.aiter_lines()
    while True:
        timeout = effective_timeout(chunk_timeout, deadline)
        try:
            if timeout is None:
                line = await anext(iterator)
            else:
                line = await asyncio.wait_for(anext(iterator), timeout=timeout)
        except StopAsyncIteration:
            return
        except TimeoutError as exc:
            raise _total_timeout_error(
                provider=provider,
                label=label,
                deadline=deadline,
                total_timeout=total_timeout,
            ) or ProviderStreamTimeoutError(provider=provider) from exc
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if not stripped.startswith("data:"):
            continue
        data = stripped[len("data:") :].strip()
        if data and data != "[DONE]":
            yield data
