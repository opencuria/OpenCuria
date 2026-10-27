"""Keep session authorization in its owning service, away from adapters."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ACCESS_SERVICE = "apps.harness.session_access_service"
ACCESS_FIELDS = {"harness_id", "created_by_id", "parent_id"}


def _reads_access_identity(node: ast.AST) -> bool:
    """Recognize direct and getattr-based identity reads within a comparison."""
    return any(
        (isinstance(child, ast.Attribute) and child.attr in ACCESS_FIELDS)
        or (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "getattr"
            and len(child.args) >= 2
            and isinstance(child.args[1], ast.Constant)
            and child.args[1].value in ACCESS_FIELDS
        )
        for child in ast.walk(node)
    )


def test_session_adapters_never_decide_ownership_or_promptability() -> None:
    """Include function-local comparisons so moving a policy inline cannot hide it."""
    adapters = ["apps/harness/api.py", "apps/mcp_app/tools/chats.py"]
    for adapter in adapters:
        tree = ast.parse((BACKEND_ROOT / adapter).read_text())
        violations = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and _reads_access_identity(node)
        ]
        assert violations == [], (
            f"{adapter}:{violations}: session ownership and parentage decisions "
            "belong to SessionAccessService, including in local functions"
        )


def test_session_access_service_has_no_application_dependencies() -> None:
    """The policy cannot import its adapters, orchestration or storage consumers."""
    path = BACKEND_ROOT / "apps/harness/session_access_service.py"
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0 and not (node.module or "").startswith("apps.")
        elif isinstance(node, ast.Import):
            assert not any(alias.name.startswith("apps.") for alias in node.names)

    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import {ACCESS_SERVICE}\n"
            "import sys\n"
            "assert 'django' not in sys.modules\n",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
