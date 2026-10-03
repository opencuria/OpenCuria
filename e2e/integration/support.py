"""Private, run-scoped artifacts and subprocess helpers for live integration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path("/workspace/.opencuria/integration")


def run_dir() -> Path:
    """Require an explicit directory confined to integration storage."""
    path = Path(os.environ["INTEGRATION_RUN_DIR"]).resolve()
    if not path.is_relative_to(ROOT.resolve()) or path == ROOT.resolve():
        raise ValueError("INTEGRATION_RUN_DIR must be a child of integration root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def save(name: str, value: Any) -> None:
    """Write private JSON, including authentication artifacts, atomically."""
    path = run_dir() / name
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, indent=2, default=str)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def load(name: str = "manifest.json") -> Any:
    """Load a run-owned artifact."""
    return json.loads((run_dir() / name).read_text())


def command(*args: str) -> str:
    """Execute a checked command without shell interpolation."""
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout
