"""Redacted, exact-action identifiers for sensitive permission actions."""

from __future__ import annotations

import hashlib
import hmac
import re

from django.conf import settings

MAX_PERMISSION_PATTERN_LENGTH = 1024
_SCOPE_MARKER = " [action-scope:v1:"
_SCOPE_SUFFIX = re.compile(r" \[action-scope:v1:([0-9a-f]{64})\]$")


def permission_scope(raw_action: str, display_action: str) -> str:
    """Build a safe persisted action, fingerprinting redacted or long actions.

    Raw action text is never returned or persisted when it differs from its
    display projection. Its HMAC uses Django's deployment key, so the opaque
    suffix is stable across runs but does not disclose credential material.
    """
    raw = str(raw_action or "")
    display = str(display_action or "")
    needs_fingerprint = (
        raw != display
        or len(display) > MAX_PERMISSION_PATTERN_LENGTH
        or _SCOPE_MARKER in display
        or display.endswith("[redacted]")
    )
    if not needs_fingerprint:
        return display
    digest = hmac.new(
        str(settings.SECRET_KEY).encode("utf-8"),
        raw.encode("utf-8", errors="surrogatepass"),
        hashlib.sha256,
    ).hexdigest()
    suffix = f"{_SCOPE_MARKER}{digest}]"
    return display[: MAX_PERMISSION_PATTERN_LENGTH - len(suffix)] + suffix


def is_permission_scope(value: str) -> bool:
    """Return whether *value* ends with a versioned opaque fingerprint."""
    return _SCOPE_SUFFIX.search(str(value or "")) is not None


def permission_scope_matches(stored_scope: str, candidate_scope: str) -> bool:
    """Match two scoped actions without applying glob semantics to HMACs."""
    stored = _SCOPE_SUFFIX.search(str(stored_scope or ""))
    candidate = _SCOPE_SUFFIX.search(str(candidate_scope or ""))
    return bool(
        stored is not None
        and candidate is not None
        and hmac.compare_digest(stored.group(1), candidate.group(1))
    )


def permission_display_action(value: str) -> str:
    """Remove a stored action fingerprint for a safe UI projection."""
    text = str(value or "")
    match = _SCOPE_SUFFIX.search(text)
    return text[: match.start()] if match is not None else text


__all__ = [
    "MAX_PERMISSION_PATTERN_LENGTH",
    "is_permission_scope",
    "permission_display_action",
    "permission_scope",
    "permission_scope_matches",
]
