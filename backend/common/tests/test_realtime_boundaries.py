"""Shared event transport must stay below the feature applications."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_shared_transport_never_imports_feature_contexts() -> None:
    """Include function-local imports so lazy factories cannot hide coupling."""
    path = BACKEND_ROOT / "common" / "realtime.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            continue
        violations.extend(
            f"{path.name}:{node.lineno} imports {module}"
            for module in modules
            if module == "apps" or module.startswith("apps.")
        )
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "statement",
    [
        "from common import realtime",
        "from apps.harness.conversation_events import ConversationEvents",
    ],
    ids=["shared_transport", "conversation_publisher"],
)
def test_event_transport_imports_without_application_setup(statement: str) -> None:
    """Importing an event publisher cannot load runner workflows or ORM models."""
    source = f"""
import sys
from django.conf import settings
{statement}
from common import realtime
assert realtime._sio is None
assert not settings.configured
assert not any(
    module == 'apps.runners' or module.startswith('apps.runners.')
    or module == 'apps.harness.harness_service' or module.startswith('django.db')
    for module in sys.modules
)
"""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
