"""Keep runner modules layered: one-way dependencies, owners keep their state."""

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


REGISTRY_MODULE = SOURCE_ROOT / "services" / "workspace_registry.py"
REGISTRY_HOLDERS = {"registry", "_registry"}


def _private_registry_access(tree: ast.Module) -> list[str]:
    """Return private attributes read or written through a registry reference."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not node.attr.startswith("_"):
            continue
        holder = node.value
        name = getattr(holder, "id", None) or getattr(holder, "attr", "")
        if name in REGISTRY_HOLDERS and not node.attr.startswith("__"):
            found.append(f"{node.lineno}: {name}.{node.attr}")
    return sorted(found)


def test_registry_guard_sees_local_and_attribute_holders() -> None:
    """Private state is reported however the registry reference is held."""
    tree = ast.parse(
        "self.registry._cache.pop(workspace_id)\n"
        "registry._epochs.get(workspace_id)\n"
        "self._registry._active\n"
        "self.registry.evict(workspace_id)\n"
        "self._epochs.get(key)\n"
    )
    assert _private_registry_access(tree) == [
        "1: registry._cache",
        "2: registry._epochs",
        "3: _registry._active",
    ]


def test_workspace_registry_state_stays_behind_its_api() -> None:
    """Only the registry touches its cache and lifecycle fence directly."""
    violations = [
        f"{path.relative_to(RUNNER_ROOT)}:{finding}"
        for path in sorted(SOURCE_ROOT.rglob("*.py"))
        if path != REGISTRY_MODULE
        for finding in _private_registry_access(ast.parse(path.read_text()))
    ]
    assert not violations, (
        "Use the WorkspaceRegistry API instead of its private state:\n"
        + "\n".join(violations)
    )
