"""Keep workspace access below the MCP protocols that consume its contracts."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from importlib.util import resolve_name
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
HARNESS_ROOT = BACKEND_ROOT / "apps" / "harness"


def _imports(path: Path) -> set[str]:
    """Resolve absolute and relative imports, including function-local ones."""
    package = ".".join(path.parent.relative_to(BACKEND_ROOT).parts)
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


@pytest.mark.parametrize(
    ("package", "forbidden"),
    [
        ("access", "apps.harness.mcp_client"),
        ("mcp_client", "apps.harness.access.runner_accessor"),
    ],
)
def test_workspace_access_and_mcp_dependencies_point_downward(
    package: str, forbidden: str
) -> None:
    """MCP uses access contracts; access never imports its MCP consumers."""
    violations = sorted(
        f"{path.relative_to(HARNESS_ROOT)} -> {module}"
        for path in (HARNESS_ROOT / package).rglob("*.py")
        for module in _imports(path)
        if module == forbidden or module.startswith(f"{forbidden}.")
    )
    assert violations == [], "Keep stream contracts and diagnostics below MCP:\n" + (
        "\n".join(violations)
    )


@pytest.mark.parametrize(
    ("module", "forbidden"),
    [
        ("common.stream_diagnostics", ["apps", "django", "mcp"]),
        (
            "apps.harness.access.base",
            [
                "apps.runners",
                "apps.harness.access.runner_accessor",
                "apps.harness.mcp_client",
                "django",
                "mcp",
            ],
        ),
        (
            "apps.harness.mcp_client.stdio",
            ["apps.runners", "apps.harness.access.runner_accessor", "django"],
        ),
        (
            "apps.harness.mcp_client.workspace_http",
            ["apps.runners", "apps.harness.access.runner_accessor", "django"],
        ),
    ],
)
def test_stream_contracts_and_transports_import_without_runner_or_django(
    module: str, forbidden: list[str]
) -> None:
    """Fresh imports must stay independent, including package initializers."""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys\n"
            f"importlib.import_module({module!r})\n"
            f"forbidden = {forbidden!r}\n"
            "loaded = sorted(name for name in sys.modules "
            "if any(name == root or name.startswith(root + '.') "
            "for root in forbidden))\n"
            "assert not loaded, loaded\n",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
