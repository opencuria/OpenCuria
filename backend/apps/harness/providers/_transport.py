"""Map HTTP/OS transport failures to harness provider errors.

OpenCode ``MessageV2.fromError`` parity: ECONNRESET, fetch/stream drops,
and zlib decompression failures become retryable API errors with a
non-empty message. Empty httpx messages (``ReadError``) must not stay
blank or the SessionRetry classifier cannot see them.
"""

from __future__ import annotations

import httpx

from .base import (
    ProviderError,
    ProviderResponseError,
    ProviderTimeoutError,
)

#: OS-level drops Bedrock (and similar SDKs) surface instead of httpx.
_OS_TRANSPORT_ERRORS = (ConnectionError, BrokenPipeError, ConnectionResetError)


def map_httpx_error(
    exc: httpx.HTTPError,
    *,
    provider: str,
    label: str,
) -> ProviderError:
    """Map an httpx failure to a harness provider error.

    Args:
        exc: Transport, timeout, or HTTP-status failure from httpx.
        provider: Adapter name stored on the raised error (e.g. ``openrouter``).
        label: Human provider label used in messages (e.g. ``OpenRouter``).

    Returns:
        A timeout or response error ready to raise. Transport and
        decompression failures set ``is_retryable=True``.
    """
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError(
            f"{label} request timed out",
            provider=provider,
        )
    if isinstance(exc, httpx.DecodingError):
        return ProviderResponseError(
            "Response decompression failed",
            provider=provider,
            is_retryable=True,
        )
    if isinstance(
        exc,
        (httpx.NetworkError, httpx.RemoteProtocolError, httpx.LocalProtocolError),
    ):
        return ProviderResponseError(
            _failed_message(
                exc,
                label,
                empty_fallback="Connection reset by server",
            ),
            provider=provider,
            is_retryable=True,
        )
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        detail = str(exc).strip()
        if not detail:
            message = f"{label} error ({status})" if status else "Unknown error"
        else:
            message = f"{label} request failed: {detail}"
        return ProviderResponseError(
            message,
            provider=provider,
            status_code=status,
        )
    return ProviderResponseError(
        _failed_message(exc, label, empty_fallback="Unknown error"),
        provider=provider,
    )


def map_os_transport_error(
    exc: BaseException,
    *,
    provider: str,
    label: str,
) -> ProviderResponseError | None:
    """Map OS connection drops to a retryable provider error, if applicable.

    Args:
        exc: Unexpected SDK exception (Bedrock ``botocore`` path).
        provider: Adapter name stored on the raised error.
        label: Human provider label used in messages.

    Returns:
        A retryable response error for connection resets, or ``None`` when
        *exc* is not a transport drop.
    """
    if not isinstance(exc, _OS_TRANSPORT_ERRORS):
        return None
    return ProviderResponseError(
        _failed_message(exc, label, empty_fallback="Connection reset by server"),
        provider=provider,
        is_retryable=True,
    )


def _failed_message(exc: BaseException, label: str, *, empty_fallback: str) -> str:
    """Return ``{label} request failed: …`` or *empty_fallback* when blank."""
    detail = str(exc).strip()
    if not detail:
        return empty_fallback
    return f"{label} request failed: {detail}"
