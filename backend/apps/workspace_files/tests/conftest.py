"""Fixtures: a workspace owned by a user and a guest that runs locally.

:class:`LocalAccessor` runs the real guest commands (``sh``, coreutils,
``python3``, ``tar``, optionally ``ffmpeg``) on this machine with
``/workspace`` mapped to a temporary directory, so the tests exercise the
scripts, the service and the HTTP adapter together.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKey, APIKeyPermission
from apps.harness.access.base import ExecResult, StreamClosedError, WorkspaceByteStream
from apps.organizations.models import Membership, MembershipRole, Organization
from apps.runners.enums import RunnerStatus, WorkspaceStatus
from apps.runners.models import Runner, Workspace
from apps.workspace_files import services as file_services
from common.utils import generate_api_token, hash_token


class LocalStream(WorkspaceByteStream):
    """A byte stream over a local subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        super().__init__("ws", uuid.uuid4().hex)
        self.process = process
        self.closed = False

    async def receive(self) -> bytes:
        assert self.process.stdout is not None
        return await self.process.stdout.read(64 * 1024)

    async def send(self, data: bytes) -> None:
        assert self.process.stdin is not None
        if self.process.stdin.is_closing():
            raise StreamClosedError("stdin closed")
        try:
            self.process.stdin.write(data)
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise StreamClosedError("guest exited") from exc

    async def send_eof(self) -> None:
        assert self.process.stdin is not None
        if not self.process.stdin.is_closing():
            self.process.stdin.close()

    async def aclose(self) -> None:
        self.closed = True
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.process.wait(), 5)
        transport = getattr(self.process, "_transport", None)
        if transport is not None:
            transport.close()

    async def wait_closed(self) -> int | None:
        return await self.process.wait()


@dataclass
class LocalAccessor:
    """Runs guest commands locally with ``/workspace`` mapped to *root*."""

    root: Path
    hide_ffmpeg: bool = False
    streams: list[LocalStream] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)

    def _argv(self, command: list[str]) -> list[str]:
        argv = [part.replace("/workspace", str(self.root)) for part in command]
        if self.hide_ffmpeg:
            argv = [
                part.replace("command -v ffmpeg", "command -v ffmpeg-missing")
                for part in argv
            ]
        self.commands.append(command)
        return argv

    async def exec_wait(self, command, workdir="/workspace", env=None, timeout=None):
        result = await asyncio.to_thread(
            subprocess.run, self._argv(list(command)), capture_output=True, text=True
        )
        return ExecResult(result.returncode, result.stdout, result.stderr)

    async def open_process(
        self, command, workdir="/workspace", env=None, timeout=None, **_
    ):
        process = await asyncio.create_subprocess_exec(
            *self._argv(list(command)),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stream = LocalStream(process)
        self.streams.append(stream)
        return stream


@dataclass
class FileWorld:
    org: Organization
    owner: Any
    stranger: Any
    workspace: Workspace
    root: Path
    accessor: LocalAccessor
    service: file_services.WorkspaceFileService

    def client(self, user=None, permissions=None) -> Client:
        token = generate_api_token()
        APIKey.objects.create(
            user=user or self.owner,
            name="files test",
            key_hash=hash_token(token),
            key_prefix=token[:12],
            permissions=(
                [
                    APIKeyPermission.WORKSPACES_READ.value,
                    APIKeyPermission.WORKSPACES_UPDATE.value,
                ]
                if permissions is None
                else permissions
            ),
        )
        return Client(HTTP_X_API_KEY=token, HTTP_X_ORGANIZATION_ID=str(self.org.id))

    def links(self, *paths: str, client: Client | None = None) -> dict:
        response = (client or self.client()).post(
            f"/api/v1/workspaces/{self.workspace.id}/files/links/",
            data=json.dumps({"paths": list(paths)}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        return {entry["path"]: entry for entry in response.json()["files"]}


@pytest.fixture
def file_world(db, tmp_path, monkeypatch) -> FileWorld:
    user_model = get_user_model()
    org = Organization.objects.create(name="Files", slug=f"files-{uuid.uuid4().hex}")
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
        runner=runner, created_by=owner, name="files", status=WorkspaceStatus.RUNNING
    )
    root = tmp_path / "workspace"
    root.mkdir()
    accessor = LocalAccessor(root)

    async def factory(_workspace_id: str) -> LocalAccessor:
        return accessor

    service = file_services.WorkspaceFileService(accessor_factory=factory)
    monkeypatch.setattr(file_services, "_service", service)
    return FileWorld(org, owner, stranger, workspace, root, accessor, service)


async def asgi_call(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    chunk: int = 64 * 1024,
    disconnect_after: int | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """Run one request through the ASGI file transport."""
    from apps.workspace_files.http import workspace_files_app

    path, _, query = url.partition("?")
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
    }
    pieces = [body[i : i + chunk] for i in range(0, len(body), chunk)] or [b""]
    queue: asyncio.Queue[dict] = asyncio.Queue()
    for index, piece in enumerate(pieces):
        queue.put_nowait(
            {
                "type": "http.request",
                "body": piece,
                "more_body": index < len(pieces) - 1,
            }
        )
    status = 0
    response_headers: dict[str, str] = {}
    received = bytearray()
    disconnected = asyncio.Event()

    async def receive() -> dict:
        if not queue.empty():
            return queue.get_nowait()
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            response_headers.update(
                {k.decode(): v.decode() for k, v in message.get("headers", [])}
            )
        elif message["type"] == "http.response.body":
            received.extend(message.get("body", b""))
            if disconnect_after is not None and len(received) >= disconnect_after:
                disconnected.set()
            if not message.get("more_body"):
                disconnected.set()

    with contextlib.suppress(Exception):
        await workspace_files_app(scope, receive, send)
    return status, response_headers, bytes(received)


def call(*args: Any, **kwargs: Any) -> tuple[int, dict[str, str], bytes]:
    """Synchronous wrapper for tests that also use Django's test client."""
    return async_to_sync(asgi_call)(*args, **kwargs)
