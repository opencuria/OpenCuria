"""Architecture guard: the MCP adapter is a registry of independent domains.

``apps.mcp_app.transport`` serves ``apps.mcp_app.server``, which exposes the
registry composed in ``apps.mcp_app.tools``. Each domain module there owns
the schemas, permissions and handlers of its tools. Domains share only
``apps.mcp_app.access`` and ``apps.mcp_app.results``; they never import each
other, the server or the transport, and no module grows back into a god file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from apps.mcp_app import tools

MCP_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = MCP_ROOT / "tools"
SHARED_MODULES = {"apps.mcp_app.access", "apps.mcp_app.results"}
MAX_MODULE_LINES = 800


def _domain_paths() -> list[Path]:
    """Return every tool domain module (the package ``__init__`` composes them)."""
    return sorted(path for path in TOOLS_DIR.glob("*.py") if path.name != "__init__.py")


def _imported_modules(tree: ast.Module) -> set[str]:
    """Return modules imported by a ``apps.mcp_app.tools.<domain>`` module."""
    package = ["apps", "mcp_app", "tools"]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def test_every_tool_module_is_a_registered_domain():
    registered = {domain.__name__ for domain in tools.DOMAINS}
    assert registered == {f"apps.mcp_app.tools.{path.stem}" for path in _domain_paths()}


def test_each_domain_declares_permission_and_handler_for_every_tool():
    owners: dict[str, str] = {}
    for domain in tools.DOMAINS:
        names = [tool.name for tool in domain.TOOLS]
        assert len(names) == len(set(names)), domain.__name__
        assert set(names) == set(domain.PERMISSIONS) == set(domain.HANDLERS), (
            domain.__name__
        )
        for name in names:
            assert name not in owners, f"{name}: {owners[name]} and {domain.__name__}"
            owners[name] = domain.__name__

    assert set(owners) == {tool.name for tool in tools.TOOLS}
    assert set(owners) == set(tools.TOOL_PERMISSIONS) == set(tools.TOOL_HANDLERS)


def test_domains_share_only_access_and_results():
    violations = sorted(
        f"{path.stem} -> {module}"
        for path in _domain_paths()
        for module in _imported_modules(ast.parse(path.read_text()))
        if module.startswith("apps.mcp_app.")
        and module not in SHARED_MODULES
        and not any(module.startswith(f"{shared}.") for shared in SHARED_MODULES)
    )
    assert violations == [], (
        "MCP tool domains must not import each other, the server or the "
        "transport; move shared adapter code into apps.mcp_app.access or "
        "apps.mcp_app.results:\n" + "\n".join(violations)
    )


def test_guard_resolves_relative_imports():
    source = (
        "from .. import access\n"
        "from ..results import text_result\n"
        "from .git import TOOLS\n"
        "from ..server import create_mcp_server\n"
    )
    imported = _imported_modules(ast.parse(source))
    assert {"apps.mcp_app.access", "apps.mcp_app.results"} <= imported
    assert {"apps.mcp_app.tools.git", "apps.mcp_app.server"} <= imported


def test_mcp_modules_stay_small():
    oversized = sorted(
        f"{path.relative_to(MCP_ROOT)}: {lines} lines"
        for path in MCP_ROOT.rglob("*.py")
        if "tests" not in path.relative_to(MCP_ROOT).parts
        and (lines := len(path.read_text().splitlines())) > MAX_MODULE_LINES
    )
    assert oversized == [], (
        "Split oversized MCP modules by domain instead of growing them:\n"
        + "\n".join(oversized)
    )
