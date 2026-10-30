"""Characterize MIME detection shared by tools and user media hydration."""

from __future__ import annotations

import pytest

from apps.harness.media.formats import sniff_attachment_mime


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF89a", "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP", "image/webp"),
        (b"%PDF-1.7", "application/pdf"),
    ],
)
def test_media_signature_wins_over_stored_mime_and_extension(
    sample: bytes, expected: str
) -> None:
    """Recognized bytes win even when both metadata and filename disagree."""
    assert sniff_attachment_mime(sample, "file.txt", "text/plain") == expected


@pytest.mark.parametrize(
    ("sample", "path", "stored_mime", "expected"),
    [
        (b"unknown", "file.png", "text/plain", "text/plain"),
        (b"unknown", "file.png", "application/octet-stream", "image/png"),
        (b"", "file.pdf", "", "application/pdf"),
        (b"RIFF", "file.bin", "", "application/octet-stream"),
        (b"RIFF\x00\x00\x00\x00WAVE", "file.bin", "", "application/octet-stream"),
        (b"\x89PNG", "file.txt", "", "text/plain"),
    ],
)
def test_unrecognized_media_falls_back_to_metadata_then_extension(
    sample: bytes, path: str, stored_mime: str, expected: str
) -> None:
    """Incomplete signatures and unrelated RIFF files do not become images."""
    assert sniff_attachment_mime(sample, path, stored_mime) == expected
