"""Shared pytest configuration for the runner test suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from src.config import RunnerSettings


@pytest.fixture(autouse=True)
def isolated_runner_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep unit tests independent of the deployment's private dotenv file."""
    monkeypatch.setitem(RunnerSettings.model_config, "env_file", None)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip process-control tests when Linux /proc is unavailable."""
    skip_proc = pytest.mark.skip(reason="requires Linux /proc")
    has_proc = Path("/proc").is_dir()
    for item in items:
        if not has_proc and (
            "test_managed_streams.py" in item.nodeid
            or "test_wrapper_pidfile" in item.nodeid
        ):
            item.add_marker(skip_proc)
