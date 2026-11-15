"""Characterize sanitized stream excerpts independently of MCP and Django."""

from __future__ import annotations

from common.stream_diagnostics import (
    MAX_STDERR_EXCERPT_CHARS,
    sanitize_stderr_excerpt,
)


def test_sanitize_stderr_excerpt_redacts_and_bounds() -> None:
    """Keep existing redaction, binary decoding and excerpt length behavior."""
    assert sanitize_stderr_excerpt(None) == ""
    assert sanitize_stderr_excerpt(b"") == ""
    excerpt = sanitize_stderr_excerpt(b"Error: api_key=hunter2\n--password s3cret\nok")
    assert "hunter2" not in excerpt
    assert "s3cret" not in excerpt
    assert "[redacted]" in excerpt
    assert "ok" in excerpt
    long = sanitize_stderr_excerpt(b"x" * (MAX_STDERR_EXCERPT_CHARS + 500))
    assert len(long) <= MAX_STDERR_EXCERPT_CHARS + 1
    # Binary noise cannot break logging.
    assert sanitize_stderr_excerpt(b"\xff\xfe\x00boom\n") != ""
