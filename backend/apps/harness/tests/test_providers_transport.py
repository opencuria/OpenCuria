"""Tests for OpenCode-parity HTTP/OS transport error mapping."""

from __future__ import annotations

import httpx

from apps.harness.provider_retry import is_retryable_provider_error
from apps.harness.providers._transport import (
    map_httpx_error,
    map_os_transport_error,
)
from apps.harness.providers.base import (
    ProviderResponseError,
    ProviderTimeoutError,
)


def test_empty_read_error_is_connection_reset() -> None:
    """Blank httpx.ReadError becomes OpenCode's ECONNRESET message."""
    error = map_httpx_error(
        httpx.ReadError(""),
        provider="openrouter",
        label="OpenRouter",
    )
    assert isinstance(error, ProviderResponseError)
    assert str(error) == "Connection reset by server"
    assert error.is_retryable is True
    assert is_retryable_provider_error(error)


def test_connect_error_keeps_detail_and_is_retryable() -> None:
    """Non-empty ConnectError keeps the detail and stays retryable."""
    error = map_httpx_error(
        httpx.ConnectError("connect ECONNREFUSED 127.0.0.1:443"),
        provider="openrouter",
        label="OpenRouter",
    )
    assert isinstance(error, ProviderResponseError)
    assert "ECONNREFUSED" in str(error)
    assert str(error).startswith("OpenRouter request failed:")
    assert error.is_retryable is True
    assert is_retryable_provider_error(error)


def test_decoding_error_is_zlib_parity() -> None:
    """httpx.DecodingError maps to OpenCode's ZlibError message."""
    error = map_httpx_error(
        httpx.DecodingError("malformed gzip"),
        provider="chatgpt",
        label="ChatGPT",
    )
    assert isinstance(error, ProviderResponseError)
    assert str(error) == "Response decompression failed"
    assert error.is_retryable is True
    assert is_retryable_provider_error(error)


def test_timeout_exception_stays_timeout_error() -> None:
    """httpx timeouts stay ProviderTimeoutError with the existing message."""
    error = map_httpx_error(
        httpx.ReadTimeout("timed out"),
        provider="openrouter",
        label="OpenRouter",
    )
    assert isinstance(error, ProviderTimeoutError)
    assert str(error) == "OpenRouter request timed out"
    assert is_retryable_provider_error(error)


def test_empty_generic_http_error_falls_back_to_unknown() -> None:
    """Non-transport HTTPError with a blank message uses Unknown error."""
    error = map_httpx_error(
        httpx.HTTPError(""),
        provider="openrouter",
        label="OpenRouter",
    )
    assert isinstance(error, ProviderResponseError)
    assert str(error) == "Unknown error"
    assert error.is_retryable is None
    assert not is_retryable_provider_error(error)


def test_os_connection_reset_is_retryable() -> None:
    """Bedrock-style ConnectionResetError maps like ECONNRESET."""
    error = map_os_transport_error(
        ConnectionResetError(),
        provider="amazon-bedrock",
        label="Bedrock",
    )
    assert error is not None
    assert str(error) == "Connection reset by server"
    assert error.is_retryable is True
    assert is_retryable_provider_error(error)


def test_os_connection_error_with_detail_is_retryable() -> None:
    """OS ConnectionError with a message keeps the detail."""
    error = map_os_transport_error(
        ConnectionError("connection refused"),
        provider="amazon-bedrock",
        label="Bedrock",
    )
    assert error is not None
    assert "connection refused" in str(error)
    assert error.is_retryable is True


def test_os_mapper_ignores_non_transport_errors() -> None:
    """Unrelated exceptions are left for the caller to wrap."""
    assert (
        map_os_transport_error(
            ValueError("nope"),
            provider="amazon-bedrock",
            label="Bedrock",
        )
        is None
    )


def test_http_status_error_does_not_force_retry() -> None:
    """Leaked HTTPStatusError keeps the status and is not auto-retryable."""
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(400, request=request)
    error = map_httpx_error(
        httpx.HTTPStatusError("bad request", request=request, response=response),
        provider="openrouter",
        label="OpenRouter",
    )
    assert isinstance(error, ProviderResponseError)
    assert error.status_code == 400
    assert error.is_retryable is None
    assert not is_retryable_provider_error(error)
