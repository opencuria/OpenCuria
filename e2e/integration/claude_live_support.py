"""Private support code for the opt-in Claude Agent LIVE fixture.

This module deliberately lives outside ``apps.harness.tests``: the live process
uses the actual engine and bundled CLI, but never imports pytest modules.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web


class FakeProcessStream:
    """Adapt the isolated native CLI's byte streams to the runner transport."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        guest_root: Path,
        remote_config_dir: str,
        local_config_dir: str,
    ) -> None:
        self.process = process
        self.guest_workspace = str(guest_root / "workspace").encode()
        self.guest_config = local_config_dir.encode()
        self.remote_config = remote_config_dir.encode()
        self.stderr = b""
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        if self.process.stderr is not None:
            self.stderr = await self.process.stderr.read()

    async def receive(self) -> bytes:
        if self.process.stdout is None:
            return b""
        chunk = await self.process.stdout.read(64 * 1024)
        return chunk.replace(self.guest_workspace, b"/workspace").replace(
            self.guest_config, self.remote_config
        )

    async def send(self, data: bytes) -> None:
        if self.process.stdin is None:
            raise RuntimeError("Isolated Claude CLI stdin is unavailable")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def send_eof(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            await self.process.stdin.wait_closed()

    async def wait_closed(self) -> int | None:
        return await self.process.wait()

    async def aclose(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
            await self.process.wait()
        await self._stderr_task

    async def stop_process_group(self) -> None:
        """Kill the detached CLI launcher and every child in its process group."""
        if self.process.returncode is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await self.process.wait()
        await self._stderr_task


class LocalSseWorkspaceAccessor:
    """Minimal fixture-owned workspace boundary, with no general shell access."""

    def __init__(self, root: Path, base_env: dict[str, str]) -> None:
        self.workspace_id = str(uuid.uuid4())
        self.root = root
        self.base_env = dict(base_env)
        self.commands: list[list[str]] = []
        self.opened: list[dict[str, Any]] = []
        self.lease_epoch = str(uuid.uuid4())

    def guest_path(self, path: str) -> Path:
        workspace_root = self.root / "workspace"
        if path == "/workspace":
            return workspace_root
        if path.startswith("/workspace/"):
            return workspace_root / path.removeprefix("/workspace/")
        guest_root = str(self.root / "guest")
        if path == guest_root:
            return workspace_root
        if path.startswith(guest_root + "/"):
            return workspace_root / path.removeprefix(guest_root + "/")
        return Path(path)

    async def exec_wait(
        self, command: list[str], workdir: str = "/workspace", env=None, timeout=None
    ):
        """Refuse general commands; the live subclass handles one engine guard."""
        del workdir, env, timeout
        raise RuntimeError(f"Synthetic runner rejected unexpected exec: {command!r}")

    async def write_file(self, path: str, content: bytes, mode: int = 0o644) -> None:
        target = self.guest_path(path)
        if (
            target != self.root / "workspace"
            and self.root / "workspace" not in target.parents
        ):
            raise RuntimeError("Synthetic runner denied a write outside /workspace")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(bytes(content))
        target.chmod(mode)

    async def read_file(self, path: str, max_size: int | None = None):
        from apps.harness.access.base import FileContent

        target = self.guest_path(path)
        if (
            target != self.root / "workspace"
            and self.root / "workspace" not in target.parents
        ):
            raise RuntimeError("Synthetic runner denied a read outside /workspace")
        if not target.is_file():
            raise FileNotFoundError(path)
        content = target.read_bytes()
        if max_size is not None:
            content = content[:max_size]
        return FileContent(content=content, size=len(content))

    async def desktop_action(self, action: str, args=None, timeout=None):
        del args, timeout
        if action == "binding":
            return {"ok": True, "epoch": self.lease_epoch}
        if action == "reserve":
            return {"ok": True, "epoch": self.lease_epoch, "lease_state": "reserved"}
        if action in {"renew", "hold"}:
            return {
                "ok": True,
                "epoch": self.lease_epoch,
                "lease_state": "held" if action == "hold" else "reserved",
            }
        if action == "release":
            return {"ok": True, "epoch": self.lease_epoch, "lease_state": "released"}
        raise RuntimeError(f"Synthetic runner rejected desktop action {action!r}")


def _sse(event_type: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _message_start(message_id: str, model: str) -> bytes:
    return _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 8, "output_tokens": 0},
            },
        },
    )


def encode_tool_stream(
    name: str, call_id: str, arguments: dict[str, Any], model: str
) -> bytes:
    """Encode a complete Anthropic streaming tool-use message."""
    return b"".join(
        (
            _message_start(f"msg-{call_id}", model),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": {},
                    },
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(arguments, ensure_ascii=False),
                    },
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 2},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        )
    )


def encode_text_stream(message_id: str, text: str, model: str) -> bytes:
    """Encode a complete Anthropic streaming text response."""
    return b"".join(
        (
            _message_start(message_id, model),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        )
    )


def encode_tool_json(
    message_id: str, name: str, call_id: str, arguments: dict[str, Any], model: str
) -> web.Response:
    """Encode a normal JSON Anthropic Messages response for ``stream: false``."""
    return web.json_response(
        {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
            ],
            "stop_reason": "tool_use",
            "stop_sequence": None,
            "usage": {"input_tokens": 8, "output_tokens": 2},
        }
    )


def encode_text_json(message_id: str, text: str, model: str) -> web.Response:
    """Encode a normal JSON Anthropic text response for ``stream: false``."""
    return web.json_response(
        {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 8, "output_tokens": 3},
        }
    )


__all__ = [
    "FakeProcessStream",
    "LocalSseWorkspaceAccessor",
    "encode_text_json",
    "encode_text_stream",
    "encode_tool_json",
    "encode_tool_stream",
]
