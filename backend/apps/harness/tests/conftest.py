"""Shared fixtures for the harness app tests."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

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
    ) -> None:
        super().__init__(workspace_id)
        self.files: dict[str, bytes] = dict(files or {})
        self.error = error
        self.exec_result = exec_result
        self.written: dict[str, bytes] = {}
        self.exec_calls: list[tuple] = []

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

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
        if text.startswith("find "):
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
