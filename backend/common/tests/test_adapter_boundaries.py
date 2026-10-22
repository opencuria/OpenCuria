"""Architecture guard: REST router and Socket.IO modules are leaf adapters.

REST (``api.py``) and MCP (``apps.mcp_app``) are sibling adapters over the
same services. Shared request/response shapes live in ``schemas`` /
presenter modules, so no application module may import a REST router
module; only the URL composition root (``config/urls.py``) mounts them.
Likewise the realtime adapters (the Socket.IO handler module and the desktop
proxy) are mounted only by ``config/asgi.py``; services and adapters resolve
the runner service, frontend fan-out and desktop tunnels from
``apps.runners.services``. The runner service itself never imports the
harness; the harness plugs in through the ``HarnessGateway`` port. The
harness in turn never imports the apps built on it; they subscribe to its
run events (``apps.harness.run_events``). Likewise the foundational contexts
(organizations, credentials, skills, plugins) only import the contexts below
them; apps built on them subscribe to ``apps.organizations.events`` and
``apps.credentials.deletion_guards`` and share ``common.schemas``. Workspaces
compose plugins, so workspace-scoped plugin endpoints and guards live in
``apps.runners``. REST, MCP and realtime adapters never query the ORM
themselves; e.g. REST and MCP both authenticate API keys through
``apps.accounts.services.APIKeyService``, and the realtime adapters reach
models and repositories only through services.
"""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = BACKEND_ROOT / "apps"

#: ASGI adapters for runner/frontend Socket.IO traffic and desktop streams.
REALTIME_ADAPTERS = ("apps.runners.sio_server", "apps.runners.desktop_proxy")

#: Foundational bounded contexts and the only other apps each may import.
FOUNDATION_DEPENDENCIES = {
    "apps.organizations": {"apps.accounts"},
    "apps.credentials": {"apps.accounts", "apps.organizations"},
    "apps.skills": {"apps.accounts", "apps.organizations"},
    "apps.plugins": {"apps.accounts", "apps.organizations", "apps.credentials"},
}


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


def _persistence_modules(trees: dict[Path, ast.Module]) -> set[str]:
    """Return ``models`` modules and modules defining a ``*Repository`` class."""
    return {
        _module_name(path)
        for path, tree in trees.items()
        if _module_name(path).rsplit(".", 1)[-1] == "models"
        or any(
            isinstance(node, ast.ClassDef) and node.name.endswith("Repository")
            for node in tree.body
        )
    }


def test_no_application_module_imports_a_realtime_adapter():
    """Only the ASGI entry point mounts the Socket.IO and desktop proxy adapters."""
    violations = sorted(
        f"{_module_name(path)} -> {adapter}"
        for path in _source_files()
        for module in _imported_modules(path, ast.parse(path.read_text()))
        for adapter in REALTIME_ADAPTERS
        if _module_name(path) != adapter
        and (module == adapter or module.startswith(f"{adapter}."))
    )
    assert violations == [], (
        "Import get_runner_service from apps.runners.services, "
        "emit_to_frontend/get_sio_server from "
        "apps.runners.services.infra.realtime and desktop tunnels from "
        "apps.runners.services.infra.desktop_tunnels instead of a realtime "
        "adapter:\n" + "\n".join(violations)
    )


def test_realtime_adapters_reach_persistence_only_through_services():
    """Access rules, presence and telemetry live in services, not in adapters."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    persistence = _persistence_modules(trees)
    assert {"apps.runners.models", "apps.runners.operations"} <= persistence

    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        if _module_name(path) in REALTIME_ADAPTERS
        for module in _imported_modules(path, tree)
        if any(
            module == owner or module.startswith(f"{owner}.")
            for owner in persistence
        )
    )
    assert violations == [], (
        "Realtime adapters must call apps.runners.services (RunnerService, "
        "StorageService) instead of importing models or repositories:\n"
        + "\n".join(violations)
    )


def test_adapters_reach_image_recipes_only_through_their_service():
    """REST and MCP share ``ImageDefinitionService`` for recipes and builds."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    adapters = {
        path
        for path, tree in trees.items()
        if _defines_ninja_router(tree) or _module_name(path).startswith("apps.mcp_app")
    }
    persistence = {
        "apps.runners.models.ImageBuildJob",
        "apps.runners.models.ImageDefinition",
        "apps.runners.repositories.ImageBuildJobRepository",
        "apps.runners.repositories.ImageDefinitionRepository",
    }
    violations = sorted(
        f"{_module_name(path)} -> {name}"
        for path in adapters
        for name in _imported_modules(path, trees[path]) & persistence
    )
    assert violations == [], (
        "Use apps.runners.services.image_definitions.ImageDefinitionService "
        "instead of image recipe models/repositories in adapters:\n"
        + "\n".join(violations)
    )


def test_adapters_never_query_the_orm():
    """REST, MCP and realtime adapters call services; only repositories query."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    adapters = {
        path
        for path, tree in trees.items()
        if _defines_ninja_router(tree)
        or _module_name(path) == "apps.accounts.api_auth"
        or _module_name(path).startswith("apps.mcp_app")
        or _module_name(path) in REALTIME_ADAPTERS
    }
    assert APPS_ROOT / "accounts" / "api.py" in adapters  # discovery sanity check
    assert APPS_ROOT / "runners" / "desktop_proxy.py" in adapters

    violations = sorted(
        f"{_module_name(path)}:{node.lineno}"
        for path in adapters
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute)
        and node.attr in {"objects", "_default_manager"}
    )
    assert violations == [], (
        "Move ORM access into the app's repositories and call it through its "
        "service from REST and MCP alike:\n" + "\n".join(violations)
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


def test_harness_never_imports_the_apps_built_on_it():
    """Consumers subscribe to harness run events; the harness never calls them."""
    consumers = ("apps.scheduled_tasks", "apps.mcp_app")
    package = APPS_ROOT / "harness"
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in _source_files()
        if path.is_relative_to(package)
        for module in _imported_modules(path, ast.parse(path.read_text()))
        if any(module == app or module.startswith(f"{app}.") for app in consumers)
    )
    assert violations == [], (
        "Connect a receiver to apps.harness.run_events from the consuming "
        "app's AppConfig.ready() instead of importing it from the harness:\n"
        + "\n".join(violations)
    )


def test_foundational_apps_never_import_the_apps_built_on_them():
    """Dependencies point from feature apps down to the foundation only."""
    violations = []
    for path in _source_files():
        app = ".".join(_module_name(path).split(".")[:2])
        allowed = FOUNDATION_DEPENDENCIES.get(app)
        if allowed is None:
            continue
        violations.extend(
            f"{_module_name(path)} -> {module}"
            for module in _imported_modules(path, ast.parse(path.read_text()))
            if module.startswith("apps.")
            and ".".join(module.split(".")[:2]) not in {app, *allowed}
        )
    assert violations == [], (
        "Foundational apps must not import the apps built on them; publish an "
        "event or guard signal they subscribe to, and put shared schemas in "
        "common.schemas:\n" + "\n".join(sorted(violations))
    )
