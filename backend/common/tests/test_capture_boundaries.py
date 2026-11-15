"""Capture policy depends on persistence; persistence never loads its workflow."""

from __future__ import annotations

import ast

from common.tests.test_adapter_boundaries import (
    APPS_ROOT,
    ORM_QUERY_ATTRIBUTES,
    _imported_modules,
    _module_name,
    _source_files,
)

CAPTURE_SERVICE = "apps.runners.services.capture"
CAPTURE_REPOSITORY = "apps.runners.capture_repository"


def test_capture_persistence_never_imports_workflows() -> None:
    """Include transitive and function-local imports in the dependency guard."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    pending = {CAPTURE_REPOSITORY}
    checked: set[str] = set()
    violations: set[str] = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                part in {"service", "services"} or part.endswith("_service")
                for part in imported.split(".")
            ) or imported.startswith("apps.runners.recreate_repository"):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert not violations, (
        "CaptureRepository persists service-selected changes; credential checks "
        "and replacement coordination belong to CaptureService:\n"
        + "\n".join(sorted(violations))
    )


def test_capture_workflow_callers_use_the_service() -> None:
    """Allocation and recovery must share the service's admission and phase rules."""
    consumers = {
        _module_name(path)
        for path in _source_files()
        if CAPTURE_REPOSITORY in _imported_modules(path, ast.parse(path.read_text()))
    }
    assert consumers == {CAPTURE_SERVICE}, (
        "Reach capture persistence only through CaptureService; migrate every "
        f"workflow caller: {sorted(consumers)}"
    )


def test_capture_service_never_queries_or_writes_the_orm() -> None:
    """Transactions, queries, reloads and model saves remain in repositories."""
    path = APPS_ROOT / "runners" / "services" / "capture.py"
    tree = ast.parse(path.read_text())
    orm_attributes = ORM_QUERY_ATTRIBUTES | {
        "save",
        "asave",
        "delete",
        "adelete",
        "select_related",
        "select_for_update",
    }
    violations = [
        f"{path.name}:{node.lineno} accesses {node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in orm_attributes
    ]
    violations.extend(
        f"{path.name} imports {imported}"
        for imported in _imported_modules(path, tree)
        if imported == "django.db" or imported.startswith("django.db.")
    )
    assert not violations, "Keep capture ORM access in repositories:\n" + "\n".join(
        sorted(violations)
    )
