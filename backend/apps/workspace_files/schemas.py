"""Request and response schemas of the workspace file REST adapter."""

from __future__ import annotations

from typing import Literal

from ninja import Field, Schema


class FileLinksIn(Schema):
    """Workspace paths to describe and sign links for."""

    paths: list[str] = Field(..., min_length=1, max_length=100)


class FileLinkOut(Schema):
    """One path: metadata and API-relative URLs, or an error code.

    URLs are relative to the API base URL (``.../api/v1``). ``content_url``
    streams the file (supports ``Range``), ``download_url`` forces a
    download (folders download as ``.tar.gz``), ``thumbnail_url`` returns a
    tiny blurred JPEG of images and videos.
    """

    path: str
    name: str
    type: Literal["file", "directory", ""] = ""
    size: int = 0
    modified_at: int = 0
    kind: str = ""
    mime_type: str = ""
    content_url: str = ""
    download_url: str = ""
    thumbnail_url: str = ""
    error: str = ""


class FileLinksOut(Schema):
    """Links in request order; all expire at ``expires_at`` (Unix seconds)."""

    files: list[FileLinkOut]
    expires_at: int


class FileUploadIn(Schema):
    """One file to upload: target directory, name and exact byte count."""

    directory: str
    filename: str
    size: int = Field(..., ge=0)
    #: ``rename`` keeps existing files (``name_1.ext``), ``replace`` overwrites.
    conflict: Literal["rename", "replace"] = "rename"


class FileUploadOut(Schema):
    """Send the bytes with ``PUT upload_url`` (relative to the API base URL)."""

    upload_url: str
    expires_at: int
    max_bytes: int
