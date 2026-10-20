"""Architecture guard: REST router and Socket.IO modules are leaf adapters.

REST (``api.py``) and MCP (``apps.mcp_app``) are sibling adapters over the
same services. Shared request/response shapes live in ``schemas`` /
presenter modules, so no application module may import a REST router
module; only the URL composition root (``config/urls.py``) mounts them.
Likewise the Socket.IO handler module is mounted only by ``config/asgi.py``;
services and adapters resolve the runner service and frontend fan-out from
``apps.runners.services``. The runner service itself never imports the
harness; the harness plugs in through the ``HarnessGateway`` port.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = BACKEND_ROOT / "apps"


def _module_name(path: Path) -> str:
    """Return the dotted module name for a backend source file."""
    parts = path.relative_to(BACKEND_ROOT).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _source_files() -> list[Path]:
    """Return non-test, non-migration application modules."""
    return [
        path
        for path in APPS_ROOT.rglob("*.py")
        if not {"tests", "migrations"} & set(path.relative_to(APPS_ROOT).parts)
    ]


def _defines_ninja_router(tree: ast.Module) -> bool:
    """Return True when the module creates a module-level Ninja ``Router``."""
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
            node.value, ast.Call
        ):
            func = node.value.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "Router":
                return True
    return False


def _imported_modules(path: Path, tree: ast.Module) -> set[str]:
    """Return every module imported anywhere in *tree* (incl. function-local)."""
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            imported.add(module)
            # ``from apps.harness import api`` imports the submodule itself.
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    return imported


def test_no_application_module_imports_a_rest_router_module():
    """Adapters never import REST routers; MCP shares schemas/services instead."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    router_modules = {
        _module_name(path)
        for path, tree in trees.items()
        if _defines_ninja_router(tree)
    }
    assert "apps.harness.api" in router_modules  # discovery sanity check

    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        for module in _imported_modules(path, tree) & router_modules
        if module != _module_name(path)
    )
    assert violations == [], (
        "REST router modules must only be imported by config/urls.py; move "
        "shared schemas/mappers to a schemas or presenter module:\n"
        + "\n".join(violations)
    )


def test_no_application_module_imports_the_socketio_adapter():
    """Only the ASGI entry point mounts the Socket.IO handler module."""
    adapter = "apps.runners.sio_server"
    violations = sorted(
        _module_name(path)
        for path in _source_files()
        if _module_name(path) != adapter
        and any(
            module == adapter or module.startswith(f"{adapter}.")
            for module in _imported_modules(path, ast.parse(path.read_text()))
        )
    )
    assert violations == [], (
        "Import get_runner_service from apps.runners.services and "
        "emit_to_frontend/get_sio_server from "
        "apps.runners.services.infra.realtime instead of the Socket.IO "
        "adapter:\n" + "\n".join(violations)
    )


def test_runner_service_never_imports_the_harness():
    """Harness depends on the runner service, never the other way round."""
    package = APPS_ROOT / "runners" / "services"
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in package.rglob("*.py")
        for module in _imported_modules(path, ast.parse(path.read_text()))
        if module == "apps.harness" or module.startswith("apps.harness.")
    )
    assert violations == [], (
        "Reach the harness through the HarnessGateway port "
        "(apps.runners.services.ports) instead of importing it:\n"
        + "\n".join(violations)
    )
