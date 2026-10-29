"""Build bounded canonical media blocks and hydrate authorized workspace context.

The base64 block representation is shared across engines. Historical Claude
limit names remain compatibility aliases for the existing bounded contract.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ...access.base import WorkspaceAccessor, sanitize_harness_path
from ...access.file_types import FileContent
from ...access.runner_accessor import RunnerAccessorError
from ...image_processing import (
    MAX_IMAGE_SOURCE_BYTES,
    image_path_notice,
    prepare_image,
)
from ...images import (
    MARKDOWN_IMAGE_RE,
    parse_markdown_link_dest,
    resolve_workspace_image_path,
)
from ...providers.base import LLMMessage
from ...tools.files import (
    MAX_MEDIA_INGEST_BYTES,
    SUPPORTED_IMAGE_MIMES,
    sniff_attachment_mime,
)
from .security import redact_arguments, redact_text, redact_value

MAX_CLAUDE_IMAGE_BASE64_CHARS = 5_000_000
MAX_CLAUDE_DOCUMENT_BYTES = MAX_MEDIA_INGEST_BYTES
MAX_USER_IMAGE_BLOCKS = 16
MAX_USER_MEDIA_BLOCKS = 16
MAX_USER_CONTENT_BYTES = 12 * 1024 * 1024
MAX_CLAUDE_TOOL_ARGUMENT_BYTES = 64 * 1024
_DATA_URL = re.compile(r"^data:([^;,]+);base64,([A-Za-z0-9+/]*={0,2})$")
_FILE_REFERENCE = re.compile(r'(?<![\w@])@file:(/workspace/[^\s<>"`\\]+)')
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_MEDIA_EXTENSIONS = (*_IMAGE_EXTENSIONS, ".pdf")
AuthorizeRead = Callable[[str], Awaitable[bool]]
_MediaKey = tuple[str, str]


class MediaAuthorizationError(RuntimeError):
    """Raised when a user-referenced media file is not authorized for reading."""


async def _read_media_no_follow(
    accessor: WorkspaceAccessor, path: str, *, max_size: int
) -> FileContent:
    """Read media without following symlinks, never falling back to normal reads."""
    reader = getattr(accessor, "read_file_no_follow", None)
    if callable(reader):
        return await reader(path, max_size=max_size)
    from ...access.safe_files import read_file_no_follow

    return await read_file_no_follow(accessor, path, max_size=max_size)


class MediaLimitError(ValueError):
    """Raised when user media exceeds a bounded Claude request limit."""


@dataclass(frozen=True)
class _MediaReference:
    """A user-authored media reference and its exact source span."""

    start: int
    end: int
    path: str
    kind: str
    markdown: bool


@dataclass
class _RequestPlan:
    """Single preflight accounting for text, inline media, and user references."""

    references: Counter[_MediaKey] = field(default_factory=Counter)
    text_bytes: int = 0
    media_chars: int = 0
    image_bytes: int = 0
    document_bytes: int = 0
    image_count: int = 0
    media_count: int = 0
    images_as_paths: bool = False
    include_documents: bool = True
    enforce_content_limit: bool = True

    def add_text(self, text: str) -> None:
        """Account for UTF-8 text in the shared request budget."""
        self.text_bytes += len(text.encode("utf-8"))

    def add_blocks(self, blocks: list[dict[str, Any]]) -> None:
        """Validate and account for already-inline text, images and documents."""
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                self.add_text(str(block.get("text") or ""))
                continue
            source = block.get("source")
            if not isinstance(source, dict):
                raise ValueError("Claude media block has no valid source")
            mime, encoded = source.get("media_type"), source.get("data")
            if not isinstance(mime, str) or not isinstance(encoded, str):
                raise ValueError("Claude media block has no valid base64 source")
            if kind == "image":
                checked = _image_block(mime, encoded)
                self.image_count += 1
                self.image_bytes += len(
                    base64.b64decode(checked["source"]["data"], validate=True)
                )
            elif kind == "document":
                checked = _document_block(mime, encoded, str(block.get("title") or ""))
                self.document_bytes += len(
                    base64.b64decode(checked["source"]["data"], validate=True)
                )
            else:
                raise ValueError(
                    f"Unsupported Claude content block: {kind or 'unknown'}"
                )
            self.media_count += 1
            self.media_chars += len(encoded)

    def add_user_text(self, text: str) -> None:
        """Account for text and media references without opening any files."""
        self.add_text(text)
        for reference in _media_references(text):
            if reference.kind == "image" and self.images_as_paths:
                continue
            if reference.kind == "document" and not self.include_documents:
                continue
            self.references[(reference.path, reference.kind)] += 1

    def check(self) -> None:
        """Reject impossible requests before reading any workspace media."""
        images = self.image_count + sum(
            count for (_path, kind), count in self.references.items() if kind == "image"
        )
        media = self.media_count + sum(self.references.values())
        if images > MAX_USER_IMAGE_BLOCKS:
            raise MediaLimitError("Claude request has too many image blocks")
        if media > MAX_USER_MEDIA_BLOCKS:
            raise MediaLimitError("Claude request has too many media blocks")
        if self.image_bytes > MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 2:
            raise MediaLimitError("Claude aggregate image limit exceeded")
        if self.document_bytes > MAX_CLAUDE_DOCUMENT_BYTES:
            raise MediaLimitError("Claude aggregate PDF limit exceeded")
        if (
            self.enforce_content_limit
            and self.text_bytes + self.media_chars > MAX_USER_CONTENT_BYTES
        ):
            raise MediaLimitError("Claude request content exceeds the input limit")


def _workspace_media_path(path: str) -> str | None:
    """Return a safe workspace media path, rejecting traversal and external paths."""
    if not path.startswith("/workspace/") or any(
        part in {".", ".."} for part in path.split("/")
    ):
        return None
    try:
        normalized = sanitize_harness_path(path)
    except ValueError:
        return None
    return (
        normalized
        if normalized == path and normalized.startswith("/workspace/")
        else None
    )


def _image_block(mime: str, encoded: str) -> dict[str, Any]:
    """Return an Anthropic image block from strictly validated base64."""
    normalized = "image/jpeg" if mime.lower() == "image/jpg" else mime.lower()
    if normalized not in SUPPORTED_IMAGE_MIMES:
        raise ValueError(f"Unsupported Claude image media type: {mime}")
    if not encoded or len(encoded) > MAX_CLAUDE_IMAGE_BASE64_CHARS:
        raise ValueError("Claude image exceeds the per-image input limit")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Claude image contains invalid base64 data") from None
    if not payload:
        raise ValueError("Claude image is empty")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": normalized, "data": encoded},
    }


def _document_block(mime: str, encoded: str, filename: str = "") -> dict[str, Any]:
    """Return a Claude PDF document block from strictly validated base64."""
    if mime.lower() != "application/pdf" or not encoded:
        raise ValueError("Unsupported Claude document media type")
    if len(encoded) > MAX_CLAUDE_DOCUMENT_BYTES * 4 // 3 + 4:
        raise ValueError("Claude PDF exceeds the input limit")
    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Claude PDF contains invalid base64 data") from None
    if not payload.startswith(b"%PDF-") or len(payload) > MAX_CLAUDE_DOCUMENT_BYTES:
        raise ValueError("Claude PDF is invalid or exceeds the input limit")
    block: dict[str, Any] = {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": encoded},
    }
    if filename:
        block["title"] = filename[:255]
    return block


def data_url_block(url: str, *, filename: str = "") -> dict[str, Any]:
    """Parse a data URL without remote fetches or permissive MIME coercion."""
    match = _DATA_URL.fullmatch(url)
    if match is None:
        raise ValueError("Claude multimodal input must use a valid base64 data URL")
    mime, encoded = match.groups()
    if mime.lower() == "application/pdf":
        return _document_block(mime, encoded, filename)
    return _image_block(mime, encoded)


def _part_blocks(part: Any) -> list[dict[str, Any]]:
    """Convert canonical harness text/image/file parts to Anthropic blocks."""
    if not isinstance(part, dict):
        raise ValueError("Claude message content part must be an object")
    kind = str(part.get("type") or "")
    if kind == "text":
        text = part.get("text")
        return (
            [{"type": "text", "text": text}] if isinstance(text, str) and text else []
        )
    if kind == "image_url":
        value = part.get("image_url")
        url = value.get("url") if isinstance(value, dict) else value
        if not isinstance(url, str):
            raise ValueError("Claude image_url part has no data URL")
        block = data_url_block(url)
        if block["type"] != "image":
            raise ValueError("Claude image_url part must contain an image")
        return [block]
    if kind == "file":
        url = part.get("url")
        if not isinstance(url, str):
            raise ValueError("Claude file part has no data URL")
        block = data_url_block(url, filename=str(part.get("filename") or ""))
        if block["type"] != "document":
            raise ValueError("Claude file part must contain a PDF")
        mime = str(part.get("mime") or "").lower()
        if mime and mime != "application/pdf":
            raise ValueError("Claude file MIME does not match its PDF data URL")
        return [block]
    raise ValueError(f"Unsupported Claude message part: {kind or 'unknown'}")


def content_blocks(content: str | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert canonical message content without interpreting arbitrary URLs."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise ValueError("Claude message content has an unsupported shape")
    return [block for part in content for block in _part_blocks(part)]


def checked_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Validate the complete serialized request and simplify text-only input."""
    plan = _RequestPlan()
    plan.add_blocks(blocks)
    plan.check()
    if not plan.media_count:
        return "".join(str(block.get("text") or "") for block in blocks)
    return blocks


def mcp_content_blocks(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve checked text/image ordering when returning managed MCP results."""
    canonical = []
    for item in content:
        if item.get("type") == "inputText":
            canonical.append({"type": "text", "text": item["text"]})
        elif item.get("type") == "inputImage":
            canonical.append(
                {"type": "image_url", "image_url": {"url": item["imageUrl"]}}
            )
        else:
            canonical.append(item)
    blocks = content_blocks(canonical)
    checked_content(blocks)
    result = []
    for block in blocks:
        if block["type"] == "text":
            result.append(block)
        elif block["type"] == "image":
            result.append(
                {
                    "type": "image",
                    "mimeType": block["source"]["media_type"],
                    "data": block["source"]["data"],
                    "_meta": {"codex/imageDetail": "original"},
                }
            )
        else:
            raise ValueError("Inline MCP results support text and images")
    return result or [{"type": "text", "text": ""}]


def _code_mask(text: str) -> list[bool]:
    """Mark fenced and inline code so code examples never trigger file reads."""
    mask = [False] * len(text)
    fence: tuple[str, int] | None = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r" {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            char, length = fence
            closing = re.match(
                rf" {{0,3}}{re.escape(char)}{{{length},}}\s*$", line.rstrip("\r\n")
            )
            if closing:
                fence = None
            mask[offset : offset + len(line)] = [True] * len(line)
        elif marker:
            run = marker.group(1)
            fence = (run[0], len(run))
            mask[offset : offset + len(line)] = [True] * len(line)
        offset += len(line)

    ticks = re.compile(r"(?<!`)`+(?!`)")
    cursor = 0
    while match := ticks.search(text, cursor):
        if mask[match.start()]:
            cursor = match.end()
            continue
        delimiter = "`" * (match.end() - match.start())
        close = re.compile(rf"(?<!`){re.escape(delimiter)}(?!`)").search(
            text, match.end()
        )
        if close is None or mask[close.start()]:
            cursor = match.end()
            continue
        mask[match.start() : close.end()] = [True] * (close.end() - match.start())
        cursor = close.end()
    return mask


def _media_references(text: str) -> list[_MediaReference]:
    """Find supported, safe media references outside code using shared spans."""
    masked = _code_mask(text)
    found: list[_MediaReference] = []
    for match in MARKDOWN_IMAGE_RE.finditer(text):
        if any(masked[match.start() : match.end()]):
            continue
        raw_dest = match.group(2)
        dest = parse_markdown_link_dest(raw_dest)
        path = resolve_workspace_image_path(raw_dest)
        if (
            path is None
            or ".." in dest.split("/")
            or not path.lower().endswith(_IMAGE_EXTENSIONS)
        ):
            continue
        found.append(_MediaReference(match.start(), match.end(), path, "image", True))

    for match in _FILE_REFERENCE.finditer(text):
        raw_path = match.group(1)
        path = raw_path.rstrip("),;!?]")
        end = match.start(1) + len(path)
        if any(masked[match.start() : end]):
            continue
        suffix = text[end:]
        if not path or (suffix and not re.match(r"[\s),;!?\]]", suffix)):
            continue
        if any(masked[match.start() : end]):
            continue
        safe_path = _workspace_media_path(path)
        if safe_path is None or not safe_path.lower().endswith(_MEDIA_EXTENSIONS):
            continue
        kind = "document" if safe_path.lower().endswith(".pdf") else "image"
        found.append(_MediaReference(match.start(), end, safe_path, kind, False))

    # Markdown image destinations and visible @file text are mutually exclusive
    # spans. Prefer the larger Markdown construct if unusual text overlaps them.
    found.sort(key=lambda item: (item.start, not item.markdown, -item.end))
    ordered: list[_MediaReference] = []
    end = -1
    for reference in found:
        if reference.start < end:
            continue
        ordered.append(reference)
        end = reference.end
    return ordered


def _tool_call_text(call: dict[str, Any]) -> str:
    """Serialize a historical tool call with redacted, bounded arguments."""
    function = call.get("function")
    function = function if isinstance(function, dict) else {}
    name = str(call.get("name") or function.get("name") or "tool")
    call_id = str(call.get("id") or "")
    arguments = call.get("arguments", function.get("arguments", ""))
    if isinstance(arguments, dict):
        safe: Any = redact_arguments(arguments)
        serialized = json.dumps(safe, ensure_ascii=False, separators=(",", ":"))
    elif isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            serialized = redact_text(arguments)
        else:
            serialized = json.dumps(
                redact_value(parsed), ensure_ascii=False, separators=(",", ":")
            )
    else:
        serialized = json.dumps(
            redact_value(arguments), ensure_ascii=False, separators=(",", ":")
        )
    raw = serialized.encode("utf-8")
    if len(raw) > MAX_CLAUDE_TOOL_ARGUMENT_BYTES:
        serialized = (
            raw[:MAX_CLAUDE_TOOL_ARGUMENT_BYTES].decode("utf-8", errors="ignore")
            + "…[tool arguments truncated]"
        )
    return (
        f"\nHistorical tool call: name={redact_text(name)} "
        f"id={redact_text(call_id)} arguments={serialized}"
    )


def _user_texts(content: str | list[dict[str, Any]] | None) -> list[str]:
    """Return user text segments, leaving structured media parts for normal mapping."""
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            str(part["text"])
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
    return []


def _image_count(messages: list[LLMMessage]) -> int:
    """Count the whole request before opening or decoding any images."""
    count = 0
    for message in messages:
        if message.role == "user":
            count += sum(
                reference.kind == "image"
                for text in _user_texts(message.content)
                for reference in _media_references(text)
            )
        if isinstance(message.content, list):
            count += sum(
                isinstance(part, dict) and part.get("type") == "image_url"
                for part in message.content
            )
    return count


async def _prepare_inline_images(
    content: str | list[dict[str, Any]] | None, *, paths_only: bool
) -> str | list[dict[str, Any]] | None:
    """Make historical inline images safe without reading any referenced paths."""
    if not isinstance(content, list):
        return content
    prepared: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            prepared.append(part)
            continue
        label = str(part.get("path") or part.get("filename") or "image attachment")
        reason = "too many images in this request"
        if not paths_only:
            try:
                value = part.get("image_url")
                url = value.get("url") if isinstance(value, dict) else value
                match = _DATA_URL.fullmatch(url) if isinstance(url, str) else None
                if (
                    match is None
                    or match.group(1).lower() not in SUPPORTED_IMAGE_MIMES
                    or len(match.group(2)) > (MAX_IMAGE_SOURCE_BYTES + 2) // 3 * 4
                ):
                    raise ValueError("Unsupported or oversized image data URL")
                preview = await prepare_image(
                    base64.b64decode(match.group(2), validate=True)
                )
                encoded = base64.b64encode(preview.content).decode("ascii")
                prepared.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{preview.mime};base64,{encoded}"},
                    }
                )
                continue
            except Exception:
                # Cancellation is a BaseException and must still stop the run.
                reason = "preview unavailable; inspect the file if needed"
        prepared.append({"type": "text", "text": image_path_notice(label, reason)})
    return prepared


def _request_plan(
    prompt: str,
    history: list[LLMMessage] | None,
    *,
    include_history: bool,
    images_as_paths: bool = False,
    include_documents: bool = True,
    enforce_content_limit: bool = True,
) -> _RequestPlan:
    """Preflight exact text/media inputs and the same user refs later hydrated."""
    plan = _RequestPlan(
        images_as_paths=images_as_paths,
        include_documents=include_documents,
        enforce_content_limit=enforce_content_limit,
    )
    if include_history and history:
        plan.add_text("Prior conversation context:")
        for message in history:
            if message.role == "tool" and message.tool_call_id:
                prefix = f"\n\nTool result id={redact_text(message.tool_call_id)}: "
            else:
                prefix = f"\n\n{str(message.role or 'user').title()}: "
            plan.add_text(prefix)
            if message.role == "user":
                for text in _user_texts(message.content):
                    plan.add_user_text(text)
                if isinstance(message.content, list):
                    plan.add_blocks(
                        [
                            block
                            for part in message.content
                            if not (
                                isinstance(part, dict) and part.get("type") == "text"
                            )
                            for block in _part_blocks(part)
                        ]
                    )
            else:
                plan.add_blocks(content_blocks(message.content))
            for call in message.tool_calls or []:
                if isinstance(call, dict):
                    plan.add_text(_tool_call_text(call))
        plan.add_text("\n\nCurrent user request: ")
    plan.add_user_text(prompt.strip())
    plan.check()
    return plan


async def _prepare_request(
    prompt: str,
    history: list[LLMMessage] | None,
    *,
    include_history: bool,
    include_documents: bool = True,
    enforce_content_limit: bool = True,
) -> tuple[_RequestPlan, list[LLMMessage]]:
    """Fall back to paths before request-wide image limits can reject a run."""
    prior = list(history or [])
    messages = prior if include_history else []
    # Resumed CLI sessions already retain history: count it without replaying it.
    image_count = _image_count([*prior, LLMMessage(role="user", content=prompt)])
    paths_only = image_count > MAX_USER_IMAGE_BLOCKS
    while True:
        prepared = [
            replace(
                message,
                content=await _prepare_inline_images(
                    message.content, paths_only=paths_only
                ),
            )
            for message in messages
        ]
        try:
            plan = _request_plan(
                prompt,
                prepared,
                include_history=include_history,
                images_as_paths=paths_only,
                include_documents=include_documents,
                enforce_content_limit=enforce_content_limit,
            )
            return plan, prepared
        except MediaLimitError:
            if paths_only or not image_count:
                raise
            paths_only = True


class _MediaLoader:
    """Authorize, cache, and meter user media reads against one preflight plan."""

    def __init__(
        self,
        accessor: WorkspaceAccessor,
        plan: _RequestPlan,
        authorize_read: AuthorizeRead | None,
    ) -> None:
        """Bind no-follow reads and per-path authorization to a preflight plan."""
        self._accessor = accessor
        self._plan = plan
        self._authorize_read = authorize_read
        self._loaded: dict[_MediaKey, tuple[FileContent, str]] = {}
        self._failed: dict[_MediaKey, Exception] = {}
        self._authorized: set[str] = set()
        self._remaining = plan.references.copy()
        self._file_chars = 0
        self._file_image_bytes = 0
        self._file_pdf_bytes = 0

    def _read_limit(self, key: _MediaKey) -> int:
        """Return a raw-byte cap accounting for every repeated emitted block."""
        path, kind = key
        uses = self._remaining[key]
        available_chars = (
            MAX_USER_CONTENT_BYTES
            - self._plan.text_bytes
            - self._plan.media_chars
            - self._file_chars
        )
        limit = min(
            available_chars * 3 // (4 * uses),
            MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 4
            if kind == "image"
            else MAX_CLAUDE_DOCUMENT_BYTES,
        )
        if kind == "image":
            limit = min(
                limit,
                (
                    MAX_CLAUDE_IMAGE_BASE64_CHARS * 3 // 2
                    - self._plan.image_bytes
                    - self._file_image_bytes
                )
                // uses,
            )
        else:
            limit = min(
                limit,
                (
                    MAX_CLAUDE_DOCUMENT_BYTES
                    - self._plan.document_bytes
                    - self._file_pdf_bytes
                )
                // uses,
            )
        if limit <= 0:
            raise MediaLimitError("Claude aggregate media limit exceeded")
        return limit

    async def load(self, path: str, kind: str) -> dict[str, Any]:
        """Return one validated media block, reading/authorizing a path once."""
        safe_path = _workspace_media_path(path)
        if safe_path is None:
            raise ValueError("User media reference must stay inside /workspace")
        key = (safe_path, kind)
        if self._remaining[key] <= 0:
            raise ValueError("Unexpected media reference after request preflight")
        if key in self._failed:
            raise self._failed[key]
        if key not in self._loaded:
            try:
                if (
                    safe_path not in self._authorized
                    and self._authorize_read is not None
                ):
                    if not await self._authorize_read(safe_path):
                        raise MediaAuthorizationError(
                            f"Access to user-referenced media was denied: {safe_path}"
                        )
                    self._authorized.add(safe_path)
                limit = (
                    MAX_IMAGE_SOURCE_BYTES if kind == "image" else self._read_limit(key)
                )
                stored = await _read_media_no_follow(
                    self._accessor,
                    safe_path,
                    max_size=limit if kind == "image" else limit + 1,
                )
                if (
                    stored.truncated
                    or stored.size > limit
                    or len(stored.content) > limit
                ):
                    raise MediaLimitError(
                        f"Workspace {kind} exceeds Claude's input limit"
                    )
                mime = sniff_attachment_mime(
                    stored.content[:4096], safe_path, getattr(stored, "mime", "")
                )
                allowed = (
                    SUPPORTED_IMAGE_MIMES if kind == "image" else {"application/pdf"}
                )
                if mime not in allowed:
                    raise ValueError(f"Unsupported Claude workspace media type: {mime}")
                if kind == "image":
                    preview = await prepare_image(stored.content)
                    stored = FileContent(content=preview.content, mime=preview.mime)
                    mime = preview.mime
                self._loaded[key] = (stored, mime)
            except Exception as exc:
                self._failed[key] = exc
                raise
        stored, mime = self._loaded[key]
        if kind == "image" and len(stored.content) > self._read_limit(key):
            raise MediaLimitError("Image preview exceeds the remaining request budget")
        encoded = base64.b64encode(stored.content).decode("ascii")
        block = (
            _image_block(mime, encoded)
            if kind == "image"
            else _document_block(mime, encoded, os.path.basename(safe_path))
        )
        self._remaining[key] -= 1
        self._file_chars += len(encoded)
        if kind == "image":
            self._file_image_bytes += len(stored.content)
        else:
            self._file_pdf_bytes += len(stored.content)
        return block

    def skip(self, path: str, kind: str) -> None:
        """Release one preflight reference that a missing Markdown image skipped."""
        key = (path, kind)
        if self._remaining[key] > 0:
            self._remaining[key] -= 1


async def _user_text_blocks(text: str, loader: _MediaLoader) -> list[dict[str, Any]]:
    """Hydrate user refs in-place while retaining visible text and punctuation."""
    output: list[dict[str, Any]] = []
    cursor = 0
    for reference in _media_references(text):
        if reference.kind == "document" and not loader._plan.include_documents:
            continue
        if reference.start > cursor:
            output.extend(content_blocks(text[cursor : reference.start]))
        if reference.kind == "image":
            # Retain the original path even when a preview is successfully sent.
            output.extend(content_blocks(text[reference.start : reference.end]))
            if loader._plan.images_as_paths:
                output.extend(
                    content_blocks(
                        image_path_notice(
                            reference.path, "too many images or image budget exceeded"
                        )
                    )
                )
            else:
                try:
                    output.append(await loader.load(reference.path, reference.kind))
                except Exception:
                    loader.skip(reference.path, reference.kind)
                    output.extend(
                        content_blocks(
                            image_path_notice(
                                reference.path,
                                "preview unavailable; inspect the file if needed",
                            )
                        )
                    )
            cursor = reference.end
            continue
        if not reference.markdown:
            output.extend(content_blocks(text[reference.start : reference.end]))
        try:
            output.append(await loader.load(reference.path, reference.kind))
        except MediaLimitError:
            raise
        except (FileNotFoundError, OSError, RunnerAccessorError, ValueError) as exc:
            if not reference.markdown or isinstance(exc, MediaLimitError):
                raise
            loader.skip(reference.path, reference.kind)
            output.extend(content_blocks(text[reference.start : reference.end]))
        cursor = reference.end
    if cursor < len(text):
        output.extend(content_blocks(text[cursor:]))
    return output


async def _history_message_blocks(
    message: LLMMessage, loader: _MediaLoader
) -> list[dict[str, Any]]:
    """Hydrate only user-authored history; preserve other roles and results."""
    if message.role == "user":
        if isinstance(message.content, str):
            blocks = await _user_text_blocks(message.content, loader)
        elif isinstance(message.content, list):
            blocks = []
            for part in message.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = part.get("text")
                    if isinstance(text, str):
                        blocks.extend(await _user_text_blocks(text, loader))
                        continue
                blocks.extend(_part_blocks(part))
        else:
            blocks = content_blocks(message.content)
    else:
        blocks = content_blocks(message.content)
    for call in message.tool_calls or []:
        if isinstance(call, dict):
            blocks.extend(content_blocks(_tool_call_text(call)))
    return blocks


async def build_user_content(
    prompt: str,
    *,
    accessor: WorkspaceAccessor,
    history: list[LLMMessage] | None = None,
    include_history: bool = True,
    authorize_read: AuthorizeRead | None = None,
) -> str | list[dict[str, Any]]:
    """Build shared media content; unavailable images always remain as paths."""
    current_prompt = prompt.strip()
    plan, history = await _prepare_request(
        current_prompt, history, include_history=include_history
    )
    loader = _MediaLoader(accessor, plan, authorize_read)
    blocks: list[dict[str, Any]] = []
    if include_history and history:
        blocks.append({"type": "text", "text": "Prior conversation context:"})
        for message in history:
            role = str(message.role or "user").title()
            if message.role == "tool" and message.tool_call_id:
                prefix = f"\n\nTool result id={redact_text(message.tool_call_id)}: "
            else:
                prefix = f"\n\n{role}: "
            blocks.append({"type": "text", "text": prefix})
            blocks.extend(await _history_message_blocks(message, loader))
        blocks.append({"type": "text", "text": "\n\nCurrent user request: "})
    blocks.extend(await _user_text_blocks(current_prompt, loader))
    return checked_content(blocks)


def _canonical_content(blocks: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Translate shared media blocks into the native provider content contract."""
    content: list[dict[str, Any]] = []
    for block in blocks:
        if block["type"] == "text":
            content.append(block)
        else:
            source = block["source"]
            url = f"data:{source['media_type']};base64,{source['data']}"
            if block["type"] == "image":
                content.append({"type": "image_url", "image_url": {"url": url}})
            else:
                content.append(
                    {
                        "type": "file",
                        "url": url,
                        "mime": source["media_type"],
                        "filename": block.get("title", ""),
                    }
                )
    if not any(part["type"] != "text" for part in content):
        return "".join(part["text"] for part in content)
    return content


async def hydrate_image_messages(
    messages: list[LLMMessage],
    accessor: WorkspaceAccessor,
    *,
    authorize_read: AuthorizeRead | None = None,
) -> list[LLMMessage]:
    """Apply the shared image policy without flattening native conversation roles."""
    plan, prepared = await _prepare_request(
        "",
        messages,
        include_history=True,
        include_documents=False,
        enforce_content_limit=False,
    )
    loader = _MediaLoader(accessor, plan, authorize_read)
    return [
        replace(
            message,
            content=_canonical_content(await _history_message_blocks(message, loader))
            if message.role == "user"
            else message.content,
        )
        for message in prepared
    ]


def limit_image_messages(messages: list[LLMMessage]) -> list[LLMMessage]:
    """Keep subsequent tool and steering images within the same request budget."""
    images = [
        part
        for message in messages
        if isinstance(message.content, list)
        for part in message.content
        if isinstance(part, dict) and part.get("type") == "image_url"
    ]
    chars = 0
    for part in images:
        value = part.get("image_url")
        url = value.get("url") if isinstance(value, dict) else value
        if isinstance(url, str):
            chars += len(url)
    if (
        len(images) <= MAX_USER_IMAGE_BLOCKS
        and chars <= MAX_CLAUDE_IMAGE_BASE64_CHARS * 2
    ):
        return messages
    return [
        replace(
            message,
            content=[
                {
                    "type": "text",
                    "text": image_path_notice(
                        str(
                            part.get("path")
                            or part.get("filename")
                            or "referenced above"
                        ),
                        "image budget exceeded; read files individually",
                    ),
                }
                if isinstance(part, dict) and part.get("type") == "image_url"
                else part
                for part in message.content
            ],
        )
        if isinstance(message.content, list)
        else message
        for message in messages
    ]


__all__ = [
    "MAX_CLAUDE_DOCUMENT_BYTES",
    "MAX_CLAUDE_IMAGE_BASE64_CHARS",
    "MAX_CLAUDE_TOOL_ARGUMENT_BYTES",
    "MAX_USER_CONTENT_BYTES",
    "MAX_USER_IMAGE_BLOCKS",
    "MAX_USER_MEDIA_BLOCKS",
    "MediaAuthorizationError",
    "build_user_content",
    "checked_content",
    "content_blocks",
    "data_url_block",
    "hydrate_image_messages",
    "limit_image_messages",
    "mcp_content_blocks",
]
