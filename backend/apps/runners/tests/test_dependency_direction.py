"""Guard the dependency direction between the runners app and its dependents.

``harness``, ``scheduled_tasks`` and ``mcp_app`` build on ``runners``; runners
reaches the harness only through the ``HarnessGateway`` port. The scan covers
module-level and function-local imports as well as app-registry lookups, so
cycles cannot be hidden behind lazy imports or ``get_model`` strings.
Runner services also delegate ORM queries to repositories, including filtering
and existence checks on querysets returned by a repository.
Lifecycle result policy belongs to its service; journal persistence never
imports that service or its transport consumers.
Operator disposition policy also belongs to its service; its repository never
imports any application's services, including through function-local imports.
Image retention policy and scheduling live in their service, while repositories
own the transaction and runner lock shared by selection and retirement.
"""

from __future__ import annotations

import ast
from pathlib import Path

RUNNERS_DIR = Path(__file__).resolve().parents[1]
FORBIDDEN_APPS = {"harness", "scheduled_tasks", "mcp_app"}
ORM_QUERY_ATTRIBUTES = {
    "objects",
    "_default_manager",
    "refresh_from_db",
    "arefresh_from_db",
    "filter",
    "exclude",
    "select_related",
    "prefetch_related",
    "select_for_update",
    "order_by",
    "values_list",
    "exists",
    "aexists",
    "annotate",
    "aggregate",
    "bulk_create",
    "bulk_update",
    "raw",
}


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
        "    import apps.scheduled_tasks.services\n"
        "    import apps.mcp_app.server\n"
        "    from apps import scheduled_tasks\n"
        "    apps.get_model('harness', 'HarnessSession')\n"
    )
    nodes = list(ast.walk(ast.parse(source)))
    assert set().union(*map(_imported_apps, nodes)) == FORBIDDEN_APPS
    assert [app for app in map(_registry_lookup_app, nodes) if app] == ["harness"]


def _orm_query_accesses(source: str) -> list[tuple[int, str]]:
    """Find manager access and queryset operations, including in local functions."""
    return sorted(
        (node.lineno, node.attr)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr in ORM_QUERY_ATTRIBUTES
    )


def test_runner_services_delegate_orm_queries_to_repositories() -> None:
    """Services use repository reads without constructing their own ORM queries."""
    violations = [
        f"{path.relative_to(RUNNERS_DIR)}:{line} accesses {attribute}"
        for path in sorted((RUNNERS_DIR / "services").rglob("*.py"))
        for line, attribute in _orm_query_accesses(path.read_text())
    ]
    assert violations == [], (
        "Move ORM queries, queryset filtering and existence checks into the "
        "owning repository; services should consume its results:\n"
        + "\n".join(violations)
    )


def test_query_guard_detects_managers_and_repository_queryset_chaining() -> None:
    """Returning a queryset from a repository must not bypass the boundary."""
    assert _orm_query_accesses(
        "def read():\n"
        "    Runner.objects.get(sid='session')\n"
        "    def nested():\n"
        "        return repository.list_all().filter(owner=1).exists()\n"
        "    return repository.list_all().exclude(status='removed')\n"
    ) == [(2, "objects"), (4, "exists"), (4, "filter"), (5, "exclude")]
    assert (
        _orm_query_accesses(
            "repository.get_by_id(identifier)\n"
            "payload.get('workspace_id')\n"
            "list(grouped.values())\n"
        )
        == []
    )


def test_lifecycle_result_persistence_never_imports_services() -> None:
    """The result service owns dispatch; repositories only lock and persist."""
    persistence = [
        RUNNERS_DIR / "operations.py",
        RUNNERS_DIR / "operation_result_repository.py",
    ]
    violations = []
    for path in persistence:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            modules = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = "apps.runners" if node.level else ""
                module = ".".join(part for part in (base, node.module) if part)
                modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
            for module in modules:
                if module == "apps.runners.services" or module.startswith(
                    "apps.runners.services."
                ):
                    violations.append(f"{path.name}:{node.lineno} imports {module}")
    assert violations == [], (
        "Keep lifecycle result dispatch in services/operation_results.py; "
        "persistence must not import service orchestration:\n" + "\n".join(violations)
    )


def test_lifecycle_result_service_never_imports_orm_modules() -> None:
    """The service consumes loaded result state instead of lazy database models."""
    path = RUNNERS_DIR / "services" / "operation_results.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or "", *(alias.name for alias in node.names)]
        for module in modules:
            if module.split(".")[0] == "django" or module.split(".")[-1] in {
                "models",
                "locking",
            }:
                violations.append(f"{path.name}:{node.lineno} imports {module}")
    assert violations == [], (
        "Keep result models, transactions and locking behind "
        "OperationResultRepository:\n" + "\n".join(violations)
    )


def test_disposition_repository_never_imports_services() -> None:
    """Authorization and action policy must not flow back into persistence."""
    path = RUNNERS_DIR / "disposition_repository.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            modules = [module, *(f"{module}.{alias.name}" for alias in node.names)]
        for module in modules:
            if {"service", "services"} & set(module.split(".")):
                violations.append(f"{path.name}:{node.lineno} imports {module}")
    assert violations == [], (
        "Keep organization authorization, allowed actions and disposition policy "
        "in DispositionService; repositories only load, lock and persist:\n"
        + "\n".join(violations)
    )


def test_disposition_service_delegates_transactions_and_writes() -> None:
    """The disposition service cannot bypass its transaction-scoped repository."""
    path = RUNNERS_DIR / "services" / "disposition.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or "", *(alias.name for alias in node.names)]
        else:
            modules = []
        for module in modules:
            if module.split(".")[0] == "django" or module in {
                "apps.runners.models",
                "apps.runners.locking",
                "models",
                "locking",
            }:
                violations.append(f"{path.name}:{node.lineno} imports {module}")
        if isinstance(node, ast.Attribute) and node.attr in {
            "save",
            "asave",
            "delete",
            "adelete",
        }:
            violations.append(f"{path.name}:{node.lineno} calls {node.attr}")
    assert violations == [], (
        "Keep transactions, model writes and locking behind DispositionRepository:\n"
        + "\n".join(violations)
    )


def test_image_retention_service_delegates_transactions_and_writes() -> None:
    """Retention policy cannot bypass the runner lock or write models directly."""
    path = RUNNERS_DIR / "services" / "image_retention.py"
    violations = []
    for node in ast.walk(ast.parse(path.read_text())):
        modules = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or "", *(alias.name for alias in node.names)]
        for module in modules:
            if module.startswith("django.db") or module.split(".")[-1] in {
                "locking",
                "transaction",
            }:
                violations.append(f"{path.name}:{node.lineno} imports {module}")
        if isinstance(node, ast.Attribute) and node.attr in {
            "save",
            "asave",
            "delete",
            "adelete",
        }:
            violations.append(f"{path.name}:{node.lineno} calls {node.attr}")
    assert violations == [], (
        "Keep ORM writes, transactions and locking behind retention repositories:\n"
        + "\n".join(violations)
    )
