"""Claude multimodal input, MCP media, and native patch projection tests."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import claude_agent_sdk
import pytest
from pydantic import BaseModel

from apps.harness.access.base import FileContent
from apps.harness.agents.definitions import get_agent
from apps.harness.engines.claude.context import build_user_content, data_url_block
from apps.harness.engines.claude.engine import ClaudeEngine
from apps.harness.engines.claude.events import StepState
from apps.harness.engines.claude.materializer import decode_pdf_attachment
from apps.harness.engines.claude.mcp_bridge import mcp_attachment_content
from apps.harness.engines.claude.message_projection import (
    MAX_NATIVE_FILE_SNAPSHOT_BYTES,
    NativeFileChangeTracker,
)
from apps.harness.providers.base import LLMMessage
from apps.harness.tools import default_tool_registry
from apps.harness.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from mcp.server.lowlevel.server import request_ctx


class MediaAccessor:
    """Workspace fake that keeps file I/O behind accessor methods."""

    workspace_id = str(uuid.uuid4())

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: dict[str, bytes] = {}
        self.reads: list[tuple[str, int | None]] = []
        self.writes: list[tuple[str, bytes, int]] = []

    def _path(self, path: str) -> Path:
        if path == "/workspace":
            return self.root
        if path.startswith("/workspace/"):
            return self.root / path.removeprefix("/workspace/")
        raise ValueError("path outside fixture workspace")

    async def read_file(self, path: str, max_size: int | None = None) -> FileContent:
        self.reads.append((path, max_size))
        if path not in self.files:
            raise FileNotFoundError(path)
        payload = self.files[path]
        full_size = len(payload)
        if max_size is not None:
            payload = payload[:max_size]
        return FileContent(
            content=payload,
            size=full_size,
            truncated=len(payload) < full_size,
            mime="application/octet-stream",
        )

    async def write_file(self, path: str, content: bytes, mode: int = 0o644) -> None:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.files[path] = bytes(content)
        self.writes.append((path, bytes(content), mode))


class FakeWriteArgs(BaseModel):
    """Simple MCP tool fixture arguments."""

    label: str


class FakeWriteTool(Tool):
    """MCP tool fixture returning a PDF attachment."""

    name = "mcp_demo_pdf"
    permission_key = "mcp_demo_pdf"
    description = "Return a PDF attachment."
    args_schema = FakeWriteArgs

    def __init__(self, attachment: dict[str, Any] | list[dict[str, Any]]) -> None:
        self.attachments = attachment if isinstance(attachment, list) else [attachment]

    def title(self, args: BaseModel) -> str:
        return "PDF fixture"

    async def execute(self, args: BaseModel | dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(output="PDF fixture returned", attachments=self.attachments)


def _png_data_url(payload: bytes = b"\x89PNG\r\n\x1a\nfixture") -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def test_data_url_block_maps_image_and_pdf_to_anthropic_shapes() -> None:
    image_payload = b"\x89PNG\r\n\x1a\nfixture"
    assert data_url_block(_png_data_url(image_payload)) == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(image_payload).decode("ascii"),
        },
    }
    pdf_payload = b"%PDF-1.4\nfixture"
    pdf = data_url_block(
        "data:application/pdf;base64," + base64.b64encode(pdf_payload).decode(),
        filename="report.pdf",
    )
    assert pdf["type"] == "document"
    assert pdf["source"]["media_type"] == "application/pdf"
    assert pdf["title"] == "report.pdf"


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/image.png",
        "data:image/svg+xml;base64,PHN2Zz4=",
        "data:image/png;base64,not-base64!",
    ],
)
def test_data_url_block_rejects_remote_unsupported_or_invalid_media(value: str) -> None:
    with pytest.raises(ValueError):
        data_url_block(value)


@pytest.mark.asyncio
async def test_workspace_markdown_image_is_hydrated_and_translated_for_claude(
    tmp_path: Path,
) -> None:
    accessor = MediaAccessor(tmp_path)
    payload = b"\x89PNG\r\n\x1a\nworkspace-image"
    accessor.files["/workspace/screenshot.png"] = payload
    content = await build_user_content(
        "Inspect ![screen](/workspace/screenshot.png) please.", accessor=accessor
    )
    assert isinstance(content, list)
    image = next(block for block in content if block["type"] == "image")
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == payload
    assert [block.get("text") for block in content if block["type"] == "text"] == [
        "Inspect ",
        " please.",
    ]
    assert accessor.reads[0][1] is not None
    assert all(block["type"] != "image_url" for block in content)


@pytest.mark.asyncio
async def test_history_image_parts_are_preserved_but_resume_omits_duplicate_history(
    tmp_path: Path,
) -> None:
    accessor = MediaAccessor(tmp_path)
    payload = b"\xff\xd8\xfffixture"
    encoded = base64.b64encode(payload).decode()
    history = [LLMMessage(role="user", content=[
        {"type": "text", "text": "Earlier"},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}},
    ])]
    content = await build_user_content("Current", accessor=accessor, history=history)
    assert isinstance(content, list)
    assert any(block.get("text") == "Earlier" for block in content)
    image = next(block for block in content if block["type"] == "image")
    assert image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"]) == payload
    assert await build_user_content(
        "Current", accessor=accessor, history=history, include_history=False
    ) == "Current"


@pytest.mark.asyncio
async def test_local_sse_real_cli_receives_workspace_image_without_provider_network(
    tmp_path: Path,
) -> None:
    from apps.harness.tests.test_claude_engine import _run_local_cli_fixture

    bundled_cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if claude_agent_sdk.__version__ != "0.2.164" or not bundled_cli.is_file():
        pytest.skip("Pinned SDK CLI is unavailable")
    payload = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAACXBIWXMAAAABAAAA"
        "AQBPJcTWAAAAKUlEQVR4nO3NsQkAAAzDsPz/c6FPJJvAs5VLpm3vAAAAAAAAAAAAoNgDp+T0"
        "Ltwd/VwAAAAASUVORK5CYII="
    )
    result, _events, _questions, requests, _permissions, _store, _accessor = (
        await _run_local_cli_fixture(
            tmp_path,
            tool_name="",
            tool_arguments={},
            tools=default_tool_registry(),
            prompt="Describe ![fixture](/workspace/fixture.png).",
            cwd="/workspace",
            fixture_files={"fixture.png": payload},
        )
    )
    assert result.output == "fixture answer"
    messages = [message for request in requests for message in request.get("messages", [])]
    image_blocks = [
        block for message in messages for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "image"
    ]
    assert image_blocks, json.dumps(requests)
    image = image_blocks[0]
    assert image["source"]["media_type"] == "image/png"
    assert base64.b64decode(image["source"]["data"]) == payload
    assert all("image_url" not in json.dumps(message) for message in messages)


@pytest.mark.asyncio
async def test_local_sse_real_cli_receives_history_image_blocks(tmp_path: Path) -> None:
    from apps.harness.tests.test_claude_engine import _run_local_cli_fixture

    bundled_cli = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if claude_agent_sdk.__version__ != "0.2.164" or not bundled_cli.is_file():
        pytest.skip("Pinned SDK CLI is unavailable")
    payload = base64.b64decode(
        "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYwLjMxLjEwMgD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABNAAEBAAAAAAAAAAAAAAAAAAAABgEBAQEAAAAAAAAAAAAAAAAAAAYHEAEAAAAAAAAAAAAAAAAAAAAAEQEAAAAAAAAAAAAAAAAAAAAA/8AAEQgAIAAgAwEiAAIRAAMRAP/aAAwDAQACEQMRAD8AiwEo38AAAAAB/9k="
    )
    url = "data:image/jpeg;base64," + base64.b64encode(payload).decode()
    result, _events, _questions, requests, _permissions, _store, _accessor = (
        await _run_local_cli_fixture(
            tmp_path,
            tool_name="",
            tool_arguments={},
            tools=default_tool_registry(),
            prompt="Continue from the prior image.",
            cwd="/workspace",
            history=[LLMMessage(role="user", content=[
                {"type": "text", "text": "Earlier image"},
                {"type": "image_url", "image_url": {"url": url}},
            ])],
        )
    )
    assert result.output == "fixture answer"
    messages = [message for request in requests for message in request.get("messages", [])]
    image_blocks = [
        block for message in messages for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "image"
    ]
    assert image_blocks, json.dumps(requests)
    image = image_blocks[0]
    assert image["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(image["source"]["data"]) == payload
    assert any("Earlier image" in json.dumps(message) for message in messages)


@pytest.mark.asyncio
async def test_native_write_diff_is_bounded_and_emitted_only_for_successful_change(
    tmp_path: Path,
) -> None:
    accessor = MediaAccessor(tmp_path)
    path = "/workspace/src/file.py"
    accessor.files[path] = b"old = 1\n"
    engine = ClaudeEngine(tools=ToolRegistry(), accessor=accessor)
    events: list[dict[str, Any]] = []

    async def emit(event: dict[str, Any]) -> None:
        events.append(event)

    engine._emit = emit
    engine._step_states = {"": StepState()}
    await engine._native_pre_tool("native-write", "Write", arguments={"file_path": path})
    engine._tool_events["native-write"] = {
        "step": 3, "tool": "write", "source_tool": "Write", "title": "Write file",
        "event_arguments": {"path": path}, "parent_tool_use_id": None,
    }
    engine._tool_arguments["native-write"] = {"file_path": path}
    await engine._start_tool("native-write", parent_id=None)
    accessor.files[path] = b"new = 2\n"
    await engine._finish_tool_completed("native-write", "Wrote file", parent_id=None)
    patch = next(event for event in events if event.get("type") == "patch")
    assert patch["call_id"] == "native-write" and patch["step"] == 3
    assert patch["path"] == path
    assert "-old = 1" in patch["unified_diff"]
    assert "+new = 2" in patch["unified_diff"]

    await engine._native_pre_tool("native-noop", "Edit", arguments={"file_path": path})
    engine._tool_events["native-noop"] = {
        "step": 4, "tool": "edit", "source_tool": "Edit", "title": "Edit file",
        "event_arguments": {"path": path}, "parent_tool_use_id": None,
    }
    engine._tool_arguments["native-noop"] = {"file_path": path}
    await engine._start_tool("native-noop", parent_id=None)
    patch_count = sum(event.get("type") == "patch" for event in events)
    await engine._finish_tool_completed("native-noop", "No change", parent_id=None)
    assert sum(event.get("type") == "patch" for event in events) == patch_count


@pytest.mark.asyncio
async def test_native_diff_skips_large_binary_and_out_of_workspace_files(tmp_path: Path) -> None:
    tracker = NativeFileChangeTracker(MediaAccessor(tmp_path))
    accessor = tracker.accessor
    accessor.files["/workspace/large.txt"] = b"x" * (MAX_NATIVE_FILE_SNAPSHOT_BYTES + 1)
    assert await tracker.capture_before("Write", {"file_path": "/workspace/large.txt"}) is None
    accessor.files["/workspace/binary.bin"] = b"\x00\x01binary"
    assert await tracker.capture_before("Edit", {"file_path": "/workspace/binary.bin"}) is None
    assert await tracker.capture_before("Write", {"file_path": "/tmp/outside.txt"}) is None


@pytest.mark.asyncio
async def test_mcp_image_result_uses_sdk_image_block_and_pdf_stages_privately(
    tmp_path: Path,
) -> None:
    accessor = MediaAccessor(tmp_path)
    root = f"/workspace/.opencuria/harness/claude/{ClaudeEngine._bounded_session_dir('test')}"
    image_bytes, pdf_bytes = b"\x89PNG\r\n\x1a\nimage", b"%PDF-1.4\nfixture"
    attachments = [
        {"mime": "image/png", "url": _png_data_url(image_bytes), "filename": "screen.png"},
        {"mime": "application/pdf", "url": "data:application/pdf;base64," + base64.b64encode(pdf_bytes).decode(), "filename": "../../private.pdf"},
    ]

    async def ensure(path: str) -> None:
        accessor._path(path).mkdir(parents=True, exist_ok=True)

    content = await mcp_attachment_content(
        attachments, accessor=accessor, managed_root=root, ensure_directory=ensure
    )
    image = next(block for block in content if block.get("type") == "image")
    assert image["mimeType"] == "image/png" and base64.b64decode(image["data"]) == image_bytes
    notice = next(block["text"] for block in content if block.get("type") == "text")
    staged_path = notice.split(" at ", 1)[1].split(". Use", 1)[0]
    assert staged_path.startswith(root + "/config/attachments/") and staged_path.endswith(".pdf")
    assert "private.pdf" not in staged_path and accessor.files[staged_path] == pdf_bytes
    assert next(row for row in accessor.writes if row[0] == staged_path)[2] == 0o600

    valid_image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAACXBIWXMAAAABAAAA"
        "AQBPJcTWAAAAKUlEQVR4nO3NsQkAAAzDsPz/c6FPJJvAs5VLpm3vAAAAAAAAAAAAoNgDp+T0"
        "Ltwd/VwAAAAASUVORK5CYII="
    )
    live_attachments = [
        {"mime": "image/png", "url": _png_data_url(valid_image), "filename": "screen.png"},
        attachments[1],
    ]
    registry = ToolRegistry()
    pdf_tool = FakeWriteTool(live_attachments)
    registry.register(pdf_tool)
    engine = ClaudeEngine(tools=registry, accessor=accessor)
    engine._managed_root = root
    engine._options = SimpleNamespace(
        session_id="test",
        workspace_id=accessor.workspace_id,
        depth=0,
        max_depth=2,
        on_question=None,
        question_timeout=None,
        run_subagent=None,
    )
    engine._agent = get_agent("build")
    engine._model, engine._cwd = "sonnet", "/workspace"
    engine._tool_arguments["approved-pdf-call"] = {"label": "pdf"}
    engine._tool_events["approved-pdf-call"] = {
        "permission_decision": "allow", "agent_name": "build", "depth": 0,
        "started": True, "tool": pdf_tool.name,
        "source_tool": f"mcp__opencuria__{pdf_tool.name}",
        "title": "PDF fixture", "event_arguments": {"label": "pdf"},
        "step": 1, "parent_tool_use_id": None,
    }

    class _Bridge:
        def __init__(self) -> None:
            self.calls = {("approved-pdf-call", pdf_tool.name, '{"label": "pdf"}')}

        async def claim_mcp_call(self, call_id: str, name: str, args: dict[str, Any]) -> bool:
            return (call_id, name, json.dumps(args, sort_keys=True)) in self.calls

        async def take_mcp_call(self, *_args: Any, **_kwargs: Any) -> str | None:
            return None

    engine._bridge = _Bridge()
    engine._wire_to_registry = {pdf_tool.name: pdf_tool.name}
    handler = engine._mcp_handler(pdf_tool.name, pdf_tool.name)

    class _ReadyAccessor(MediaAccessor):
        async def exec_wait(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        async def write_file(self, path: str, content: bytes, mode: int = 0o644) -> None:
            self.files[path] = bytes(content)
            self.writes.append((path, bytes(content), mode))

    engine.accessor = _ReadyAccessor(tmp_path)
    engine.accessor.files.update(accessor.files)
    engine._ensure_managed_dir = lambda _path: asyncio.sleep(0)
    engine._native_file_change_tracker = NativeFileChangeTracker(engine.accessor)

    class _Context:
        class _Meta:
            model_extra = {"claudecode/toolUseId": "approved-pdf-call"}
        meta = _Meta()

    request = SimpleNamespace(meta=_Context.meta)
    token = request_ctx.set(SimpleNamespace(meta=request.meta))
    try:
        response = await handler({"label": "pdf"})
    finally:
        request_ctx.reset(token)
    assert response["is_error"] is False
    assert {item.get("type") for item in response["content"]} == {"text", "image"}
    assert any(item.get("data") == base64.b64encode(valid_image).decode() for item in response["content"])


def test_pdf_attachment_validation_rejects_bad_and_unsupported_payloads() -> None:
    payload = b"%PDF-1.7\nfixture"
    attachment = {"mime": "application/pdf", "url": "data:application/pdf;base64," + base64.b64encode(payload).decode()}
    assert decode_pdf_attachment(attachment) == payload
    assert decode_pdf_attachment({**attachment, "url": "https://example.test/a.pdf"}) is None
    assert decode_pdf_attachment({**attachment, "url": "data:application/pdf;base64,%%%"}) is None
    assert decode_pdf_attachment({**attachment, "mime": "text/plain"}) is None
