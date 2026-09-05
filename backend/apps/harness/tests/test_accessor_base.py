"""Tests for the harness WorkspaceAccessor ABC sandbox."""

from __future__ import annotations

import pytest

from apps.harness.access.base import (
    HARNESS_WORKSPACE_ROOT,
    WorkspaceAccessor,
    sanitize_harness_path,
)


def test_sandbox_accepts_workspace_paths() -> None:
    """Absolute and relative paths resolve under /workspace."""
    assert sanitize_harness_path("/workspace") == "/workspace"
    assert sanitize_harness_path("/workspace/a/b.txt") == "/workspace/a/b.txt"
    assert sanitize_harness_path("a/b.txt") == "/workspace/a/b.txt"
    assert sanitize_harness_path("/workspace/a/../b") == "/workspace/b"


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/workspace/../etc/passwd",
        "/workspace/../../root",
        "..",
        "../secret",
        "/",
        "",
    ],
)
def test_sandbox_rejects_traversal(path: str) -> None:
    """Traversal outside /workspace raises ValueError."""
    with pytest.raises(ValueError, match="under /workspace"):
        sanitize_harness_path(path)


def test_workspace_accessor_is_abstract() -> None:
    """The ABC cannot be instantiated without all methods."""
    with pytest.raises(TypeError):
        WorkspaceAccessor("ws-1")  # type: ignore[abstract]


def test_workspace_root_constant() -> None:
    """Workspace root is pinned to /workspace."""
    assert HARNESS_WORKSPACE_ROOT == "/workspace"
