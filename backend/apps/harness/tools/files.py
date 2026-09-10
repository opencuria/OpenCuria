"""Standard harness tools: read, write, edit."""

from __future__ import annotations

import base64
import difflib

from pydantic import BaseModel, Field

from ..access.base import guess_mime_type, sanitize_harness_path
from ..access.runner_accessor import RunnerAccessorError
from .base import Tool, ToolContext, ToolError, ToolResult
from .file_locks import get_lock
from .truncate import MAX_BYTES, MAX_LINES

# Output page cap sent to the model (OpenCode ``MAX_BYTES``).
READ_MAX_BYTES = MAX_BYTES

# Bytes fetched from the runner so later pages can still be sliced.
READ_FETCH_MAX_BYTES = 5 * 1024 * 1024

# Default page size, mirroring OpenCode ``DEFAULT_READ_LIMIT``.
READ_DEFAULT_LIMIT = MAX_LINES

# OpenCode ``MAX_LINE_LENGTH`` — minify/lockfile lines stay bounded.
READ_MAX_LINE_LENGTH = 2000
_READ_LINE_SUFFIX = f"... (line truncated to {READ_MAX_LINE_LENGTH} chars)"
_READ_FOOTER_RESERVE = 256

# Binary detection: NUL byte in the probed prefix means "not text".
_BINARY_PROBE_BYTES = 4096

# OpenCode parity: media sniffing + ingestion cap.
SUPPORTED_IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
}
SUPPORTED_PDF_MIME = "application/pdf"
MAX_MEDIA_INGEST_BYTES = 20 * 1024 * 1024

# Bytes probed for magic-byte sniffing (OpenCode ``SAMPLE_BYTES``).
_MEDIA_SNIFF_BYTES = 4096


def _is_binary(content: bytes) -> bool:
    """Return True when *content* looks like a binary file."""
    return b"\x00" in content[:_BINARY_PROBE_BYTES]


def _starts_with(content: bytes, prefix: tuple[int, ...]) -> bool:
    """Return True when *content* starts with the given byte prefix."""
    if len(content) < len(prefix):
        return False
    return all(byte == value for byte, value in zip(content, prefix))


def _sniff_image_mime(sample: bytes) -> str | None:
    """Detect an image MIME from magic bytes (OpenCode ``imageMime``).

    PNG/JPEG/GIF/WEBP are recognized from the first bytes, independent
    of the file extension. Returns None when no magic matches.
    """
    if _starts_with(sample, (0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)):
        return "image/png"
    if _starts_with(sample, (0xFF, 0xD8, 0xFF)):
        return "image/jpeg"
    if _starts_with(sample, (0x47, 0x49, 0x46, 0x38)):
        return "image/gif"
    if _starts_with(sample, (0x52, 0x49, 0x46, 0x46)) and _starts_with(
        sample[8:], (0x57, 0x45, 0x42, 0x50)
    ):
        return "image/webp"
    return None


def sniff_attachment_mime(sample: bytes, path: str, stored_mime: str = "") -> str:
    """Return the attachment MIME for *sample* (OpenCode parity).

    Magic bytes win over the stored/extension MIME so a JPEG saved as
    ``.bin`` is still detected as ``image/jpeg``. Falls back to
    *stored_mime* (runner ``file --mime-type``) and finally to the
    extension guess.
    """
    sniffed = _sniff_image_mime(sample)
    if sniffed is not None:
        return sniffed
    if _starts_with(sample, (0x25, 0x50, 0x44, 0x46)):
        return SUPPORTED_PDF_MIME
    if stored_mime and stored_mime != "application/octet-stream":
        return stored_mime
    return guess_mime_type(path)


def is_pdf_attachment(mime: str) -> bool:
    """Return True when *mime* denotes a PDF attachment."""
    return mime == SUPPORTED_PDF_MIME


def _media_attachment(
    mime: str, content: bytes, *, filename: str = ""
) -> dict[str, object]:
    """Build an OpenCode-style file attachment dict.

    ``filename`` is the workspace basename (e.g. ``cat.png``) so
    providers and compaction placeholders can name the file without
    parsing the data URL. Fallback is ``""`` when unknown.
    """
    encoded = base64.b64encode(content).decode("ascii")
    return {
        "type": "file",
        "mime": mime,
        "url": f"data:{mime};base64,{encoded}",
        "filename": filename,
    }


def _clip_line(line: str) -> str:
    """Truncate one line to OpenCode's per-line character cap."""
    if len(line) <= READ_MAX_LINE_LENGTH:
        return line
    return f"{line[:READ_MAX_LINE_LENGTH]}{_READ_LINE_SUFFIX}"


def paginate_read(
    lines: list[str],
    *,
    offset: int,
    limit: int,
    max_bytes: int = READ_MAX_BYTES,
) -> tuple[list[str], bool, bool]:
    """Return a page of *lines* capped by *limit* and *max_bytes*.

    The third flag is True when the byte budget stopped the page early
    (OpenCode ``cut``), as opposed to only the line limit.
    """
    if offset < 0:
        offset = 0
    budget = max(1, max_bytes - _READ_FOOTER_RESERVE)
    raw: list[str] = []
    bytes_used = 0
    cut = False
    end = min(len(lines), offset + limit)
    for index in range(offset, end):
        line = _clip_line(lines[index])
        size = len(line.encode("utf-8")) + (1 if raw else 0)
        if bytes_used + size > budget:
            cut = True
            break
        raw.append(line)
        bytes_used += size
    more = cut or (offset + len(raw) < len(lines))
    return raw, more, cut


async def _miss_message(safe_path: str, exc: Exception, ctx: ToolContext) -> str:
    """Build a miss error with up to 3 same-directory suggestions."""
    base = str(exc)
    text = base.lower()
    if "not found" not in text and "no such file" not in text:
        return base
    suggestions = await _sibling_suggestions_async(safe_path, ctx)
    if not suggestions:
        return base
    listed = "\n".join(f"- {name}" for name in suggestions[:3])
    return f"{base}\nDid you mean one of these?\n{listed}"


async def _sibling_suggestions_async(safe_path: str, ctx: ToolContext) -> list[str]:
    """Awaitable sibling lookup (best effort, swallows errors)."""
    accessor = getattr(ctx, "accessor", None)
    list_dir = getattr(accessor, "list_dir", None)
    if list_dir is None:
        return []
    dirname = safe_path.rsplit("/", 1)[0] or "/workspace"
    wanted = safe_path.rsplit("/", 1)[-1].lower()
    try:
        entries = await list_dir(dirname)
    except Exception:
        return []
    names: list[str] = []
    for entry in entries or []:
        name = getattr(entry, "name", "") or ""
        if not name:
            continue
        names.append(name)
    if not names:
        return []
    ranked = sorted(
        names,
        key=lambda name: (
            0 if wanted in name.lower() or name.lower() in wanted else 1,
            name.lower(),
        ),
    )
    return ranked[:3]


def _patch_metadata(
    path: str,
    *,
    old_text: str,
    new_text: str,
) -> dict[str, str]:
    """Build unified-diff patch metadata for write/edit tools."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{path.lstrip('/')}",
        tofile=f"b/{path.lstrip('/')}",
        lineterm="",
    )
    unified = "\n".join(diff_lines)
    if not unified and old_text != new_text:
        unified = f"--- a/{path}\n+++ b/{path}\n(content changed)"
    return {
        "path": path,
        "old_content": old_text,
        "new_content": new_text,
        "unified_diff": unified,
    }


class ReadArgs(BaseModel):
    """Arguments for the read tool.

    Phase 3 decision: ``offset`` stays 0-based (backwards compatible);
    displayed line numbers are 0-based too (``f"{lineno}: {line}"``
    with ``lineno = offset + index``).
    """

    path: str = Field(description="Workspace-relative or /workspace path.")
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "First line to return (0-based, displayed line numbers "
            "are 0-based). Use to continue a large file."
        ),
    )
    limit: int = Field(
        default=READ_DEFAULT_LIMIT,
        gt=0,
        description="Max lines to return (defaults to 2000).",
    )


class WriteArgs(BaseModel):
    """Arguments for the write tool."""

    path: str = Field(description="Workspace-relative or /workspace path.")
    content: str = Field(description="Full file content (replaces file).")


class EditArgs(BaseModel):
    """Arguments for the edit tool."""

    path: str = Field(description="Workspace-relative or /workspace path.")
    old_string: str = Field(description="Exact string to replace.")
    new_string: str = Field(default="", description="Replacement string.")
    replace_all: bool = Field(default=False, description="Replace every occurrence.")


class ReadTool(Tool):
    """Read a text file with an optional line range.

    Phase 3 decision: 0-based ``offset`` is kept (backwards
    compatible); output lines carry a ``"{lineno}: "`` prefix with
    0-based numbers so the pagination footer (``lines X-Y``) stays
    consistent. On a miss, up to 3 same-directory basenames are
    suggested (OpenCode parity, best effort).
    """

    name = "read"
    description = (
        "Read a text file from the workspace. Paged via offset/limit "
        "(defaults: offset 0, limit 2000 lines, at most 50 KB per page). "
        "Offset and displayed line numbers are 0-based. "
        "Use offset to continue in large files. Rejects binary files. "
        "Lines longer than 2000 characters are truncated. "
        "This tool can read image files and PDFs and return them "
        "as file attachments."
    )
    args_schema: type[BaseModel] = ReadArgs
    permission_key = "read"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a read invocation."""
        assert isinstance(args, ReadArgs)
        return f"Read {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Read a file via the workspace accessor, paginated like OpenCode."""
        validated = self.coerce_args(args)
        assert isinstance(validated, ReadArgs)
        args = validated
        safe_path = sanitize_harness_path(args.path)
        try:
            stored = await ctx.accessor.read_file(
                safe_path, max_size=READ_FETCH_MAX_BYTES
            )
        except RunnerAccessorError as exc:
            raise ToolError(
                await _miss_message(safe_path, exc, ctx), tool=self.name
            ) from exc
        content = stored.content
        mime = sniff_attachment_mime(
            content[:_MEDIA_SNIFF_BYTES],
            safe_path,
            getattr(stored, "mime", ""),
        )
        if mime in SUPPORTED_IMAGE_MIMES or is_pdf_attachment(mime):
            return await self._execute_media(args.path, safe_path, mime, ctx)
        if _is_binary(content):
            raise ToolError(
                f"Refusing to read binary file: {args.path}",
                tool=self.name,
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(
                f"File is not valid UTF-8: {args.path}",
                tool=self.name,
            ) from exc
        lines = text.splitlines()
        if args.offset > 0 and args.offset >= len(lines):
            raise ToolError(
                f"Offset {args.offset} is out of range for this file "
                f"({len(lines)} lines): {args.path}",
                tool=self.name,
            )
        page, more, cut = paginate_read(lines, offset=args.offset, limit=args.limit)
        numbered = [f"{args.offset + index}: {line}" for index, line in enumerate(page)]
        output = "\n".join(numbered)
        last = args.offset + len(page)
        next_offset = last
        if cut:
            output += (
                f"\n\n(Output capped at {READ_MAX_BYTES // 1024} KB. "
                f"Showing lines {args.offset}-{last - 1}. "
                f"Use offset={next_offset} to continue.)"
            )
        elif more:
            output += (
                f"\n\n(Showing lines {args.offset}-{last - 1} of "
                f"{len(lines)}. Use offset={next_offset} to continue.)"
            )
        elif stored.truncated:
            output += (
                f"\n\n(Fetched the first {len(content)} bytes; the file "
                f"continues on disk. Use Grep or raise offset to inspect "
                "later sections.)"
            )
        return ToolResult(
            output=output,
            truncated=more or cut or stored.truncated,
            metadata={
                "path": safe_path,
                "lines": len(lines),
                "size": stored.size or len(content),
                "offset": args.offset,
                "next_offset": next_offset if (more or cut) else None,
            },
        )

    async def _execute_media(
        self, display_path: str, safe_path: str, mime: str, ctx: ToolContext
    ) -> ToolResult:
        """Return an image/PDF file as a file attachment (OpenCode parity).

        Re-reads up to ``MAX_MEDIA_INGEST_BYTES`` so media larger than
        the text fetch budget is still fully attached. Oversized media
        raises ``ToolError`` with an ingestion-limit message.
        """
        try:
            stored = await ctx.accessor.read_file(
                safe_path, max_size=MAX_MEDIA_INGEST_BYTES + 1
            )
        except RunnerAccessorError as exc:
            raise ToolError(
                await _miss_message(safe_path, exc, ctx), tool=self.name
            ) from exc
        content = stored.content
        on_disk = stored.size or len(content)
        if on_disk > MAX_MEDIA_INGEST_BYTES or (len(content) > MAX_MEDIA_INGEST_BYTES):
            raise ToolError(
                f"Media exceeds {MAX_MEDIA_INGEST_BYTES} byte ingestion "
                f"limit: {display_path}",
                tool=self.name,
            )
        output = (
            "PDF read successfully"
            if is_pdf_attachment(mime)
            else "Image read successfully"
        )
        filename = safe_path.rsplit("/", 1)[-1] or ""
        attachment = _media_attachment(mime, content, filename=filename)
        metadata = {
            "path": safe_path,
            "mime": mime,
            "size": on_disk,
            "attachments": [attachment],
        }
        return ToolResult(
            output=output,
            truncated=False,
            metadata=metadata,
            attachments=[attachment],
        )


class WriteTool(Tool):
    """Create or overwrite a file with the given content."""

    name = "write"
    description = (
        "Create or overwrite a file with the given content. Parent "
        "directories are created implicitly by the runner."
    )
    args_schema: type[BaseModel] = WriteArgs
    permission_key = "edit"

    def title(self, args: BaseModel) -> str:
        """Return a short title for a write invocation."""
        assert isinstance(args, WriteArgs)
        return f"Write {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Write file content via the workspace accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, WriteArgs)
        args = validated
        safe_path = sanitize_harness_path(args.path)
        async with get_lock(safe_path):
            old_text = ""
            try:
                stored = await ctx.accessor.read_file(safe_path)
                if not _is_binary(stored.content):
                    old_text = stored.content.decode("utf-8", errors="replace")
            except RunnerAccessorError:
                old_text = ""
            try:
                await ctx.accessor.write_file(safe_path, args.content.encode("utf-8"))
            except RunnerAccessorError as exc:
                raise ToolError(str(exc), tool=self.name) from exc
        patch = _patch_metadata(safe_path, old_text=old_text, new_text=args.content)
        return ToolResult(
            output=f"Wrote {len(args.content.encode('utf-8'))} bytes to {safe_path}",
            metadata=patch,
        )


class EditTool(Tool):
    """Exact string replacement inside a text file."""

    name = "edit"
    description = (
        "Replace an exact string in a text file. Fails unless the target "
        "occurs exactly once, unless replace_all is true."
    )
    args_schema: type[BaseModel] = EditArgs
    permission_key = "edit"

    def title(self, args: BaseModel) -> str:
        """Return a short title for an edit invocation."""
        assert isinstance(args, EditArgs)
        return f"Edit {args.path}"

    async def execute(
        self, args: BaseModel | dict[str, object], ctx: ToolContext
    ) -> ToolResult:
        """Apply an exact string replacement via the accessor."""
        validated = self.coerce_args(args)
        assert isinstance(validated, EditArgs)
        args = validated
        if not args.old_string:
            raise ToolError("old_string must not be empty", tool=self.name)
        safe_path = sanitize_harness_path(args.path)
        async with get_lock(safe_path):
            try:
                stored = await ctx.accessor.read_file(safe_path)
            except RunnerAccessorError as exc:
                raise ToolError(str(exc), tool=self.name) from exc
            if _is_binary(stored.content):
                raise ToolError(
                    f"Refusing to edit binary file: {args.path}",
                    tool=self.name,
                )
            try:
                text = stored.content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolError(
                    f"File is not valid UTF-8: {args.path}",
                    tool=self.name,
                ) from exc
            occurrences = text.count(args.old_string)
            if occurrences == 0:
                raise ToolError(
                    f"old_string not found in {args.path}",
                    tool=self.name,
                )
            if occurrences > 1 and not args.replace_all:
                raise ToolError(
                    f"old_string occurs {occurrences}x in {args.path}; "
                    "set replace_all=true to replace all",
                    tool=self.name,
                )
            if args.replace_all:
                updated = text.replace(args.old_string, args.new_string)
            else:
                updated = text.replace(args.old_string, args.new_string, 1)
            try:
                await ctx.accessor.write_file(safe_path, updated.encode("utf-8"))
            except RunnerAccessorError as exc:
                raise ToolError(str(exc), tool=self.name) from exc
            patch = _patch_metadata(safe_path, old_text=text, new_text=updated)
            patch["replacements"] = occurrences if args.replace_all else 1
            return ToolResult(
                output=f"Replaced {occurrences if args.replace_all else 1} "
                f"occurrence(s) in {safe_path}",
                metadata=patch,
            )
