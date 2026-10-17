"""Run-scoped Socket.IO runner stand-in for the mobile E2E ASGI fixture.

This is not the production runner: it authenticates with the private fixture
runner token and handles only bounded file RPCs below seeded workspace roots.
Run this as a separate local process after the ASGI fixture. The isolated
Socket.IO manager relays backend-to-runner events without Redis.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import os
import re
import signal
import uuid
from pathlib import Path
from typing import Any

import socketio

CHUNK_B64_SIZE = 256 * 1024
SAFE_ROOT = Path("/tmp/opencuria-mobile-e2e").resolve()


def _run_state(run_dir_value: str) -> tuple[Path, dict[str, Any], str]:
    """Load only the explicitly selected private run manifest and token."""
    run_dir = Path(run_dir_value).resolve()
    if not run_dir.is_relative_to(SAFE_ROOT) or run_dir == SAFE_ROOT:
        raise RuntimeError("Runner fixture directory must be under its safe tmp root")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    secret_file = run_dir / "runner-secret.json"
    if secret_file.stat().st_mode & 0o077:
        raise RuntimeError("Runner fixture token file is not private")
    token = str(json.loads(secret_file.read_text())["runner_token"])
    return run_dir, manifest, token


def _workspace_root(manifest: dict[str, Any], workspace_id: str) -> Path:
    """Resolve exactly one workspace recorded in this run's manifest."""
    workspace_uuid = str(uuid.UUID(workspace_id))
    if workspace_uuid not in manifest["workspace_ids"]:
        raise ValueError("Workspace does not belong to this fixture runner")
    expected = Path(manifest["workspace_root"]).resolve()
    root = (expected / workspace_uuid).resolve()
    if not root.is_relative_to(expected):
        raise ValueError("Fixture workspace escaped its temporary root")
    return root


def _workspace_path(manifest: dict[str, Any], workspace_id: str, path: str) -> Path:
    """Resolve /workspace paths while refusing symlink and parent traversal."""
    requested = Path(path or "/workspace")
    if not requested.is_absolute():
        requested = Path("/workspace") / requested
    if requested.parts[:2] != ("/", "workspace"):
        raise ValueError("File operation escaped /workspace")
    root = _workspace_root(manifest, workspace_id)
    target = root.joinpath(*requested.parts[2:]).resolve()
    if not target.is_relative_to(root):
        raise ValueError("File operation escaped its fixture workspace")
    return target


def _safe_name(name: str) -> str:
    """Restrict upload filenames to the real runner's safe component shape."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("Invalid file name")
    return cleaned


class MobileRunnerStandIn:
    """Minimal authenticated runner protocol backed by run-owned local files."""

    def __init__(self, base_url: str, token: str, manifest: dict[str, Any]) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.manifest = manifest
        self.client = socketio.AsyncClient(reconnection=False)
        self.uploads: dict[str, dict[str, Any]] = {}
        self.connected = asyncio.Event()
        self.shutdown = asyncio.Event()
        self._register_handlers()

    @staticmethod
    def _echo(data: dict[str, Any]) -> dict[str, str]:
        """Normalize correlation fields; paths are independently confined."""
        return {
            "workspace_id": str(data.get("workspace_id", "")),
            "request_id": str(data.get("request_id", "")),
            "path": str(data.get("path", "/workspace")),
        }

    def _register_handlers(self) -> None:
        @self.client.event
        async def connect() -> None:
            await self.client.emit(
                "runner:register", {"supported_runtimes": ["docker"]}
            )
            await self.client.emit(
                "runner:heartbeat",
                {"workspaces": [], "workspace_states_observed": False},
            )
            self.connected.set()

        @self.client.on("files:list")
        async def files_list(data: dict[str, Any]) -> None:
            result = self._echo(data)
            target: Path | None = None
            try:
                target = _workspace_path(
                    self.manifest, result["workspace_id"], result["path"]
                )
                workspace_root = _workspace_root(self.manifest, result["workspace_id"])
                entries = [
                    {
                        "name": item.name,
                        "path": "/workspace/" + str(item.relative_to(workspace_root)),
                        "type": "directory" if item.is_dir() else "file",
                        "size": item.stat().st_size if item.is_file() else 0,
                    }
                    for item in sorted(
                        target.iterdir(),
                        key=lambda value: (not value.is_dir(), value.name.lower()),
                    )
                ]
                result["entries"] = entries
            except (OSError, TypeError, ValueError) as exc:
                error = str(exc)
                if target is not None and not target.exists():
                    error = (
                        "Failed to list files: [Errno 2] No such file or directory: "
                        + result["path"]
                    )
                result.update(entries=[], error=error)
            await self.client.emit("files:list_result", result)

        @self.client.on("files:find")
        async def files_find(data: dict[str, Any]) -> None:
            result = self._echo(data)
            query = str(data.get("query", "")).strip().lower()
            paths: list[dict[str, str]] = []
            try:
                root = _workspace_path(
                    self.manifest, result["workspace_id"], "/workspace"
                )
                limit = min(max(int(data.get("limit", 50)), 1), 50)
                for item in root.rglob("*"):
                    if query and query not in item.name.lower():
                        continue
                    if item.is_symlink():
                        continue
                    paths.append(
                        {
                            "path": "/workspace/" + str(item.relative_to(root)),
                            "name": item.name,
                        }
                    )
                    if len(paths) >= limit:
                        break
                result.update(paths=paths, query=query, truncated=False)
            except (OSError, TypeError, ValueError) as exc:
                result.update(paths=[], query=query, truncated=False, error=str(exc))
            await self.client.emit("files:find_result", result)

        @self.client.on("files:read")
        async def files_read(data: dict[str, Any]) -> None:
            result = self._echo(data)
            try:
                target = _workspace_path(
                    self.manifest, result["workspace_id"], result["path"]
                )
                content = target.read_bytes()
                max_size = int(data.get("max_size") or 5 * 1024 * 1024)
                if not 0 < max_size <= 100 * 1024 * 1024:
                    raise ValueError("Invalid file read size")
                encoded = base64.b64encode(content[:max_size]).decode("ascii")
                result.update(
                    size=len(content),
                    truncated=len(content) > max_size,
                    mime_type=(
                        mimetypes.guess_type(str(target))[0]
                        or "application/octet-stream"
                    ),
                )
                if len(encoded) <= CHUNK_B64_SIZE:
                    await self.client.emit(
                        "files:content_result", {**result, "content": encoded}
                    )
                else:
                    chunks = [
                        encoded[offset : offset + CHUNK_B64_SIZE]
                        for offset in range(0, len(encoded), CHUNK_B64_SIZE)
                    ]
                    for index, piece in enumerate(chunks):
                        await self.client.emit(
                            "files:content_chunk",
                            {
                                **result,
                                "index": index,
                                "total_chunks": len(chunks),
                                "content": piece,
                            },
                        )
                    await self.client.emit(
                        "files:content_result",
                        {
                            **result,
                            "content": "",
                            "chunked": True,
                            "total_chunks": len(chunks),
                        },
                    )
            except (OSError, TypeError, ValueError) as exc:
                result.update(content="", size=0, truncated=False, error=str(exc))
                await self.client.emit("files:content_result", result)

        @self.client.on("files:download")
        async def files_download(data: dict[str, Any]) -> None:
            result = self._echo(data)
            try:
                target = _workspace_path(
                    self.manifest, result["workspace_id"], result["path"]
                )
                content = target.read_bytes()
                encoded = base64.b64encode(content).decode("ascii")
                result.update(
                    filename=target.name,
                    is_archive=False,
                    size=len(content),
                )
                if len(encoded) <= CHUNK_B64_SIZE:
                    await self.client.emit(
                        "files:download_result",
                        {**result, "content": encoded},
                    )
                else:
                    chunks = [
                        encoded[offset : offset + CHUNK_B64_SIZE]
                        for offset in range(0, len(encoded), CHUNK_B64_SIZE)
                    ]
                    for index, piece in enumerate(chunks):
                        await self.client.emit(
                            "files:download_chunk",
                            {
                                **result,
                                "index": index,
                                "total_chunks": len(chunks),
                                "content": piece,
                            },
                        )
                    await self.client.emit(
                        "files:download_result",
                        {
                            **result,
                            "content": "",
                            "chunked": True,
                            "total_chunks": len(chunks),
                        },
                    )
            except (OSError, TypeError, ValueError) as exc:
                result.update(
                    content="",
                    filename="",
                    is_archive=False,
                    size=0,
                    error=str(exc),
                )
                await self.client.emit("files:download_result", result)

        @self.client.on("files:upload")
        async def files_upload(data: dict[str, Any]) -> None:
            result = self._echo(data)
            request_id = result["request_id"]
            try:
                if data.get("chunked"):
                    count = int(data.get("total_chunks", 0))
                    if count < 1 or count > 64 or not request_id:
                        raise ValueError("Invalid chunked upload header")
                    self.uploads[request_id] = {
                        **result,
                        "filename": _safe_name(str(data.get("filename", ""))),
                        "total_chunks": count,
                        "chunks": {},
                    }
                    return
                raw = base64.b64decode(str(data["content"]), validate=True)
                self._write_upload(
                    result["workspace_id"],
                    result["path"],
                    str(data["filename"]),
                    raw,
                )
                result["status"] = "success"
            except (KeyError, OSError, TypeError, ValueError) as exc:
                result.update(status="error", error=str(exc))
            await self.client.emit("files:upload_result", result)

        @self.client.on("files:upload_chunk")
        async def files_upload_chunk(data: dict[str, Any]) -> None:
            request_id = str(data.get("request_id", ""))
            upload = self.uploads.get(request_id)
            try:
                if upload is None:
                    raise ValueError("Unknown upload transfer")
                if (
                    data.get("workspace_id") != upload["workspace_id"]
                    or data.get("path") != upload["path"]
                ):
                    raise ValueError("Upload identity mismatch")
                if int(data.get("total_chunks", 0)) != upload["total_chunks"]:
                    raise ValueError("Upload chunk count mismatch")
                index = int(data.get("index", -1))
                piece = "".join(str(data.get("content", "")).split())
                if (
                    not 0 <= index < upload["total_chunks"]
                    or not piece
                    or len(piece) > CHUNK_B64_SIZE
                ):
                    raise ValueError("Invalid upload chunk")
                if index in upload["chunks"]:
                    raise ValueError("Duplicate upload chunk")
                upload["chunks"][index] = piece
            except (TypeError, ValueError) as exc:
                self.uploads.pop(request_id, None)
                await self.client.emit(
                    "files:upload_result",
                    {**self._echo(data), "status": "error", "error": str(exc)},
                )

        @self.client.on("files:upload_finish")
        async def files_upload_finish(data: dict[str, Any]) -> None:
            request_id = str(data.get("request_id", ""))
            upload = self.uploads.pop(request_id, None)
            result = self._echo(data)
            try:
                if upload is None or upload["path"] != result["path"]:
                    raise ValueError("Unknown upload transfer")
                chunks = upload["chunks"]
                if len(chunks) != upload["total_chunks"]:
                    raise ValueError("Incomplete upload transfer")
                encoded = "".join(
                    chunks[index] for index in range(upload["total_chunks"])
                )
                raw = base64.b64decode(encoded, validate=True)
                self._write_upload(
                    upload["workspace_id"], upload["path"], upload["filename"], raw
                )
                result["status"] = "success"
            except (OSError, TypeError, ValueError) as exc:
                result.update(status="error", error=str(exc))
            await self.client.emit("files:upload_result", result)

    def _write_upload(
        self, workspace_id: str, directory: str, name: str, raw: bytes
    ) -> None:
        """Write one bounded file only under the selected fixture workspace."""
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError("Fixture upload exceeds 10 MiB")
        target_directory = _workspace_path(self.manifest, workspace_id, directory)
        target_directory.mkdir(parents=True, exist_ok=True)
        relative = directory.rstrip("/") + "/" + _safe_name(name)
        _workspace_path(self.manifest, workspace_id, relative).write_bytes(raw)

    async def run(self) -> None:
        """Stay connected until SIGINT/SIGTERM, then disconnect cleanly."""
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, self.shutdown.set)
        await self.client.connect(
            self.base_url,
            socketio_path="ws/runner",
            transports=["websocket"],
            auth={"token": self.token},
            wait_timeout=15,
        )
        await self.connected.wait()
        print(
            "Authenticated isolated Socket.IO runner for "
            f"{len(self.manifest['workspace_ids'])} fixture workspaces",
            flush=True,
        )
        await self.shutdown.wait()
        await self.client.disconnect()


async def _main() -> None:
    """Run only the fixture-owned authenticated Socket.IO stand-in."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", default=os.environ.get("MOBILE_E2E_RUN_DIR", ""))
    args = parser.parse_args()
    _, manifest, token = _run_state(args.run_dir)
    await MobileRunnerStandIn(manifest["socket_url"], token, manifest).run()


def main() -> None:
    """Start the fixture runner protocol, never the production runner daemon."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
