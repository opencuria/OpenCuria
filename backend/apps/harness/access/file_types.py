"""File values and MIME classification shared by workspace access implementations.

These contracts are independent of accessors, process streams and file readers.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field


def guess_mime_type(path: str) -> str:
    """Return a best-effort MIME type for *path*."""
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


@dataclass(frozen=True)
class FileContent:
    """Content returned by ``read_file``."""

    content: bytes = b""
    size: int = 0
    truncated: bool = False
    mime: str = "application/octet-stream"


@dataclass(frozen=True)
class DirEntry:
    """A single entry returned by ``list_dir``."""

    name: str
    path: str
    is_dir: bool = False
    size: int = 0


@dataclass(frozen=True)
class FileStat:
    """Metadata returned by ``stat``."""

    path: str
    is_dir: bool = False
    size: int = 0
    mime: str = "application/octet-stream"
    extra: dict = field(default_factory=dict)
