"""Fixtures for published apps: accounts, a workspace and fake Expo builds."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import tarfile
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import WSMsgType, web
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.access.base import ExecResult, WorkspaceByteStream
from apps.harness.access.file_types import FileContent
from apps.harness.access.runner_accessor import RunnerAccessorError
from apps.mobile_apps.export import ExportFiles
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from common.utils import generate_api_token, hash_token

PNG = b"\x89PNG\r\n\x1a\n" + b"icon-pixels"


def build_files(
    *,
    slug: str = "todo",
    version: str = "1.0.0",
    sdk: str = "57.0.0",
    bundle: bytes = b"bundle-android",
    ios_bundle: bytes | None = b"bundle-ios",
    assets: dict[str, bytes] | None = None,
    dependencies: dict[str, str] | None = None,
) -> tuple[ExportFiles, bytes]:
    """An ``expo export`` output (both platforms) and its package.json."""
    assets = {"logo.png": b"logo-png"} if assets is None else assets
    files: dict[str, bytes] = {"assets/icon.png": PNG}
    metadata: dict[str, Any] = {"version": 0, "bundler": "metro", "fileMetadata": {}}
    platforms = {"android": bundle, "ios": ios_bundle}
    for platform, content in platforms.items():
        if content is None:
            continue
        path = f"_expo/static/js/{platform}/index-{platform}.hbc"
        files[f"dist/{path}"] = content
        entries = []
        for name, data in assets.items():
            key = hashlib.md5(data, usedforsecurity=False).hexdigest()
            files[f"dist/assets/{key}"] = data
            entries.append({"path": f"assets/{key}", "ext": name.split(".")[-1]})
        metadata["fileMetadata"][platform] = {"bundle": path, "assets": entries}
    files["dist/metadata.json"] = json.dumps(metadata).encode()
    config = {
        "name": slug.title(),
        "slug": slug,
        "version": version,
        "sdkVersion": sdk,
        "icon": "./assets/icon.png",
        "orientation": "portrait",
    }
    package = json.dumps(
        {"dependencies": dependencies or {"expo": "~57.0.30", "react": "19.2.3"}}
    ).encode()
    return ExportFiles(config=config, output_dir="dist", files=files), package


def tarball(files: dict[str, bytes]) -> bytes:
    """Pack ``files`` like ``tar -czf`` in the workspace would."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class FakeReader:
    """Stands in for the workspace: returns a prepared build."""

    def __init__(self, files: ExportFiles, package: bytes | None) -> None:
        self.result = (files, package)
        self.calls: list[tuple[uuid.UUID, str, str]] = []

    async def read(self, workspace_id, project_path, output_dir="dist"):
        self.calls.append((workspace_id, project_path, output_dir))
        return self.result


class FakeAccessor:
    """Runner accessor double recording exec calls and serving one archive."""

    def __init__(
        self,
        *,
        config: dict[str, Any] | None = None,
        archive: bytes = b"",
        config_exit: int = 0,
        read_failures: int = 0,
    ) -> None:
        self.config = config or {}
        self.archive = archive
        self.config_exit = config_exit
        self.read_failures = read_failures
        self.commands: list[list[str]] = []
        self.read_paths: list[str] = []

    async def exec_wait(self, command, workdir="/workspace", env=None, timeout=None):
        self.commands.append(list(command))
        if command[:3] == ["npx", "--no-install", "expo"]:
            if self.config_exit:
                return ExecResult(exit_code=self.config_exit, stderr="no expo here")
            return ExecResult(
                exit_code=0, stdout=f"warning: noise\n{json.dumps(self.config)}\n"
            )
        return ExecResult(exit_code=0)

    async def read_file(self, path, max_size=None):
        self.read_paths.append(path)
        if self.read_failures:
            self.read_failures -= 1
            raise RunnerAccessorError("harness:read_file failed: incomplete transfer")
        return FileContent(
            content=self.archive, size=len(self.archive), truncated=False
        )


@dataclass
class AppWorld:
    org: Organization
    owner: Any
    stranger: Any
    workspace: Workspace

    def client(self, user=None, permissions=None) -> Client:
        token = generate_api_token()
        APIKey.objects.create(
            user=user or self.owner,
            name="apps test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=(
                [
                    APIKeyPermission.HARNESS_READ.value,
                    APIKeyPermission.HARNESS_RUN.value,
                ]
                if permissions is None
                else permissions
            ),
        )
        return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(self.org.id))


def build_world() -> AppWorld:
    user_model = get_user_model()
    org = Organization.objects.create(name="Apps", slug=f"apps-{uuid.uuid4().hex}")
    owner = user_model.objects.create_user(email=f"owner-{uuid.uuid4().hex}@x.test")
    stranger = user_model.objects.create_user(email=f"other-{uuid.uuid4().hex}@x.test")
    for user in (owner, stranger):
        Membership.objects.create(
            user=user, organization=org, role=MembershipRole.MEMBER
        )
    runner = Runner.objects.create(
        organization=org, api_token_hash=uuid.uuid4().hex, status=RunnerStatus.ONLINE
    )
    workspace = Workspace.objects.create(
        runner=runner, created_by=owner, name="app-dev", status=WorkspaceStatus.RUNNING
    )
    return AppWorld(org=org, owner=owner, stranger=stranger, workspace=workspace)


@pytest.fixture(autouse=True)
def media_root(settings, tmp_path):
    """Store blobs in a throwaway directory."""
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


@pytest.fixture
def app_world(db) -> AppWorld:
    return build_world()


class SocketStream(WorkspaceByteStream):
    """A workspace byte stream over a real local TCP connection."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        super().__init__("ws-1", uuid.uuid4().hex)
        self.reader, self.writer = reader, writer

    async def receive(self) -> bytes:
        return await self.reader.read(65536)

    async def send(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def send_eof(self) -> None:
        self.writer.write_eof()

    async def aclose(self) -> None:
        self.writer.close()

    async def wait_closed(self) -> int | None:
        return None


class Process:
    """What the workspace process saw."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.stream_closed = asyncio.Event()


@pytest.fixture
async def workspace_process():
    seen = Process()

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        seen.requests.append(
            {
                "method": request.method,
                "path_qs": request.path_qs,
                "headers": dict(request.headers),
                "body": body,
            }
        )
        return web.json_response({"ok": True}, headers={"X-Upstream": "yes"})

    async def events(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        try:
            for index in range(1000):
                await response.write(f"data: {index}\n\n".encode())
                await asyncio.sleep(0.02)
        except (ConnectionResetError, RuntimeError):
            seen.stream_closed.set()
        finally:
            seen.stream_closed.set()
        return response

    async def chunks(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        for part in (b"one,", b"two,", b"three"):
            await response.write(part)
        await response.write_eof()
        return response

    async def login(request: web.Request) -> web.Response:
        raise web.HTTPFound(
            "/home",
            headers={"Set-Cookie": "sid=42; Path=/; Domain=localhost; HttpOnly"},
        )

    async def socket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(protocols=("chat",))
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                await ws.send_str(f"echo:{message.data}")
            elif message.type == WSMsgType.BINARY:
                await ws.send_bytes(message.data[::-1])
        return ws

    app = web.Application()
    app.add_routes(
        [
            web.route("*", "/echo", echo),
            web.get("/events", events),
            web.get("/chunks", chunks),
            web.get("/login", login),
            web.get("/socket", socket),
        ]
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    seen.port = port
    yield seen
    await runner.cleanup()
