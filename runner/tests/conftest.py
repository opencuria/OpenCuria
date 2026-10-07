"""Shared pytest configuration for the runner test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.config import RunnerSettings


@pytest.fixture(autouse=True)
def isolated_runner_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests independent of the deployment's private dotenv file."""
    monkeypatch.setitem(RunnerSettings.model_config, "env_file", None)


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
    "QEMU runtime dependencies not installed (pip install -r requirements-qemu.txt)"
)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Avoid importing QEMU-only modules when optional deps are missing."""
    if not HAS_QEMU_RUNTIME_DEPS and collection_path.name in {
        "test_qemu_runtime.py",
        "test_storage_inventory.py",
    }:
        return True
    return None


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip QEMU-related tests in mixed modules when optional deps are missing."""
    skip_qemu = pytest.mark.skip(reason=QEMU_SKIP_REASON)
    skip_proc = pytest.mark.skip(reason="requires Linux /proc")
    has_proc = Path("/proc").is_dir()
    for item in items:
        if not HAS_QEMU_RUNTIME_DEPS and "qemu" in item.nodeid.lower():
            item.add_marker(skip_qemu)
        if not has_proc and (
            "test_managed_streams.py" in item.nodeid
            or "test_wrapper_pidfile" in item.nodeid
        ):
            item.add_marker(skip_proc)
