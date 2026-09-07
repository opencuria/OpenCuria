"""Shared fixtures for the harness app tests."""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest

from apps.harness.access.base import (
    DirEntry,
    ExecChunk,
    ExecResult,
    FileContent,
    FileStat,
    WorkspaceAccessor,
)
from apps.harness.access.runner_accessor import RunnerAccessorError
from apps.organizations.models import Organization

# Allow sync ORM calls from async test functions (Django safety check).
os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

TINY_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
)
DEFAULT_DESKTOP_WIDTH = 1920
DEFAULT_DESKTOP_HEIGHT = 1080


def desktop_screenshot_result(
    *,
    width: int = DEFAULT_DESKTOP_WIDTH,
    height: int = DEFAULT_DESKTOP_HEIGHT,
    jpeg: bytes = TINY_JPEG,
) -> dict[str, Any]:
    """Build a runner-style screenshot payload for FakeAccessor."""
    return {
        "ok": True,
        "image_b64": base64.b64encode(jpeg).decode("ascii"),
        "mime": "image/jpeg",
        "width": width,
        "height": height,
        "text": "",
    }


def desktop_display_info_result(
    *,
    width: int = DEFAULT_DESKTOP_WIDTH,
    height: int = DEFAULT_DESKTOP_HEIGHT,
) -> dict[str, Any]:
    """Build a runner-style display_info payload for FakeAccessor."""
    return {
        "ok": True,
        "display": ":1",
        "width": width,
        "height": height,
    }


@pytest.fixture
def organization(db) -> Organization:
    """Default organization for harness fixtures."""
    return Organization.objects.create(
        name=f"Harness Org {uuid.uuid4().hex[:6]}",
        slug=f"harness-org-{uuid.uuid4().hex[:10]}",
    )


class FakeAccessor(WorkspaceAccessor):
    """In-memory WorkspaceAccessor for tool tests (no network)."""

    def __init__(
        self,
        workspace_id: str = "ws-1",
        *,
        files: dict[str, bytes] | None = None,
        error: Exception | None = None,
        exec_result: ExecResult | None = None,
        desktop_result: dict[str, Any] | None = None,
        desktop_results: list[dict[str, Any]] | None = None,
        desktop_width: int = DEFAULT_DESKTOP_WIDTH,
        desktop_height: int = DEFAULT_DESKTOP_HEIGHT,
    ) -> None:
        super().__init__(workspace_id)
        self.files: dict[str, bytes] = dict(files or {})
        self.error = error
        self.exec_result = exec_result
        self.desktop_result = desktop_result
        self.desktop_results = list(desktop_results or [])
        self.desktop_width = desktop_width
        self.desktop_height = desktop_height
        self.written: dict[str, bytes] = {}
        self.exec_calls: list[tuple] = []
        self.desktop_calls: list[tuple] = []
        self.processes: dict[str, dict[str, Any]] = {}

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    def _desktop_payload(
        self, action: str, args: dict[str, Any] | None
    ) -> dict[str, Any]:
        if self.desktop_results:
            return dict(self.desktop_results.pop(0))
        if self.desktop_result is not None:
            return dict(self.desktop_result)
        if action == "display_info":
            return desktop_display_info_result(
                width=self.desktop_width,
                height=self.desktop_height,
            )
        if action == "ensure":
            return {"ok": True, "display": ":1", "port": 5900}
        if action == "hold":
            return {
                "ok": True,
                "display": ":1",
                "port": 5900,
                "viewer": False,
                "computer_use": True,
            }
        if action == "release":
            return {
                "ok": True,
                "stopped": True,
                "process_alive": False,
                "viewer_held": False,
                "computer_use_active": False,
            }
        if action == "record_start":
            run_id = str((args or {}).get("run_id") or "run")
            path = f"/workspace/.opencuria/computeruse/{run_id}/session.mp4"
            self.files[path] = b"fake-mp4"
            return {"ok": True, "path": path, "run_id": run_id}
        if action == "record_stop":
            run_id = str((args or {}).get("run_id") or "run")
            path = f"/workspace/.opencuria/computeruse/{run_id}/session.mp4"
            return {"ok": True, "path": path}
        if action == "screenshot":
            crop_w = (args or {}).get("crop_w")
            crop_h = (args or {}).get("crop_h")
            width = int(crop_w) if crop_w is not None else self.desktop_width
            height = int(crop_h) if crop_h is not None else self.desktop_height
            return desktop_screenshot_result(width=width, height=height)
        return {"ok": True}

    async def exec_stream(
        self,
        command,
        workdir="/workspace",
        env=None,
        timeout=None,
    ) -> AsyncIterator[ExecChunk]:
        """Yield a single done chunk (unused by current tools)."""
        self._maybe_fail()
        yield ExecChunk(stream="", exit_code=0, done=True)

    async def exec_wait(
        self,
        command,
        workdir="/workspace",
        env=None,
        timeout=None,
    ) -> ExecResult:
        """Return the canned result or run a tiny fake shell."""
        self._maybe_fail()
        self.exec_calls.append((command, workdir))
        if self.exec_result is not None:
            return self.exec_result
        text = command if isinstance(command, str) else " ".join(command)
        if "rg --files" in text or text.startswith("find "):
            paths = sorted(self.files)
            return ExecResult(exit_code=0, stdout="\n".join(paths))
        if "rg " in text or "grep " in text:
            return ExecResult(exit_code=0, stdout="")
        return ExecResult(exit_code=0, stdout="ok")

    async def read_file(self, path: str, max_size=None) -> FileContent:
        """Read from the in-memory file map."""
        self._maybe_fail()
        if path not in self.files:
            raise RunnerAccessorError(f"read_file failed: not found {path}")
        content = self.files[path]
        return FileContent(content=content, size=len(content))

    async def write_file(self, path: str, content: bytes, mode=0o644) -> None:
        """Write into the in-memory file map."""
        self._maybe_fail()
        self.files[path] = bytes(content)
        self.written[path] = bytes(content)

    async def list_dir(self, path: str) -> list[DirEntry]:
        """List files with the given prefix."""
        self._maybe_fail()
        prefix = path.rstrip("/") + "/"
        entries = []
        for full, content in sorted(self.files.items()):
            if full.startswith(prefix):
                rest = full[len(prefix) :]
                if "/" not in rest:
                    entries.append(
                        DirEntry(
                            name=rest,
                            path=full,
                            is_dir=False,
                            size=len(content),
                        )
                    )
        return entries

    async def stat(self, path: str) -> FileStat:
        """Stat the in-memory file map."""
        self._maybe_fail()
        if path in self.files:
            return FileStat(path=path, size=len(self.files[path]))
        raise RunnerAccessorError(f"stat failed: not found {path}")

    async def desktop_action(
        self,
        action: str,
        args: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Return the canned desktop action result."""
        self._maybe_fail()
        self.desktop_calls.append((action, args, timeout))
        return self._desktop_payload(action, args or {})

    async def process_start(
        self,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str = "",
    ) -> dict[str, Any]:
        """Return a canned started-process record."""
        self._maybe_fail()
        record = {
            "process_id": "proc-1",
            "workspace_id": self.workspace_id,
            "name": name or "",
            "command": command,
            "workdir": workdir,
            "pid": 1234,
            "log_path": "/workspace/.opencuria/processes/proc-1.log",
            "status": "running",
            "exit_code": None,
        }
        self.processes[record["process_id"]] = record
        return dict(record)

    async def process_list(self) -> list[dict[str, Any]]:
        """Return canned process records."""
        self._maybe_fail()
        return [dict(record) for record in self.processes.values()]

    async def process_get(self, process_id: str) -> dict[str, Any]:
        """Return one canned process record."""
        self._maybe_fail()
        try:
            return dict(self.processes[process_id])
        except KeyError as exc:
            raise RunnerAccessorError(
                f"process_get failed: unknown process {process_id}"
            ) from exc

    async def process_stop(self, process_id: str) -> dict[str, Any]:
        """Mark a canned process record stopped."""
        self._maybe_fail()
        try:
            record = self.processes[process_id]
        except KeyError as exc:
            raise RunnerAccessorError(
                f"process_stop failed: unknown process {process_id}"
            ) from exc
        record["status"] = "exited"
        record["exit_code"] = 0
        return dict(record)


@pytest.fixture
def fake_accessor() -> FakeAccessor:
    """Default fake accessor with one text file."""
    return FakeAccessor(files={"/workspace/a.txt": b"hello\nworld\n"})


@pytest.fixture
def harness_workspace(db, organization):
    """A running runners.Workspace for harness persistence tests."""
    import uuid as _uuid

    from django.contrib.auth import get_user_model

    from apps.runners.enums import RunnerStatus, WorkspaceStatus
    from apps.runners.models import Runner, Workspace
    from common.utils import hash_token

    user_model = get_user_model()
    user = user_model.objects.create_user(
        email=f"harness-{_uuid.uuid4().hex[:8]}@example.com",
        password="secret",
    )
    runner = Runner.objects.create(
        name="harness-runner",
        api_token_hash=hash_token(f"harness-{_uuid.uuid4().hex}"),
        status=RunnerStatus.ONLINE,
        sid=f"harness-sid-{_uuid.uuid4().hex[:8]}",
        organization=organization,
        available_runtimes=["docker"],
    )
    return Workspace.objects.create(
        runner=runner,
        name="Harness Workspace",
        status=WorkspaceStatus.RUNNING,
        created_by=user,
    )
