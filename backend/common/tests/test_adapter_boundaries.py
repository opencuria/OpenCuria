"""Enforce service, persistence and adapter boundaries, including local imports.

Parse the application once per module and share dependency traversal across rules.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from graphlib import TopologicalSorter
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = BACKEND_ROOT / "apps"

#: ASGI adapters for runner/frontend Socket.IO traffic and desktop streams.
REALTIME_ADAPTERS = ("apps.runners.sio_server", "apps.runners.desktop_proxy")

#: Foundational bounded contexts and the only other apps each may import.
FOUNDATION_DEPENDENCIES = {
    "apps.accounts": set(),
    "apps.organizations": {"apps.accounts"},
    "apps.credentials": {"apps.accounts", "apps.organizations"},
    "apps.skills": {"apps.accounts", "apps.organizations"},
    "apps.plugins": {"apps.accounts", "apps.organizations", "apps.credentials"},
}

#: Bounded contexts that other apps may reach only through their ``services``.
SERVICE_ONLY_CONTEXTS = {"apps.skills"}

#: Bounded contexts whose repositories are private, with the service to use.
PRIVATE_REPOSITORY_CONTEXTS = {
    "apps.credentials": (
        "resolve catalog entries and workspace attachments through "
        "apps.credentials.services"
    ),
    "apps.organizations": (
        "check organizations and active memberships through "
        "apps.organizations.services.OrganizationService"
    ),
    "apps.plugins": (
        "read and replace workspace plugin selections through "
        "apps.plugins.services.PluginService"
    ),
}

#: Apps whose non-persistence modules reach the ORM only through repositories.
REPOSITORY_ONLY_APPS = {
    "apps.accounts",
    "apps.credentials",
    "apps.harness",
    "apps.mcp_app",
    "apps.mobile_apps",
    "apps.notifications",
    "apps.organizations",
    "apps.plugins",
    "apps.scheduled_tasks",
    "apps.skills",
}

#: Model manager and instance-reload attributes that issue queries.
ORM_QUERY_ATTRIBUTES = {"objects", "_default_manager", "refresh_from_db"}

#: Queryset operations also expose ORM through related managers on loaded models.
ADAPTER_QUERY_ATTRIBUTES = ORM_QUERY_ATTRIBUTES | {
    "all",
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

#: ``CredentialSvc`` methods that decrypt credential secrets.
CREDENTIAL_RESOLUTION_CALLS = {"resolve_credentials", "resolve_workspace_credentials"}

DependencyGraph = dict[str, set[str]]
SourceSelector = str | tuple[str, ...]


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


@pytest.fixture(scope="module")
def trees() -> dict[Path, ast.Module]:
    """Read and parse one consistent application snapshot for all rules."""
    return {path: ast.parse(path.read_text()) for path in _source_files()}


@pytest.fixture(scope="module")
def modules(trees: dict[Path, ast.Module]) -> dict[str, Path]:
    """Index the source snapshot by its importable module names."""
    return {_module_name(path): path for path in trees}


@pytest.fixture(scope="module")
def dependencies(trees: dict[Path, ast.Module]) -> dict[str, set[str]]:
    """Resolve imports, including aliases and function-local relative imports."""
    return {
        _module_name(path): _imported_modules(path, tree)
        for path, tree in trees.items()
    }


def _within(module: str, namespaces: Iterable[str]) -> bool:
    """Match a namespace itself and its dotted descendants, not similar names."""
    return any(module == name or module.startswith(f"{name}.") for name in namespaces)


def _dependency_violations(
    dependencies: dict[str, set[str]],
    roots: Iterable[str],
    forbidden: Callable[[str], bool],
) -> set[str]:
    """Report forbidden edges reachable from roots without traversing them."""
    pending = set(roots)
    checked: set[str] = set()
    violations: set[str] = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        for imported in dependencies[module]:
            if forbidden(imported):
                violations.add(f"{module} -> {imported}")
            elif imported in dependencies and imported not in checked:
                pending.add(imported)
    return violations


def test_dependency_guard_reports_direct_and_transitive_edges_through_cycles() -> None:
    """Follow dependency cycles and distinguish similarly named namespaces."""
    graph = {
        "root": {"helper", "blocked.Direct", "blocked_helper", "stdlib"},
        "helper": {"root", "blocked.Indirect"},
        "blocked.Direct": {"blocked.BeyondBoundary"},
        "blocked_helper": set(),
    }
    assert _dependency_violations(
        graph, {"root"}, lambda name: _within(name, {"blocked"})
    ) == {"root -> blocked.Direct", "helper -> blocked.Indirect"}


def _repository_modules(trees: dict[Path, ast.Module]) -> set[str]:
    """Return modules defining a module-level ``*Repository`` class."""
    return {
        _module_name(path)
        for path, tree in trees.items()
        if any(
            isinstance(node, ast.ClassDef) and node.name.endswith("Repository")
            for node in tree.body
        )
    }


def _persistence_modules(trees: dict[Path, ast.Module]) -> set[str]:
    """Return ``models`` modules and modules defining a ``*Repository`` class."""
    return _repository_modules(trees) | {
        _module_name(path)
        for path in trees
        if _module_name(path).rsplit(".", 1)[-1] == "models"
    }


def _adapter_modules(trees: dict[Path, ast.Module]) -> set[Path]:
    """Return REST router, API-key auth, MCP and realtime adapter modules."""
    return {
        path
        for path, tree in trees.items()
        if _defines_ninja_router(tree)
        or _module_name(path) == "apps.accounts.api_auth"
        or _module_name(path).startswith("apps.mcp_app")
        or _module_name(path) in REALTIME_ADAPTERS
    }


def _is_persistence_module(path: Path) -> bool:
    """Return True for ``models``/``repositories``-style modules."""
    return path.stem in {"models", "repositories"} or path.stem.endswith(
        ("_models", "_repository")
    )


@pytest.fixture(scope="module")
def source_groups(
    trees: dict[Path, ast.Module], modules: dict[str, Path]
) -> DependencyGraph:
    """Discover shared source sets once instead of in every boundary test."""
    repositories = _repository_modules(trees)
    persistence = _persistence_modules(trees)
    routers = {m for m, p in modules.items() if _defines_ninja_router(trees[p])}
    return {
        "all": set(modules),
        "routers": routers,
        "adapters": {_module_name(p) for p in _adapter_modules(trees)},
        "rest_and_mcp": routers | {m for m in modules if _within(m, {"apps.mcp_app"})},
        "repositories": repositories,
        "persistence": persistence,
        "harness_persistence": {m for m in persistence if _within(m, {"apps.harness"})},
        "repository_only": {
            m
            for m, p in modules.items()
            if ".".join(m.split(".")[:2]) in REPOSITORY_ONLY_APPS
            and not _is_persistence_module(p)
        },
        "account_services": {
            m
            for m, p in modules.items()
            if _within(m, {"apps.accounts"}) and not _is_persistence_module(p)
        },
        "credential_and_plugin_services": {
            m
            for m, p in modules.items()
            if _within(m, {"apps.credentials", "apps.plugins"})
            and not _is_persistence_module(p)
        },
    }


def _sources(
    selectors: SourceSelector,
    groups: DependencyGraph,
    modules: Iterable[str],
) -> set[str]:
    """Resolve a discovered source group or explicit module namespaces."""
    if isinstance(selectors, str):
        result = groups[selectors]
        assert result, f"Empty source group: {selectors}"
        return result
    result = {m for m in modules if _within(m, selectors)}
    assert all(any(_within(m, {s}) for m in result) for s in selectors), selectors
    return result


@pytest.mark.parametrize(
    ("sources", "forbidden"),
    [
        (
            ("apps.harness.mcp_client.scopes",),
            (
                "apps.harness.mcp_client.runtime",
                "apps.harness.mcp_client.connection",
                "apps.plugins",
            ),
        ),
        (
            ("apps.runners.process_repository", "apps.runners.services.process_lookup"),
            (
                "apps.harness",
                "apps.runners.repositories",
                "apps.runners.services",
            ),
        ),
        (
            ("apps.runners.operations",),
            (
                "apps.runners.repositories",
                "apps.runners.deletion_repository",
                "apps.runners.capture_repository",
                "apps.runners.recreate_repository",
                "apps.runners.services",
            ),
        ),
        (("apps.harness.provider_account_management_service",), "adapters"),
        (
            ("apps.harness.run_recovery_service",),
            (
                "apps.harness.harness_service",
                "apps.harness.runner",
                "apps.harness.tools",
                "apps.scheduled_tasks",
            ),
        ),
        (
            (
                "apps.harness.commit_message_api",
                "apps.harness.commit_message_service",
                "apps.harness.commit_messages",
                "apps.harness.small_model_service",
                "apps.harness.session_metadata_service",
            ),
            ("apps.harness.harness_service",),
        ),
        (
            ("apps.harness.interaction_service",),
            (
                "apps.harness.harness_service",
                "apps.harness.runner",
                "apps.harness.engines.execution",
                "apps.harness.engines.service_runtime",
                "apps.harness.tools",
            ),
        ),
        (
            (
                "apps.harness.provider_history_service",
                "apps.harness.conversation_service",
                "apps.harness.conversation_events",
                "apps.harness.session_settings_service",
                "apps.harness.schemas",
            ),
            (
                "apps.harness.harness_service",
                "apps.harness.runner",
                "apps.harness.engines.execution",
                "apps.harness.engines.service_runtime",
            ),
        ),
        (
            ("apps.harness.provider_accounts",),
            (
                "apps.harness.services",
                "apps.harness.provider_turns",
                "apps.harness.harness_service",
                "apps.harness.engines",
                "apps.harness.model_catalog_service",
                "apps.harness.providers.resolver",
            ),
        ),
        (
            (
                "apps.harness.engines.base",
                "apps.harness.tools.invocation",
                "apps.harness.engines.claude",
                "apps.harness.engines.codex",
            ),
            ("apps.harness.runner",),
        ),
        (
            ("apps.runners.inventory_repository",),
            (
                "apps.runners.services",
                "apps.runners.activity_repository",
                "apps.runners.retention_repository",
                "apps.runners.deletion_repository",
            ),
        ),
        (
            ("apps.runners.retention_repository", "apps.runners.image_lines"),
            (
                "apps.runners.services",
                "apps.runners.deletion_repository",
            ),
        ),
        (
            (
                "apps.runners.workspace_operation_repository",
                "apps.runners.repositories",
                "apps.runners.operations",
            ),
            (
                "apps.runners.capture_repository",
                "apps.runners.recreate_repository",
                "apps.runners.phase_children",
            ),
        ),
        (
            ("apps.credentials.repositories",),
            (
                "apps.credentials.services",
                "apps.credentials.mcp_oauth",
                "apps.credentials.oauth_grant_service",
                "apps.organizations.services",
            ),
        ),
        (
            ("apps.credentials.mcp_oauth",),
            (
                "apps.credentials.services",
                "apps.credentials.oauth_grant_service",
            ),
        ),
        (
            ("apps.credentials.oauth_grant_service",),
            (
                "apps.credentials.services",
                "apps.credentials.mcp_oauth",
            ),
        ),
        (
            ("apps.credentials.oauth_metadata",),
            (
                "apps.credentials.services",
                "apps.credentials.mcp_oauth",
                "apps.credentials.oauth_grant_service",
                "apps.credentials.repositories",
            ),
        ),
        (
            ("apps.harness.media", "apps.harness.redaction"),
            (
                "apps.harness.engines",
                "apps.harness.tools",
                "apps.harness.runner",
                "apps.harness.harness_service",
            ),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else ", ".join(value),
)
def test_transitive_import_boundaries(
    sources: SourceSelector,
    forbidden: SourceSelector,
    source_groups: DependencyGraph,
    dependencies: DependencyGraph,
) -> None:
    """Reject forbidden dependencies even through lazy imports and helper modules."""
    roots = _sources(sources, source_groups, dependencies)
    blocked = source_groups[forbidden] if isinstance(forbidden, str) else forbidden
    violations = _dependency_violations(
        dependencies, roots, lambda imported: _within(imported, blocked)
    )
    assert not violations, "\n".join(sorted(violations))


@pytest.mark.parametrize(
    ("sources", "forbidden", "exact"),
    [
        (
            ("apps.runners.services.domains", "apps.runners.services.infra"),
            ("apps.runners.services",),
            True,
        ),
        ("all", "routers", True),
        (
            ("apps.harness.harness_service",),
            (
                "apps.harness.commit_message_api",
                "apps.harness.commit_message_service",
                "apps.harness.commit_messages",
            ),
            False,
        ),
        ("harness_persistence", ("apps.harness.tools",), False),
        (
            ("apps.harness.todos",),
            (
                "apps.harness.tools",
                "apps.harness.engines",
                "apps.harness.harness_service",
            ),
            False,
        ),
        (
            ("apps.harness.tools", "apps.harness.engines"),
            ("apps.harness.todos.repositories",),
            False,
        ),
        ("all", REALTIME_ADAPTERS, False),
        (REALTIME_ADAPTERS, "persistence", False),
        (
            "rest_and_mcp",
            (
                "apps.runners.models.ImageBuildJob",
                "apps.runners.models.ImageDefinition",
                "apps.runners.repositories.ImageBuildJobRepository",
                "apps.runners.repositories.ImageDefinitionRepository",
            ),
            True,
        ),
        ("adapters", "repositories", False),
        (
            ("apps.runners.services.domains.runner_lifecycle",),
            ("apps.runners.services.domains.workspace_lifecycle",),
            False,
        ),
        (
            ("apps.runners.services.infra.task_dispatch",),
            (
                "apps.runners.services.qemu_resources",
                "apps.runners.services.workspace_capacity",
            ),
            False,
        ),
        (
            ("apps.runners.services.qemu_resources",),
            ("django", "apps.runners.services", "apps.runners.repositories"),
            False,
        ),
        (("apps.runners.services",), ("apps.harness",), False),
        (("apps.harness",), ("apps.scheduled_tasks", "apps.mcp_app"), False),
        (("apps.harness.engines",), ("apps.harness.tools.files",), True),
        (
            (
                "apps.harness.provider_accounts",
                "apps.harness.provider_oauth",
                "apps.harness.provider_turns",
                "apps.credentials.oauth_grant_service",
            ),
            ("django.db",),
            False,
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else str(value),
)
def test_direct_import_boundaries(
    sources: SourceSelector,
    forbidden: SourceSelector,
    exact: bool,
    source_groups: DependencyGraph,
    dependencies: DependencyGraph,
) -> None:
    """Keep adapters, workflows and persistence on their owning side of a boundary."""
    roots = _sources(sources, source_groups, dependencies)
    blocked = source_groups[forbidden] if isinstance(forbidden, str) else forbidden
    violations = {
        f"{module} -> {imported}"
        for module in roots
        for imported in dependencies[module]
        if imported != module
        and (imported in blocked if exact else _within(imported, blocked))
    }
    assert not violations, "\n".join(sorted(violations))


@pytest.mark.parametrize(
    ("sources", "forbidden"),
    [
        ("adapters", ADAPTER_QUERY_ATTRIBUTES | CREDENTIAL_RESOLUTION_CALLS),
        ("repository_only", ORM_QUERY_ATTRIBUTES),
        (
            "account_services",
            {
                "save",
                "asave",
                "save_base",
                "check_password",
                "acheck_password",
            },
        ),
        (
            "credential_and_plugin_services",
            {
                "filter",
                "exclude",
                "select_related",
                "prefetch_related",
                "select_for_update",
                "order_by",
                "values_list",
                "annotate",
                "aggregate",
                "bulk_create",
                "bulk_update",
                "raw",
            },
        ),
        (
            (
                "apps.runners.services.process_lookup",
                "apps.runners.services.process_controls",
            ),
            ADAPTER_QUERY_ATTRIBUTES,
        ),
        (
            (
                "apps.runners.services.storage",
                "apps.runners.services.inventory_inspection",
            ),
            ORM_QUERY_ATTRIBUTES
            | {"select_related", "prefetch_related", "values_list", "save"},
        ),
        (
            (
                "apps.harness.provider_accounts",
                "apps.harness.provider_oauth",
                "apps.harness.provider_turns",
            ),
            {"save", "asave", "delete", "adelete", "lock_mutex"},
        ),
        (
            ("apps.credentials.oauth_grant_service",),
            {"save", "asave", "delete", "adelete"},
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else ", ".join(sorted(value)),
)
def test_persistence_access_boundaries(
    sources: SourceSelector,
    forbidden: set[str],
    source_groups: DependencyGraph,
    modules: dict[str, Path],
    trees: dict[Path, ast.Module],
) -> None:
    """Services own policy; only repositories query and mutate persisted records."""
    violations = {
        f"{module}:{node.lineno} -> {node.attr}"
        for module in _sources(sources, source_groups, modules)
        for node in ast.walk(trees[modules[module]])
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    }
    assert not violations, "\n".join(sorted(violations))


@pytest.mark.parametrize(
    "sources",
    [
        ("apps.harness.mcp_client",),
        ("apps.harness.media", "apps.harness.redaction"),
    ],
)
def test_shared_packages_are_acyclic(
    sources: tuple[str, ...],
    source_groups: DependencyGraph,
    dependencies: DependencyGraph,
) -> None:
    """Shared packages cannot acquire a circular internal dependency."""
    roots = _sources(sources, source_groups, dependencies)
    tuple(TopologicalSorter({m: dependencies[m] & roots for m in roots}).static_order())


@pytest.mark.parametrize(
    "service",
    [
        "apps.harness.interaction_service",
        "apps.harness.session_metadata_service",
        "apps.harness.provider_history_service",
    ],
)
def test_orchestration_imports_owning_services(
    service: str, dependencies: DependencyGraph
) -> None:
    """Orchestration uses the extracted interaction, metadata and history services."""
    assert service in dependencies["apps.harness.harness_service"]


@pytest.mark.parametrize(
    ("forbidden_methods", "forbidden_attributes"),
    [
        (
            {
                "list_pending_interactions",
                "list_pending_permissions",
                "list_pending_questions",
                "resolve_permission",
                "resolve_question",
                "_authorize_file_read",
                "_on_permission",
                "_on_question",
                "_reject_pending_user_gates",
                "_resolve_sibling_permissions",
                "_gate_ids",
                "_reject_tree_gates",
            },
            {"_pending_permissions", "_pending_questions"},
        ),
        (
            {
                "get_session",
                "get_session_for_workspace",
                "list_sessions",
                "list_timeline_messages",
                "list_timeline_parts",
                "get_part",
                "list_todos",
                "fork_session",
                "_forked_title",
                "list_conversations",
                "mark_session_read",
                "mark_session_unread",
                "dismiss_notice",
                "unread_for_sessions",
                "is_session_unread",
                "_is_conversation_unread",
                "attention_for_sessions",
                "_emit_conversations_changed",
                "_emit_conversations_changed_sync",
                "set_mode",
                "set_model",
                "set_reasoning_effort",
                "update_title",
                "update_skill_ids",
                "_validate_codex_session_config",
            },
            set(),
        ),
    ],
    ids=["interactions", "saved_state"],
)
def test_orchestration_does_not_restore_extracted_facades(
    forbidden_methods: set[str],
    forbidden_attributes: set[str],
    trees: dict[Path, ast.Module],
) -> None:
    """Keep extracted methods and state out of the orchestration facade."""
    tree = trees[APPS_ROOT / "harness" / "harness_service.py"]
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not names & forbidden_methods
    assert not attributes & forbidden_attributes


def test_notifications_reach_harness_only_through_activity_service_and_events(
    trees, dependencies
) -> None:
    """Notification storage must not own harness queries or activity rules."""
    public_modules = {
        "apps.harness.conversation_activity_service",
        "apps.harness.run_events",
    }
    violations = []
    for path in trees:
        if path.relative_to(APPS_ROOT).parts[0] != "notifications":
            continue
        for imported in dependencies[_module_name(path)]:
            if imported != "apps.harness" and not imported.startswith("apps.harness."):
                continue
            if not _is_persistence_module(path) and _within(imported, public_modules):
                continue
            violations.append(f"{_module_name(path)} -> {imported}")
    assert not violations, "\n".join(sorted(violations))


def test_notification_worker_and_persistence_leave_policy_to_services(
    trees, dependencies
) -> None:
    """Workers schedule; notification services own eligibility and retention."""
    package = APPS_ROOT / "notifications"
    worker = package / "worker.py"
    forbidden = {
        "apps.notifications.repositories",
        "apps.notifications.models",
        "apps.harness",
    }
    violations = [
        f"worker -> {module}"
        for module in dependencies[_module_name(worker)]
        if _within(module, forbidden)
    ]
    violations.extend(
        f"worker:{node.lineno} -> {node.attr}"
        for node in ast.walk(trees[worker])
        if isinstance(node, ast.Attribute) and node.attr in {"repo", "activity"}
    )
    for path in package.rglob("*.py"):
        if "tests" in path.parts or not _is_persistence_module(path):
            continue
        tree = trees[path]
        violations.extend(
            f"{_module_name(path)} -> {module}"
            for module in dependencies[_module_name(path)]
            if module == "apps.notifications.services"
            or module.startswith("apps.")
            and not module.startswith("apps.notifications.")
        )
        violations.extend(
            f"{_module_name(path)}:{node.lineno} -> {node.value}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("user__")
        )
        violations.extend(
            f"{_module_name(path)}:{node.lineno} -> {node.arg}"
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg is not None
            and node.arg.startswith("user__")
        )
    assert not violations, "\n".join(sorted(violations))


def test_process_routes_delegate_user_policy_to_process_controls(trees) -> None:
    """REST maps process schemas; services own visibility and allowed controls."""
    routes = [
        node
        for node in trees[APPS_ROOT / "runners" / "api.py"].body
        if isinstance(node, ast.AsyncFunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and "/processes/" in str(decorator.args[0].value)
            for decorator in node.decorator_list
        )
    ]
    assert {route.name for route in routes} == {
        "list_processes",
        "stop_process",
        "restart_process",
        "delete_process",
    }
    forbidden = {
        "get_process",
        "get_workspace_for_user",
        "kind",
        "status",
        "session_id",
    }
    violations = []
    for route in routes:
        nodes = list(ast.walk(route))
        assert any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ProcessControlService"
            for node in nodes
        ), f"{route.name} must use ProcessControlService"
        for node in nodes:
            if (
                isinstance(node, ast.Attribute)
                and node.attr in forbidden
                or isinstance(node, ast.Name)
                and node.id in {"getattr", "_get_owned_workspace_async"}
            ):
                violations.append(f"{route.name}:{node.lineno}")
    assert not violations, "\n".join(violations)


def test_facade_guard_resolves_absolute_and_local_relative_imports() -> None:
    """Lazy imports and aliases must not conceal the reverse dependency."""
    path = APPS_ROOT / "runners" / "services" / "domains" / "git_operations.py"
    facade = "apps.runners.services"
    for source in (
        "import apps.runners.services as facade",
        "from apps.runners import services as facade",
        "from apps.runners.services import RunnerService",
        "def dispatch():\n    from .. import RunnerService",
    ):
        assert facade in _imported_modules(path, ast.parse(source))
    assert facade not in _imported_modules(
        path, ast.parse("from ..infra.rpc_registry import PendingGitRequest")
    )


def test_provider_account_routes_delegate_management_policy(
    trees, dependencies
) -> None:
    """Account routes must not compose credential mutations or catalog policy."""
    path = APPS_ROOT / "harness" / "provider_accounts_api.py"
    imports = dependencies[_module_name(path)]
    assert "apps.harness.provider_account_management_service" in imports
    violations = sorted(
        module
        for module in imports
        if _within(module, {"apps.harness.model_catalog_service"})
    )
    violations.extend(
        f"{node.lineno}: {node.attr}"
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute)
        and node.attr in {"get_account", "save_account", "list_accounts", "provider"}
    )
    violations.extend(
        f"{node.lineno}: connected account policy"
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == "connected"
    )
    violations.extend(
        f"{node.lineno}: ProviderAccountService.{node.func.attr}"
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Call)
        and isinstance(node.func.value.func, ast.Name)
        and node.func.value.func.id == "ProviderAccountService"
        and node.func.attr != "select_account"
    )
    assert not violations, "\n".join(sorted(violations))


def test_scheduled_restart_recovery_reaches_harness_only_through_its_service(
    dependencies,
) -> None:
    """The schedule workflow must not read or mutate harness storage directly."""
    path = APPS_ROOT / "scheduled_tasks" / "recovery.py"
    service = "apps.harness.run_recovery_service"
    imported = dependencies[_module_name(path)]
    assert service in imported
    violations = sorted(
        module
        for module in imported
        if module.startswith("apps.harness.")
        and module != service
        and not module.startswith(f"{service}.")
    )
    assert not violations, "\n".join(sorted(violations))


def test_scheduled_tasks_reach_the_harness_only_through_its_services(
    dependencies,
) -> None:
    """Engine, model and provider rules stay behind harness services."""
    services = {
        "apps.harness.conversation_service",
        "apps.harness.run_events",
        "apps.harness.run_recovery_service",
        "apps.harness.run_settings_service",
    }
    # The orchestration module also re-exports models; allow only its service.
    orchestration = {
        "apps.harness.harness_service",
        "apps.harness.harness_service.HarnessService",
        "apps.harness.harness_service.get_harness_service",
    }
    assert (
        "apps.harness.run_settings_service"
        in dependencies["apps.scheduled_tasks.services"]
    )
    violations = sorted(
        f"{module} -> {imported}"
        for module, imported_modules in dependencies.items()
        if _within(module, {"apps.scheduled_tasks"})
        for imported in imported_modules
        if _within(imported, {"apps.harness"})
        and imported not in orchestration
        and not _within(imported, services)
    )
    assert not violations, "\n".join(violations)


def test_scheduled_tasks_persist_only_their_ledger(trees, dependencies) -> None:
    """Schedules reach workspaces, chats and members through their services."""
    package = APPS_ROOT / "scheduled_tasks"
    foreign = {
        module
        for module in _persistence_modules(trees) | {"apps.runners.locking"}
        if not module.startswith("apps.scheduled_tasks.")
    }
    assert {"apps.harness.models", "apps.runners.repositories"} <= foreign
    adapters = _adapter_modules(trees)
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        if path.is_relative_to(package) and path not in adapters
        for module in dependencies[_module_name(path)]
        if module in foreign
    )
    assert not violations, "\n".join(sorted(violations))


def test_provider_turn_persistence_never_imports_other_contexts_or_services(
    dependencies,
) -> None:
    """Snapshot persistence leaves membership and admission policy to services."""
    path = APPS_ROOT / "harness" / "provider_turn_repository.py"
    violations = [
        module
        for module in dependencies[_module_name(path)]
        if module.startswith("apps.")
        and not module.startswith("apps.harness.")
        or {"services", "provider_turns", "provider_accounts"} & set(module.split("."))
    ]
    assert not violations, "\n".join(sorted(violations))


def test_provider_account_events_remain_independent_of_application_code(
    dependencies,
) -> None:
    """Account publishers and run subscribers share a leaf notification module."""
    path = APPS_ROOT / "harness" / "provider_account_events.py"
    imports = dependencies[_module_name(path)]
    assert {module.split(".")[0] for module in imports} <= sys.stdlib_module_names


def test_provider_wire_adapters_never_import_harness_orchestration(
    trees, modules, dependencies
) -> None:
    """Provider streams and catalog protocols stay below the control plane."""
    package = "apps.harness.providers"
    # AccountBoundAdapter in resolver.py is service composition, not a wire adapter.
    pending = {
        module
        for module, path in modules.items()
        if module.startswith(f"{package}.")
        and path.name != "resolver.py"
        and any(
            isinstance(node, ast.ClassDef)
            and any(
                isinstance(base, ast.Name) and base.id == "ProviderAdapter"
                for base in node.bases
            )
            for node in trees[path].body
        )
    }
    assert {
        f"{package}.bedrock",
        f"{package}.chatgpt",
        f"{package}.openai_compatible",
        f"{package}.openrouter",
    } <= pending
    pending.add(f"{package}.models_catalog")
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: (
            imported.startswith("apps.")
            and (not (imported == package or imported.startswith(f"{package}.")))
            or (imported == "django" or imported.startswith("django."))
        ),
    )
    assert not violations, "\n".join(sorted(violations))


def test_provider_wire_adapters_never_import_each_other(dependencies) -> None:
    """Transport reuse must not couple a provider to another wire protocol."""
    package = "apps.harness.providers"
    adapters = {
        f"{package}.{name}"
        for name in ("bedrock", "chatgpt", "openai_compatible", "openrouter")
    }
    violations = set()
    for adapter in adapters:
        pending = {adapter}
        violations.update(
            f"{adapter}: {edge}"
            for edge in _dependency_violations(
                dependencies, pending, lambda imported: imported in adapters - {adapter}
            )
        )
    assert not violations, "\n".join(sorted(violations))


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            "from apps.harness.providers.bedrock import BedrockAdapter\n"
            "from apps.harness.providers.chatgpt import ChatGPTAdapter\n"
            "from apps.harness.providers.openai_compatible "
            "import OpenAICompatibleAdapter\n"
            "from apps.harness.providers.openrouter import OpenRouterAdapter\n"
            "from apps.harness.providers.models_catalog "
            "import normalize_reasoning_effort, normalize_openrouter_catalog\n"
            "import sys\n"
            "assert 'django' not in sys.modules\n",
            id="provider_wire_adapters_import_without_django_setup",
        ),
        pytest.param(
            "import sys\n"
            "from typing import get_type_hints\n"
            "from apps.harness.engines.base "
            "import HarnessEngine, RunOptions, RunResult\n"
            "hints = get_type_hints(HarnessEngine.run)\n"
            "assert hints['opts'] is RunOptions\n"
            "assert hints['return'] is RunResult\n"
            "assert not {'apps.harness.runner', 'apps.harness.harness_service', "
            "'django'} & sys.modules.keys()\n",
            id="engine_contracts_import_without_application_setup",
        ),
        pytest.param(
            "import sys\n"
            "from apps.harness.media import attachments, formats, references\n"
            "from apps.harness import redaction\n"
            "assert not {'django', 'apps.harness.engines', 'apps.harness.tools'} "
            "& sys.modules.keys()\n",
            id="media_formats_and_references_load_without_application_setup",
        ),
    ],
)
def test_contracts_import_without_application_setup(source: str) -> None:
    """Check each contract in a fresh interpreter without Django settings."""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_foundational_apps_never_import_the_apps_built_on_them(trees, dependencies):
    """Dependencies point from feature apps down to the foundation only."""
    violations = []
    for path in trees:
        app = ".".join(_module_name(path).split(".")[:2])
        allowed = FOUNDATION_DEPENDENCIES.get(app)
        if allowed is None:
            continue
        violations.extend(
            f"{_module_name(path)} -> {module}"
            for module in dependencies[_module_name(path)]
            if module.startswith("apps.")
            and ".".join(module.split(".")[:2]) not in {app, *allowed}
        )
    assert not violations, "\n".join(sorted(violations))


def test_service_only_contexts_are_reached_through_their_services(dependencies):
    """Consumers call e.g. ``SkillService``; its repositories stay private."""
    assert "apps.skills.services" in dependencies["apps.harness.harness_service"]

    violations = sorted(
        f"{module} -> {imported}"
        for module, imported_modules in dependencies.items()
        for context in SERVICE_ONLY_CONTEXTS
        if module != context and not module.startswith(f"{context}.")
        for imported in imported_modules
        if imported.startswith(f"{context}.")
        and imported != f"{context}.services"
        and not imported.startswith(f"{context}.services.")
    )
    assert not violations, "\n".join(sorted(violations))


def test_context_repositories_are_private_to_their_context(trees, dependencies) -> None:
    """Consumers use the owning context's service, never its repositories."""
    repository_modules = _repository_modules(trees)
    violations = []
    for context, boundary in PRIVATE_REPOSITORY_CONTEXTS.items():
        owner = APPS_ROOT / context.split(".")[1]
        repositories = {
            module for module in repository_modules if module.startswith(f"{context}.")
        }
        assert f"{context}.repositories" in repositories
        violations.extend(
            f"{_module_name(path)} -> {module}: {boundary}"
            for path, tree in trees.items()
            if not path.is_relative_to(owner)
            for module in dependencies[_module_name(path)]
            if _within(module, repositories)
        )
    assert not violations, "\n".join(sorted(violations))
