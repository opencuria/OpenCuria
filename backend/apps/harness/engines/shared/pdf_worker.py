"""Isolated, resource-bounded PDF text worker; stdin is bytes, never a filename."""

from __future__ import annotations

import io
import json
import resource
import sys

MAX_INPUT = 20 * 1024 * 1024
MAX_TEXT = 512 * 1024
MAX_PAGES = 100


class PDFExtractionError(ValueError):
    """A bounded, deliberately public document rejection message."""


def extract(payload: bytes) -> str:
    """Extract bounded text without opening embedded links, files or attachments."""
    from pypdf import PdfReader

    if not payload.startswith(b"%PDF-") or len(payload) > MAX_INPUT:
        raise PDFExtractionError("PDF is invalid or exceeds the input limit")
    reader = PdfReader(io.BytesIO(payload), strict=True)
    if reader.is_encrypted:
        raise PDFExtractionError(
            "Encrypted PDF is unsupported; provide an unencrypted PDF"
        )
    if len(reader.pages) > MAX_PAGES:
        raise PDFExtractionError("PDF exceeds the 100-page limit")
    parts: list[str] = []
    size = 0
    for page in reader.pages:
        text = (page.extract_text() or "") + "\n"
        size += len(text.encode("utf-8"))
        if size > MAX_TEXT:
            raise PDFExtractionError("Extracted PDF exceeds the 512 KiB text limit")
        parts.append(text)
    text = "".join(parts).strip()
    if not text:
        raise PDFExtractionError(
            "PDF contains no extractable text (image-only PDFs need OCR)"
        )
    return text


def main() -> None:
    """Apply hard process limits before importing or parsing untrusted PDF data."""
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (5, 6))
    resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    try:
        text = extract(sys.stdin.buffer.read(MAX_INPUT + 1))
    except PDFExtractionError as exc:
        result = {"error": str(exc)}
    except Exception:
        # Parser internals and document content must not leak into errors.
        result = {"error": "PDF is malformed or exceeds extraction resource limits"}
    else:
        result = {"text": text}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
