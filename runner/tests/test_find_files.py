"""Tests for WorkspaceService.find_files mention search."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.service import FIND_FILES_DEFAULT_LIMIT, WorkspaceService


def _service() -> tuple[WorkspaceService, DummyRuntime, uuid.UUID]:
    runtime = DummyRuntime()
    svc = WorkspaceService(runtimes={"docker": runtime}, settings=RunnerSettings())
    workspace_id = uuid.uuid4()
    svc._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )
    return svc, runtime, workspace_id


class DummyRuntime:
    """Minimal runtime stub for find_files tests."""

    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock(return_value=(0, ""))


def test_build_find_files_command_prunes_and_caps() -> None:
    command = WorkspaceService.build_find_files_command("src/a.ts", 50)
    assert command[:2] == ["bash", "-lc"]
    pipeline = command[2]
    assert "-name '.git'" in pipeline or "-name .git" in pipeline
    assert "node_modules" in pipeline
    assert "-ipath" in pipeline
    assert "*src/a.ts*" in pipeline
    assert f"head -n {FIND_FILES_DEFAULT_LIMIT + 1}" in pipeline


def test_build_find_files_command_empty_query_has_no_ipath() -> None:
    pipeline = WorkspaceService.build_find_files_command("", 8)[2]
    assert "-ipath" not in pipeline
    assert "head -n 9" in pipeline


def test_sanitize_find_query_rejects_globs_and_traversal() -> None:
    with pytest.raises(ValueError, match="Invalid find query"):
        WorkspaceService.sanitize_find_query("foo*")
    with pytest.raises(ValueError, match="Invalid find query"):
        WorkspaceService.sanitize_find_query("../etc/passwd")
    assert WorkspaceService.sanitize_find_query("src/a.ts") == "src/a.ts"
    assert WorkspaceService.sanitize_find_query("") == ""


@pytest.mark.asyncio
async def test_find_files_parses_depth_prefixed_output() -> None:
    svc, runtime, workspace_id = _service()
    runtime.exec_command_wait.return_value = (
        0,
        "1\t/workspace/README.md\n2\t/workspace/src/a.ts\n",
    )
    result = await svc.find_files(workspace_id, query="a", limit=50)
    assert result["truncated"] is False
    assert result["paths"] == [
        {"name": "README.md", "path": "/workspace/README.md"},
        {"name": "a.ts", "path": "/workspace/src/a.ts"},
    ]


@pytest.mark.asyncio
async def test_find_files_treats_sigpipe_as_success_and_truncates() -> None:
    svc, runtime, workspace_id = _service()
    runtime.exec_command_wait.return_value = (
        141,
        "\n".join(f"1\t/workspace/f{i}.txt" for i in range(FIND_FILES_DEFAULT_LIMIT + 1)),
    )
    result = await svc.find_files(workspace_id, query="", limit=50)
    assert result["truncated"] is True
    assert len(result["paths"]) == FIND_FILES_DEFAULT_LIMIT


@pytest.mark.asyncio
async def test_find_files_rejects_non_workspace_paths() -> None:
    svc, runtime, workspace_id = _service()
    runtime.exec_command_wait.return_value = (0, "1\t/etc/passwd\n")
    result = await svc.find_files(workspace_id)
    assert result["paths"] == []
