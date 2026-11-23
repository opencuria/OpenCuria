"""Signed, path-bound links to workspace files.

Browsers load ``<img>``/``<video>`` sources and downloads without an
``Authorization`` header, so file URLs carry a signed token instead of an
access token. A token names one user, organization, workspace and path, the
operation it allows and its expiry. Read tokens expire on hour boundaries and
carry no nonce, so the same file keeps the same URL for a while and HTTP caches
work; access is re-checked on every request, so revoking membership or deleting
the workspace takes effect immediately. Blob tokens name one immutable Git
object instead of the file on disk, so a link streams the version it was
signed for.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

from django.core import signing

#: Read links stay valid for one to two hours (expiry rounds to the hour).
READ_LINK_MIN_LIFETIME_SECONDS = 3600
#: An upload must start within this many seconds after its link was issued.
UPLOAD_LINK_LIFETIME_SECONDS = 15 * 60

_SALT = "opencuria.workspace-file-link"

FileOperation = Literal["read", "upload", "blob"]
UploadConflict = Literal["rename", "replace"]

_CODES: dict[str, str] = {"read": "r", "upload": "u", "blob": "b"}
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def is_object_id(value: object) -> bool:
    """Whether *value* is a full SHA-1 or SHA-256 Git object id."""
    return isinstance(value, str) and _OBJECT_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class FileLinkClaims:
    """What a verified file link allows."""

    operation: FileOperation
    user_id: int
    organization_id: str
    workspace_id: str
    #: Read: the file or directory. Upload: the target directory. Blob: the
    #: file the blob is a version of (its name and type).
    path: str
    expires_at: int
    #: Upload only: requested file name, exact byte count and name conflicts.
    filename: str = ""
    size: int = 0
    conflict: UploadConflict = "rename"
    #: Blob only: the repository and the Git object streamed for ``path``.
    repo: str = ""
    object_id: str = ""


def read_link_expiry(now: float | None = None) -> int:
    """Return the shared expiry of read links issued at *now*."""
    current = int(time.time() if now is None else now)
    return (current // 3600 + 1) * 3600 + READ_LINK_MIN_LIFETIME_SECONDS


def issue_read_token(
    *,
    user_id: int,
    organization_id: str,
    workspace_id: str,
    path: str,
    expires_at: int,
) -> str:
    """Return a deterministic token that lets *user_id* read *path*."""
    payload = {
        "o": "r",
        "u": int(user_id),
        "g": str(organization_id),
        "w": str(workspace_id),
        "p": path,
        "e": int(expires_at),
    }
    return signing.Signer(salt=_SALT).sign_object(payload, compress=True)


def issue_blob_token(
    *,
    user_id: int,
    organization_id: str,
    workspace_id: str,
    path: str,
    repo: str,
    object_id: str,
    expires_at: int,
) -> str:
    """Return a deterministic token that lets *user_id* read one Git blob."""
    payload = {
        "o": "b",
        "u": int(user_id),
        "g": str(organization_id),
        "w": str(workspace_id),
        "p": path,
        "r": repo,
        "b": object_id,
        "e": int(expires_at),
    }
    return signing.Signer(salt=_SALT).sign_object(payload, compress=True)


def issue_upload_token(
    *,
    user_id: int,
    organization_id: str,
    workspace_id: str,
    directory: str,
    filename: str,
    size: int,
    conflict: UploadConflict,
    now: float | None = None,
) -> tuple[str, int]:
    """Return a single-file upload token and its expiry."""
    expires_at = int(time.time() if now is None else now) + (
        UPLOAD_LINK_LIFETIME_SECONDS
    )
    payload = {
        "o": "u",
        "u": int(user_id),
        "g": str(organization_id),
        "w": str(workspace_id),
        "p": directory,
        "f": filename,
        "s": int(size),
        "c": conflict,
        "e": expires_at,
    }
    return signing.Signer(salt=_SALT).sign_object(payload, compress=True), expires_at


def verify_token(
    token: str, operation: FileOperation, *, now: float | None = None
) -> FileLinkClaims | None:
    """Return the claims of a valid, unexpired token for *operation*."""
    try:
        payload = signing.Signer(salt=_SALT).unsign_object(token)
    except (signing.BadSignature, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = _CODES[operation]
    user_id, expires_at = payload.get("u"), payload.get("e")
    texts = [payload.get(key) for key in ("g", "w", "p")]
    if (
        payload.get("o") != code
        or not isinstance(user_id, int)
        or not isinstance(expires_at, int)
        or not all(isinstance(value, str) and value for value in texts)
        or expires_at <= int(time.time() if now is None else now)
    ):
        return None
    organization_id, workspace_id, path = texts
    if operation == "read":
        return FileLinkClaims(
            "read", user_id, organization_id, workspace_id, path, expires_at
        )
    if operation == "blob":
        repo, object_id = payload.get("r"), payload.get("b")
        if not isinstance(repo, str) or not repo or not is_object_id(object_id):
            return None
        return FileLinkClaims(
            "blob",
            user_id,
            organization_id,
            workspace_id,
            path,
            expires_at,
            repo=repo,
            object_id=object_id,
        )
    filename, size, conflict = payload.get("f"), payload.get("s"), payload.get("c")
    if (
        not isinstance(filename, str)
        or not filename
        or not isinstance(size, int)
        or size < 0
        or conflict not in ("rename", "replace")
    ):
        return None
    return FileLinkClaims(
        "upload",
        user_id,
        organization_id,
        workspace_id,
        path,
        expires_at,
        filename=filename,
        size=size,
        conflict=conflict,
    )
