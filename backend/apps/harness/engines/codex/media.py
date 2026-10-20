"""Translate checked shared media; PDFs use isolated bounded text extraction."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path
from typing import Any

from ...access.base import WorkspaceAccessor
from ...runner import RunOptions
from ..shared.media import (
    MAX_CLAUDE_DOCUMENT_BYTES,
    MAX_USER_CONTENT_BYTES,
    build_user_content,
    content_blocks,
    data_url_block,
)

PDF_TIMEOUT = 10
MAX_PDF_TEXT_BYTES = 512 * 1024
_PDF_WORKER = Path(__file__).resolve().parents[1] / "shared" / "pdf_worker.py"


async def _pdf_text(block: dict[str, Any]) -> str:
    """Extract PDF text in a cancellable process with hard CPU/memory limits."""
    source = block["source"]
    data_url_block(f"data:{source['media_type']};base64,{source['data']}")
    payload = base64.b64decode(source["data"], validate=True)
    if len(payload) > MAX_CLAUDE_DOCUMENT_BYTES:
        raise ValueError("PDF exceeds the input limit")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(_PDF_WORKER),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"LANG": "C.UTF-8"},
    )
    try:
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(payload), PDF_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise ValueError("PDF extraction exceeded its time limit") from None
        if process.returncode:
            raise ValueError("PDF extraction exceeded resource limits or failed")
        if len(output) > MAX_PDF_TEXT_BYTES * 6 + 1024:
            raise ValueError("PDF extraction exceeded its output limit")
        record = json.loads(output)
        if "error" in record:
            raise ValueError(record["error"])
        text = record["text"]
        if not isinstance(text, str) or len(text.encode()) > MAX_PDF_TEXT_BYTES:
            raise ValueError("Extracted PDF exceeds the text limit")
        return "PDF extracted text only (images and layout are not preserved):\n" + text
    finally:
        # wait_for cancels communicate, not the child. Always kill and reap it,
        # including on caller cancellation; never leave a parser thread running.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass  # The process may exit between the status check and kill.
        reaped = asyncio.create_task(process.wait())
        cancelled = False
        while not reaped.done():
            try:
                await asyncio.shield(reaped)
            except asyncio.CancelledError:
                cancelled = True
        reaped.result()
        if cancelled:
            raise asyncio.CancelledError


async def convert(
    blocks: list[dict[str, Any]], *, dynamic: bool = False
) -> list[dict[str, Any]]:
    """Convert checked blocks to Codex input with aggregate request bounds."""
    output: list[dict[str, Any]] = []
    total = 0
    for block in blocks:
        kind = block["type"]
        if kind == "document":
            text = await _pdf_text(block)
            total += len(text.encode())
            output.append({"type": "inputText" if dynamic else "text", "text": text})
        elif kind == "text":
            total += len(block["text"].encode())
            output.append(
                {"type": "inputText" if dynamic else "text", "text": block["text"]}
            )
        elif kind == "image":
            source = block["source"]
            url = f"data:{source['media_type']};base64,{source['data']}"
            data_url_block(url)
            total += len(url.encode())
            output.append(
                {
                    "type": "inputImage" if dynamic else "image",
                    "imageUrl" if dynamic else "url": url,
                }
            )
        else:
            raise ValueError("Unsupported Codex content block")
        if total > MAX_USER_CONTENT_BYTES:
            raise ValueError("Codex request content exceeds the input limit")
    return output


async def user_input(
    prompt: str,
    *,
    accessor: WorkspaceAccessor,
    opts: RunOptions,
    include_history: bool,
) -> list[dict[str, Any]]:
    """Hydrate only authorized no-follow workspace media before conversion."""
    content = await build_user_content(
        prompt,
        accessor=accessor,
        history=opts.history,
        include_history=include_history,
        authorize_read=opts.authorize_file_read,
    )
    return await convert(
        content_blocks(content) if isinstance(content, str) else content
    )


async def dynamic_content(
    content: str | list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Convert inline tool results without fetching arbitrary file or HTTP URLs."""
    return await convert(content_blocks(content), dynamic=True)
