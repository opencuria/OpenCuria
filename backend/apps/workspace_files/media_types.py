"""How workspace files are labelled when the backend serves them.

Workspace files are untrusted. They are served from the API origin, so only
types a browser renders without running code are served inline under their
own type; markup and scripts are served as plain text, everything else as an
attachment. Every response also carries a sandboxing CSP (see :mod:`.http`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MediaKind = Literal["image", "video", "audio", "pdf", "text", "binary"]

_IMAGES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "avif": "image/avif",
    "bmp": "image/bmp",
    "ico": "image/x-icon",
    "svg": "image/svg+xml",
    "heic": "image/heic",
    "heif": "image/heif",
    "tif": "image/tiff",
    "tiff": "image/tiff",
}
_VIDEOS = {
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "webm": "video/webm",
    "ogv": "video/ogg",
    "ogg": "video/ogg",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "avi": "video/x-msvideo",
    "wmv": "video/x-ms-wmv",
    "flv": "video/x-flv",
    "mpeg": "video/mpeg",
    "mpg": "video/mpeg",
    "3gp": "video/3gpp",
    "3g2": "video/3gpp2",
    "ts": "video/mp2t",
    "m2ts": "video/mp2t",
}
_AUDIO = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "opus": "audio/opus",
    "oga": "audio/ogg",
}
_TEXT = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "csv",
        "tsv",
        "log",
        "json",
        "jsonl",
        "yaml",
        "yml",
        "xml",
        "html",
        "htm",
        "css",
        "scss",
        "less",
        "js",
        "jsx",
        "mjs",
        "cjs",
        "tsx",
        "vue",
        "py",
        "go",
        "rs",
        "java",
        "kt",
        "kts",
        "swift",
        "rb",
        "php",
        "c",
        "h",
        "cpp",
        "hpp",
        "cs",
        "sh",
        "bash",
        "zsh",
        "fish",
        "sql",
        "ini",
        "conf",
        "cfg",
        "toml",
        "env",
        "gitignore",
        "dockerfile",
        "makefile",
        "lock",
        "diff",
        "patch",
    }
)
#: Browsers display these themselves without executing anything.
_INLINE_IMAGES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-icon",
        "image/svg+xml",
    }
)


@dataclass(frozen=True)
class MediaType:
    """Kind, served Content-Type and whether a browser may show it inline."""

    kind: MediaKind
    content_type: str
    inline: bool


def extension(name: str) -> str:
    """Return the lower-case extension, or a special file name like ``makefile``."""
    base = name.rsplit("/", 1)[-1]
    if "." not in base.lstrip("."):
        lowered = base.lower()
        return lowered if lowered in ("dockerfile", "makefile") else ""
    return base.rsplit(".", 1)[-1].lower()


def media_type(name: str) -> MediaType:
    """Classify *name* by extension, matching the clients' classification."""
    ext = extension(name)
    if ext in _IMAGES:
        content_type = _IMAGES[ext]
        return MediaType("image", content_type, content_type in _INLINE_IMAGES)
    if ext in _VIDEOS:
        return MediaType("video", _VIDEOS[ext], True)
    if ext in _AUDIO:
        return MediaType("audio", _AUDIO[ext], True)
    if ext == "pdf":
        return MediaType("pdf", "application/pdf", True)
    if ext in _TEXT:
        return MediaType("text", "text/plain; charset=utf-8", True)
    return MediaType("binary", "application/octet-stream", False)


ARCHIVE_TYPE = MediaType("binary", "application/gzip", False)
THUMBNAIL_TYPE = MediaType("image", "image/jpeg", True)
