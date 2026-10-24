"""Guard the dependency direction between the runners app and its dependents.

``harness``, ``scheduled_tasks`` and ``mcp_app`` build on ``runners``; runners
reaches the harness only through the ``HarnessGateway`` port. The scan covers
module-level and function-local imports as well as app-registry lookups, so
cycles cannot be hidden behind lazy imports or ``get_model`` strings.
"""

from __future__ import annotations

import ast
from pathlib import Path

RUNNERS_DIR = Path(__file__).resolve().parents[1]
FORBIDDEN_APPS = {"harness", "scheduled_tasks", "mcp_app"}


def _runner_modules() -> list[Path]:
    """Return every non-test, non-migration module of the runners app."""
    return [
        path
        for path in sorted(RUNNERS_DIR.rglob("*.py"))
        if not {"tests", "migrations"} & set(path.relative_to(RUNNERS_DIR).parts)
    ]


def _imported_apps(node: ast.AST) -> set[str]:
    """Return ``apps.<name>`` targets referenced by an import node."""
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        parts = node.module.split(".")
        if parts[0] != "apps":
            return set()
        if len(parts) > 1:
            return {parts[1]}
        return {alias.name.split(".")[0] for alias in node.names}
    if isinstance(node, ast.Import):
        return {
            alias.name.split(".")[1]
            for alias in node.names
            if alias.name.startswith("apps.")
        }
    return set()


def _registry_lookup_app(node: ast.AST) -> str | None:
    """Return the app label of a ``get_model("<app>", ...)`` call, if any."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    first = node.args[0]
    if name != "get_model" or not isinstance(first, ast.Constant):
        return None
    if not isinstance(first.value, str):
        return None
    return first.value.split(".")[0]


def test_runners_never_depends_on_its_dependents():
    violations = []
    for path in _runner_modules():
        relative = path.relative_to(RUNNERS_DIR).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            for app in _imported_apps(node) & FORBIDDEN_APPS:
                violations.append(f"{relative}:{node.lineno} imports apps.{app}")
            app = _registry_lookup_app(node)
            if app in FORBIDDEN_APPS:
                violations.append(f"{relative}:{node.lineno} looks up {app} models")
    assert violations == []


def test_guard_detects_function_local_imports_and_registry_lookups():
    source = (
        "def f():\n"
        "    from apps.harness.models import HarnessSession\n"
        "    import apps.mcp_app.server\n"
        "    from apps import scheduled_tasks\n"
        "    apps.get_model('harness', 'HarnessSession')\n"
    )
    nodes = list(ast.walk(ast.parse(source)))
    assert set().union(*map(_imported_apps, nodes)) == FORBIDDEN_APPS
    assert [app for app in map(_registry_lookup_app, nodes) if app] == ["harness"]
