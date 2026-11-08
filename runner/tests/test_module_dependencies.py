"""Keep runner modules layered: dependencies point one way, without cycles."""

from __future__ import annotations

import ast
from graphlib import CycleError, TopologicalSorter
from importlib.util import resolve_name
from pathlib import Path

import pytest

RUNNER_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = RUNNER_ROOT / "src"


def _module_name(path: Path) -> str:
    """Return the dotted module name for a runner source file."""
    parts = path.relative_to(RUNNER_ROOT).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(module: str, tree: ast.Module, *, package: bool) -> set[str]:
    """Resolve every import, including function-local and TYPE_CHECKING ones."""
    anchor = module if package else module.rpartition(".")[0]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = resolve_name("." * node.level + (node.module or ""), anchor)
            imported.add(base)
            imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def _graph(sources: dict[str, tuple[ast.Module, bool]]) -> dict[str, set[str]]:
    """Map each module to the other source modules it imports."""
    return {
        module: (_imports(module, tree, package=package) & sources.keys()) - {module}
        for module, (tree, package) in sources.items()
    }


def test_guard_sees_function_local_and_type_checking_imports() -> None:
    """Imports that dodge a cycle at runtime still count as dependencies."""
    store = ast.parse(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from .service import Service\n"
    )
    service = ast.parse("def wire():\n    from .store import Store\n")
    graph = _graph({"src.store": (store, False), "src.service": (service, False)})
    with pytest.raises(CycleError):
        tuple(TopologicalSorter(graph).static_order())


def test_runner_modules_are_acyclic() -> None:
    """Persistence, services, evidence, execution and transport layer one way."""
    sources = {
        _module_name(path): (ast.parse(path.read_text()), path.name == "__init__.py")
        for path in SOURCE_ROOT.rglob("*.py")
    }
    try:
        tuple(TopologicalSorter(_graph(sources)).static_order())
    except CycleError as error:
        # CycleError lists dependencies before their importers; read it as imports.
        cycle = " -> ".join(reversed(error.args[1]))
        pytest.fail(
            "Runner import cycle; move the shared piece into a lower module "
            f"instead of importing lazily: {cycle}"
        )
