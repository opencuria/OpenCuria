"""Raw ASGI transport for signed workspace file links.

``GET|HEAD {PREFIX}content/<token>/<name>[?download=1]``
    The file, or one byte range of it (``Range: bytes=...``); a folder
    streams as ``.tar.gz``, a blob link the Git object it names. Media
    elements seek through ranges, so videos play while they load.
``GET {PREFIX}thumbnail/<token>``
    A tiny blurred JPEG for "tap to load" placeholders.
``PUT {PREFIX}upload/<token>``
    The raw request body becomes one workspace file.

Links carry their own signed authorization (:mod:`.tokens`), so ``<img>``,
``<video>``, downloads and native players use them directly. Django is
bypassed because it buffers request bodies before calling a view.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from email.utils import formatdate
from typing import Any
from urllib.parse import parse_qs, quote

import structlog

from . import tokens
from .services import FileAccessError, FileTransfer, get_workspace_file_service

log = structlog.get_logger(__name__)

ASGIReceive = Callable[[], Awaitable[dict]]
ASGISend = Callable[[dict], Awaitable[None]]

#: URL prefix of the transport (inside the API base URL ``/api/v1``).
PREFIX = "/api/v1/workspace-files/"

_ROUTE = re.compile(r"^(content|thumbnail|upload)/([^/]+)(?:/[^/]*)?$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
#: File versions are ``<size>-<mtime>``, Git blob versions their object id.
_ETAG = re.compile(r'"([0-9]+-[0-9]+|[0-9a-f]{40}|[0-9a-f]{64})"')
#: Workspace files may be markup: never let them run code on the API origin.
_SANDBOX = (
    "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; "
    "style-src 'unsafe-inline'"
)
_CORS = [
    (b"access-control-allow-origin", b"*"),
    (
        b"access-control-expose-headers",
        b"Content-Length, Content-Range, Content-Disposition, Accept-Ranges, ETag",
    ),
]
_CACHE_SECONDS = 3600


def _header(scope: dict, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return ""


def parse_range(value: str) -> tuple[int | None, int | None, int | None] | None:
    """Parse one ``bytes=`` range into ``(first, last, suffix)``; else ``None``.

    Multiple or malformed ranges are ignored (the whole file is served), as
    RFC 9110 allows.
    """
    match = _RANGE.match(value.strip().replace(" ", ""))
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        return None, None, int(last)
    if last and int(last) < int(first):
        return None
    return int(first), int(last) if last else None, None


def _disposition(kind: str, filename: str) -> bytes:
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "_")
    return (
        f'{kind}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    ).encode("latin-1", "replace")


def _security_headers(content_type: str) -> list[tuple[bytes, bytes]]:
    headers = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-accel-buffering", b"no"),
    ]
    # Browsers refuse to show PDFs in a sandboxed document.
    if content_type != "application/pdf":
        headers.append((b"content-security-policy", _SANDBOX.encode()))
    return headers + _CORS


async def _send_json(
    send: ASGISend,
    status: int,
    body: dict[str, Any],
    extra: list[tuple[bytes, bytes]] | None = None,
) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                (b"cache-control", b"no-store"),
                *_CORS,
                *(extra or []),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def _send_error(send: ASGISend, error: FileAccessError) -> None:
    extra: list[tuple[bytes, bytes]] = [(b"x-opencuria-error", error.code.encode())]
    if error.retry_after is not None:
        extra.append((b"retry-after", str(error.retry_after).encode()))
    if error.status == 416 and error.size is not None:
        extra.append((b"content-range", f"bytes */{error.size}".encode()))
    body: dict[str, Any] = {"detail": error.message, "code": error.code}
    if error.size is not None:
        body["size"] = error.size
    await _send_json(send, error.status, body, extra)


async def _wait_disconnect(receive: ASGIReceive) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass


async def _stream(
    send: ASGISend, receive: ASGIReceive, body: AsyncIterator[bytes]
) -> None:
    """Send *body*; stop pulling from the guest as soon as the client leaves."""

    async def pump() -> None:
        async for chunk in body:
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b""})

    pumping = asyncio.create_task(pump())
    watching = asyncio.create_task(_wait_disconnect(receive))
    try:
        await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (pumping, watching):
            task.cancel()
        await asyncio.gather(pumping, watching, return_exceptions=True)
    if not pumping.cancelled() and pumping.exception() is not None:
        # Headers are out: break the connection so the client sees a failure.
        raise pumping.exception()  # type: ignore[misc]


def _content_headers(
    transfer: FileTransfer, *, download: bool, version: str
) -> list[tuple[bytes, bytes]]:
    media = transfer.media
    headers = [
        (b"content-type", media.content_type.encode()),
        (
            b"content-disposition",
            _disposition(
                "inline" if media.inline and not download else "attachment",
                transfer.filename,
            ),
        ),
        (b"etag", f'"{transfer.etag}"'.encode()),
        (
            b"cache-control",
            (
                f"private, max-age={_CACHE_SECONDS}"
                if version and version == transfer.etag
                else "private, no-cache"
            ).encode(),
        ),
    ]
    # Git blobs have no modification time.
    if transfer.modified_at:
        headers.append(
            (b"last-modified", formatdate(transfer.modified_at, usegmt=True).encode())
        )
    if transfer.ranged:
        headers.append((b"accept-ranges", b"bytes"))
    return headers + _security_headers(media.content_type)


async def _content(
    scope: dict, receive: ASGIReceive, send: ASGISend, token: str
) -> None:
    claims = tokens.verify_token(token, "read") or tokens.verify_token(token, "blob")
    if claims is None:
        raise FileAccessError(404, "link_expired", "This link expired or is invalid.")
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    download = query.get("download", [""])[0] in ("1", "true")
    version = query.get("v", [""])[0]
    head = scope["method"] == "HEAD"
    byte_range = None
    if not head and not _header(scope, b"if-range"):
        byte_range = parse_range(_header(scope, b"range"))
    etag = _ETAG.search(_header(scope, b"if-none-match"))
    service = get_workspace_file_service()
    transfer = await service.open_content(
        claims,
        byte_range=byte_range,
        if_none_match=etag.group(1) if etag else "",
        head=head,
    )
    try:
        headers = _content_headers(transfer, download=download, version=version)
        if transfer.status == 304:
            await send(
                {"type": "http.response.start", "status": 304, "headers": headers}
            )
            await send({"type": "http.response.body", "body": b""})
            return
        partial = byte_range is not None
        length = transfer.size if head else transfer.length
        headers.append((b"content-length", str(length).encode()))
        if partial:
            headers.append(
                (
                    b"content-range",
                    f"bytes {transfer.first}-{transfer.last}/{transfer.size}".encode(),
                )
            )
        await send(
            {
                "type": "http.response.start",
                "status": 206 if partial else 200,
                "headers": headers,
            }
        )
        if head or transfer.body is None:
            await send({"type": "http.response.body", "body": b""})
            return
        await _stream(send, receive, transfer.body)
    finally:
        await transfer.close()


async def _thumbnail(scope: dict, send: ASGISend, token: str) -> None:
    claims = tokens.verify_token(token, "read")
    if claims is None:
        raise FileAccessError(404, "link_expired", "This link expired or is invalid.")
    query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
    version = query.get("v", [""])[0]
    data = await get_workspace_file_service().thumbnail(claims, version)
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"image/jpeg"),
                (b"content-length", str(len(data)).encode()),
                (
                    b"cache-control",
                    b"private, max-age=86400" if version else b"private, no-cache",
                ),
                *_security_headers("image/jpeg"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": data})


async def _upload(
    scope: dict, receive: ASGIReceive, send: ASGISend, token: str
) -> None:
    claims = tokens.verify_token(token, "upload")
    if claims is None:
        raise FileAccessError(404, "link_expired", "This upload link expired.")
    raw_length = _header(scope, b"content-length")
    content_length = int(raw_length) if raw_length.isdigit() else None

    async def body() -> AsyncIterator[bytes]:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise FileAccessError(
                    400, "upload_incomplete", "The upload was aborted."
                )
            if message.get("body"):
                yield message["body"]
            if not message.get("more_body", False):
                return

    result = await get_workspace_file_service().upload(
        claims, body(), content_length=content_length
    )
    await _send_json(send, 201, {"path": result.path, "size": result.size})


async def workspace_files_app(
    scope: dict, receive: ASGIReceive, send: ASGISend
) -> None:
    """Serve one request below :data:`PREFIX`."""
    if scope["type"] != "http":
        return
    method = scope["method"]
    if method == "OPTIONS":
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [
                    *_CORS,
                    (b"access-control-allow-methods", b"GET, HEAD, PUT, OPTIONS"),
                    (
                        b"access-control-allow-headers",
                        b"Content-Type, Range, If-None-Match, If-Range",
                    ),
                    (b"access-control-max-age", b"86400"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})
        return
    match = _ROUTE.match(scope.get("path", "")[len(PREFIX) :])
    try:
        if match is None:
            raise FileAccessError(404, "not_found", "Not found.")
        action, token = match.groups()
        allowed = {
            "content": ("GET", "HEAD"),
            "thumbnail": ("GET",),
            "upload": ("PUT",),
        }
        if method not in allowed[action]:
            raise FileAccessError(405, "method_not_allowed", "Method not allowed.")
        if action == "content":
            await _content(scope, receive, send, token)
        elif action == "thumbnail":
            await _thumbnail(scope, send, token)
        else:
            await _upload(scope, receive, send, token)
    except FileAccessError as exc:
        with contextlib.suppress(Exception):
            await _send_error(send, exc)
    except Exception:
        log.exception("workspace_file_request_failed", action=match and match.group(1))
        raise
