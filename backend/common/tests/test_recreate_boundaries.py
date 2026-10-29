"""Recreate policy depends on persistence; persistence never owns the workflow."""

from __future__ import annotations

import ast

from common.tests.test_adapter_boundaries import (
    APPS_ROOT,
    ORM_QUERY_ATTRIBUTES,
    _imported_modules,
    _module_name,
    _source_files,
)

RECREATE_SERVICE = "apps.runners.services.recreate"
RECREATE_REPOSITORY = "apps.runners.recreate_repository"


def test_recreate_persistence_never_imports_workflows() -> None:
    """Include transitive and function-local imports in the dependency guard."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    pending = {RECREATE_REPOSITORY}
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
            ) or imported in {
                "apps.runners.provisioning",
                "apps.runners.capture_repository",
            }:
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert not violations, (
        "RecreateRepository only locks, reads and persists; admission, command "
        "selection and phase progression belong to RecreateService:\n"
        + "\n".join(sorted(violations))
    )


def test_recreate_workflow_callers_use_the_service() -> None:
    """Allocation, capture handoff and recovery share a single workflow owner."""
    consumers = {
        _module_name(path)
        for path in _source_files()
        if RECREATE_REPOSITORY in _imported_modules(path, ast.parse(path.read_text()))
    }
    assert consumers == {RECREATE_SERVICE}, (
        "Reach recreate persistence only through RecreateService; migrate every "
        f"workflow caller: {sorted(consumers)}"
    )


def test_recreate_service_never_queries_or_writes_the_orm() -> None:
    """Transactions, queries, reloads and model saves remain in repositories."""
    path = APPS_ROOT / "runners" / "services" / "recreate.py"
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
        if imported == "django.db"
        or imported.startswith("django.db.")
        or imported.startswith("apps.runners.locking")
    )
    assert not violations, "Keep recreate ORM access in repositories:\n" + "\n".join(
        sorted(violations)
    )
