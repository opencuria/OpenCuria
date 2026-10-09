"""Secret-safe projections for Claude tool events and permission prompts."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

_SECRET_NAME = (
    r"(?:[A-Za-z_][A-Za-z0-9_-]*?(?:api[_-]?key|access[_-]?token|"
    r"token|secret|password|passwd|credential|private[_-]?key)"
    r"[A-Za-z0-9_-]*|api[_-]?key|access[_-]?token|authorization|token|"
    r"secret|password|passwd|credential|private[_-]?key)"
)
_SECRET_ASSIGNMENT_PREFIX = re.compile(
    rf"(?i)(?<![A-Za-z0-9])({_SECRET_NAME}\s*[:=]\s*)"
)
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?<![A-Za-z0-9])({_SECRET_NAME}(?!\s*bearer)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&|]+)"
)
_AUTH_HEADER = re.compile(r'(?i)(authorization:\s*bearer\s+)[^"\s,;]+')
_SECRET_OPTION = re.compile(
    r"(?i)(--(?:api[-_]?key|access[-_]?token|password|passwd|secret|"
    r"token|authorization|credential)\s+|bearer\s+|basic\s+)\S+"
)
_URL_CREDENTIALS = re.compile(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@")
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "authorization",
    "credential",
    "privatekey",
    "cookie",
)


def _clean_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    """Return stable, nontrivial secret strings sorted longest first."""
    return tuple(
        sorted(
            {s for s in secrets if isinstance(s, str) and len(s) >= 4},
            key=len,
            reverse=True,
        )
    )


def redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    """Remove supplied credentials and common assignment/header patterns."""
    text = str(value or "")
    for secret in _clean_secrets(secrets):
        text = text.replace(secret, "[redacted]")
    headers: list[str] = []

    def hide_header(match: re.Match[str]) -> str:
        headers.append(match.group(1) + "[redacted]")
        return f"CLAUDEAUTHPLACEHOLDER{len(headers) - 1}END"

    text = _AUTH_HEADER.sub(hide_header, text)
    text = _SECRET_ASSIGNMENT.sub(r"\1[redacted]", text)
    text = _SECRET_OPTION.sub(r"\1[redacted]", text)
    text = _URL_CREDENTIALS.sub(r"\1[redacted]@", text)
    for index, header in enumerate(headers):
        text = text.replace(f"CLAUDEAUTHPLACEHOLDER{index}END", header)
    return text


def redact_value(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Recursively sanitize user-visible event payload values."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, nested in value.items():
            name = str(key)
            normalized = re.sub(r"[^a-z0-9]", "", name.lower())
            private = any(part in normalized for part in _SENSITIVE_KEY_PARTS)
            # Usage counters are public numeric metadata, not credentials.
            counter_keys = {
                "tokens",
                "prompttokens",
                "completiontokens",
                "totaltokens",
                "inputtokens",
                "outputtokens",
                "maxoutputtokens",
                "cachereadinputtokens",
                "cachecreationinputtokens",
            }
            numeric_counts = (
                isinstance(nested, (int, float))
                and not isinstance(nested, bool)
                or isinstance(nested, dict)
                and all(
                    isinstance(count, (int, float)) and not isinstance(count, bool)
                    for count in nested.values()
                )
            )
            if normalized in counter_keys and numeric_counts:
                private = False
            safe[name] = (
                "[redacted]"
                if private
                or normalized in {"env", "environment", "environmentvariables"}
                else redact_value(nested, secrets)
            )
        return safe
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets) for item in value)
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def redact_arguments(
    arguments: dict[str, Any], secrets: Iterable[str] = ()
) -> dict[str, Any]:
    """Return tool arguments with credential values removed recursively."""
    result = redact_value(arguments, secrets)
    return result if isinstance(result, dict) else {}


def redact_action(value: str, secrets: Iterable[str] = ()) -> str:
    """Make an action safe for a permission UI while policy uses its raw form."""
    return redact_text(value, secrets)[:2000]


def redact_title(value: str, secrets: Iterable[str] = ()) -> str:
    """Remove credentials from a human-visible permission title."""
    return redact_text(value, secrets)[:240]


class StreamingRedactor:
    """Withhold a bounded tail so cross-delta credentials are never emitted."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        """Initialize a stream-local redactor from this run's auth values."""
        self._secrets = _clean_secrets(secrets)
        self._pending = ""

    @property
    def pending_text(self) -> str:
        """Return the suffix withheld until a stream boundary."""
        return self._pending

    def _safe_cutoff(self) -> int:
        """Find the earliest suffix that could grow into a credential."""
        # Unknown auth values use a one-character suffix holdback: the next
        # delta can start an arbitrary secret. Exact known values below may
        # extend this range until their prefix diverges.
        cutoff = max(0, len(self._pending) - 1) if self._secrets else len(self._pending)
        if self._secrets:
            # Extend holdback for exact known-secret prefixes of any length.
            for secret in self._secrets:
                for size in range(min(len(secret), len(self._pending)), 0, -1):
                    if self._pending.endswith(secret[:size]):
                        cutoff = min(cutoff, len(self._pending) - size)
                        break
            # Redact a complete known secret anywhere in the pending text.
            for secret in self._secrets:
                start = 0
                while True:
                    start = self._pending.find(secret, start)
                    if start < 0:
                        break
                    if start < cutoff:
                        cutoff = start
                    start += 1

        # Keep a possible secret assignment wholly buffered until its value
        # terminator arrives, then redact the whole value at once.
        for match in _SECRET_ASSIGNMENT_PREFIX.finditer(self._pending):
            prefix = match.group(0)
            if re.search(r"(?i)\bauthorization\s*:\s*bearer\s*$", prefix):
                continue
            value_start = match.end()
            if value_start >= len(self._pending):
                cutoff = min(cutoff, match.start())
                continue
            quote = (
                self._pending[value_start]
                if self._pending[value_start] in {"'", '"'}
                else ""
            )
            if quote:
                if self._pending.find(quote, value_start + 1) < 0:
                    cutoff = min(cutoff, match.start())
                continue
            value_end = value_start
            while value_end < len(self._pending) and not (
                self._pending[value_end].isspace() or self._pending[value_end] in ",;&|"
            ):
                value_end += 1
            if value_end == len(self._pending):
                cutoff = min(cutoff, match.start())

        # Credential-key fragments must remain buffered before their
        # separator arrives; single-character fragments are ambiguous and
        # therefore conservatively withheld too.
        markers = (
            "api_key",
            "api-key",
            "apikey",
            "access_token",
            "access-token",
            "authorization",
            "token",
            "secret",
            "password",
            "passwd",
            "credential",
            "private_key",
            "private-key",
        )
        for marker in markers:
            for size in range(min(len(marker), len(self._pending)), 0, -1):
                suffix = self._pending[-size:].lower()
                if marker.startswith(suffix):
                    cutoff = min(cutoff, len(self._pending) - size)
                    break

        header = re.search(r"(?i)authorization:\s*bearer\s+\S*$", self._pending)
        if header is not None:
            cutoff = min(cutoff, header.start())
        return cutoff

    def push(self, text: str, *, final: bool = False) -> str:
        """Append a delta and return only text safe to stream immediately."""
        self._pending += str(text or "")
        if final:
            raw, self._pending = self._pending, ""
            return redact_text(raw, self._secrets)
        cutoff = self._safe_cutoff()
        if cutoff <= 0:
            return ""
        raw, self._pending = self._pending[:cutoff], self._pending[cutoff:]
        return redact_text(raw, self._secrets)

    def finish(self) -> str:
        """Flush the remaining safe suffix after a block boundary."""
        raw, self._pending = self._pending, ""
        return redact_text(raw, self._secrets)


__all__ = [
    "StreamingRedactor",
    "redact_action",
    "redact_arguments",
    "redact_text",
    "redact_title",
    "redact_value",
]
