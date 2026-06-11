"""Shared pytest configuration for the runner test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


def _qemu_runtime_deps_available() -> bool:
    """Return whether optional QEMU runtime Python deps are installed."""
    try:
        import asyncssh  # noqa: F401
        import libvirt  # noqa: F401
    except ImportError:
        return False
    return True


HAS_QEMU_RUNTIME_DEPS = _qemu_runtime_deps_available()
QEMU_SKIP_REASON = (
    "QEMU runtime dependencies not installed "
    "(pip install -r requirements-qemu.txt)"
)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Avoid importing QEMU-only modules when optional deps are missing."""
    if (
        not HAS_QEMU_RUNTIME_DEPS
        and collection_path.name == "test_qemu_runtime.py"
    ):
        return True
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip QEMU-related tests in mixed modules when optional deps are missing."""
    if HAS_QEMU_RUNTIME_DEPS:
        return

    skip_qemu = pytest.mark.skip(reason=QEMU_SKIP_REASON)
    for item in items:
        if "qemu" in item.nodeid.lower():
            item.add_marker(skip_qemu)
