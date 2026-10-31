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
}

#: Apps whose non-persistence modules reach the ORM only through repositories.
REPOSITORY_ONLY_APPS = {
    "apps.accounts",
    "apps.credentials",
    "apps.harness",
    "apps.mcp_app",
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


def test_runner_domains_and_infrastructure_never_import_composition_facade(
    dependencies,
) -> None:
    """Domain and infrastructure dependencies must point away from the facade."""
    services = APPS_ROOT / "runners" / "services"
    facade = "apps.runners.services"
    violations = sorted(
        f"{_module_name(path)} -> {facade}"
        for package in (services / "domains", services / "infra")
        for path in package.rglob("*.py")
        if facade in dependencies[_module_name(path)]
    )
    assert not violations, "\n".join(sorted(violations))


def test_process_lookup_and_persistence_are_independent_of_runner_workflows(
    trees, dependencies
) -> None:
    """Process reads must not load unrelated persistence or live execution."""
    forbidden = {
        "apps.harness",
        "apps.runners.repositories",
        "apps.runners.services",
    }
    pending = {
        "apps.runners.process_repository",
        "apps.runners.services.process_lookup",
    }
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, forbidden),
    )
    lookup = APPS_ROOT / "runners" / "services" / "process_lookup.py"
    violations.update(
        f"{_module_name(lookup)}:{node.lineno} -> {node.attr}"
        for node in ast.walk(trees[lookup])
        if isinstance(node, ast.Attribute) and node.attr in ADAPTER_QUERY_ATTRIBUTES
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
        "list_processes", "stop_process", "restart_process", "delete_process"
    }
    forbidden = {
        "get_process", "get_workspace_for_user", "kind", "status", "session_id"
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
                isinstance(node, ast.Attribute) and node.attr in forbidden
                or isinstance(node, ast.Name)
                and node.id in {"getattr", "_get_owned_workspace_async"}
            ):
                violations.append(f"{route.name}:{node.lineno}")
    assert not violations, "\n".join(violations)

    service = APPS_ROOT / "runners" / "services" / "process_controls.py"
    assert not [
        node.attr
        for node in ast.walk(trees[service])
        if isinstance(node, ast.Attribute) and node.attr in ADAPTER_QUERY_ATTRIBUTES
    ], "Process controls must delegate persistence to the execution service"


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


def test_lifecycle_outbox_never_imports_its_consumers(dependencies) -> None:
    """Command claiming must not import the workflows that allocate commands."""
    consumers = {
        "apps.runners.repositories",
        "apps.runners.deletion_repository",
        "apps.runners.capture_repository",
        "apps.runners.recreate_repository",
        "apps.runners.services",
    }
    pending = {"apps.runners.operations"}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, consumers),
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


def test_harness_restart_recovery_is_independent_of_live_orchestration(
    dependencies,
) -> None:
    """Restart cleanup must stay independent even through local imports."""
    pending = {"apps.harness.run_recovery_service"}
    forbidden = (
        "apps.harness.harness_service",
        "apps.harness.runner",
        "apps.harness.tools",
        "apps.scheduled_tasks",
    )
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, forbidden),
    )
    assert not violations, "\n".join(sorted(violations))


def test_no_application_module_imports_a_rest_router_module(trees, dependencies):
    """Adapters never import REST routers; shared code lives in schemas/services."""
    router_modules = {
        _module_name(path)
        for path, tree in trees.items()
        if _defines_ninja_router(tree)
    }
    assert "apps.harness.api" in router_modules  # discovery sanity check
    assert "apps.harness.commit_message_api" in router_modules

    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        for module in dependencies[_module_name(path)] & router_modules
        if module != _module_name(path)
    )
    assert not violations, "\n".join(sorted(violations))


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


def test_commit_suggestions_are_independent_of_session_orchestration(
    modules, dependencies
) -> None:
    """Commit generation must not depend on chat lifecycle, even transitively."""
    workflow = {
        "apps.harness.commit_message_api",
        "apps.harness.commit_message_service",
        "apps.harness.commit_messages",
        "apps.harness.small_model_service",
    }
    lifecycle = "apps.harness.harness_service"
    assert workflow <= modules.keys()
    pending = set(workflow)
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: imported == lifecycle or imported.startswith(f"{lifecycle}."),
    )
    violations.update(
        f"{lifecycle} -> {imported}"
        for imported in dependencies[lifecycle]
        if _within(imported, workflow - {"apps.harness.small_model_service"})
    )
    assert not violations, "\n".join(sorted(violations))


def test_user_interactions_are_independent_of_run_orchestration(
    trees, modules, dependencies
) -> None:
    """All engines and adapters share interactions without a reverse dependency."""
    interactions = "apps.harness.interaction_service"
    lifecycle = "apps.harness.harness_service"
    forbidden = {
        lifecycle,
        "apps.harness.runner",
        "apps.harness.engines.execution",
        "apps.harness.engines.service_runtime",
        "apps.harness.tools",
    }
    assert interactions in modules
    assert interactions in dependencies[lifecycle]
    pending = {interactions}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, forbidden),
    )
    assert not violations, "\n".join(sorted(violations))

    moved_operations = {
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
    }
    lifecycle_methods = {
        node.name
        for node in ast.walk(trees[modules[lifecycle]])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert moved_operations.isdisjoint(lifecycle_methods), (
        "Migrate interaction callers directly; do not restore orchestration wrappers"
    )
    assert not {
        node.attr
        for node in ast.walk(trees[modules[lifecycle]])
        if isinstance(node, ast.Attribute)
    } & {"_pending_permissions", "_pending_questions"}


def test_session_metadata_is_independent_of_run_orchestration(
    modules, dependencies
) -> None:
    """Metadata generation must work without importing the run lifecycle."""
    lifecycle = "apps.harness.harness_service"
    metadata = "apps.harness.session_metadata_service"
    assert metadata in modules
    assert metadata in dependencies[lifecycle]
    pending = {metadata}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: imported == lifecycle or imported.startswith(f"{lifecycle}."),
    )
    assert not violations, "\n".join(sorted(violations))


def test_provider_history_is_independent_of_run_orchestration(
    modules, dependencies
) -> None:
    """Transcript reconstruction must not import live execution, even indirectly."""
    history = "apps.harness.provider_history_service"
    lifecycle = "apps.harness.harness_service"
    execution = {
        lifecycle,
        "apps.harness.runner",
        "apps.harness.engines.execution",
        "apps.harness.engines.service_runtime",
    }
    assert history in modules
    assert history in dependencies[lifecycle]
    pending = {history}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, execution),
    )
    assert not violations, "\n".join(sorted(violations))


def test_saved_session_state_is_independent_of_run_orchestration(
    trees, modules, dependencies
) -> None:
    """Saved-state queries, forks and settings must not load agent execution."""
    lifecycle = "apps.harness.harness_service"
    owners = {
        "apps.harness.conversation_service",
        "apps.harness.conversation_events",
        "apps.harness.session_settings_service",
        "apps.harness.schemas",
    }
    execution = {
        lifecycle,
        "apps.harness.runner",
        "apps.harness.engines.execution",
        "apps.harness.engines.service_runtime",
    }
    assert owners <= modules.keys()
    pending = set(owners)
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, execution),
    )
    assert not violations, "\n".join(sorted(violations))

    moved_operations = {
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
    }
    assert (
        not {
            node.name
            for node in ast.walk(trees[modules[lifecycle]])
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        & moved_operations
    ), "Keep saved conversations and settings out of HarnessService"


def test_provider_accounts_never_import_their_orchestration_consumers(
    dependencies,
) -> None:
    """Accounts stay below model discovery, turn orchestration and configuration."""
    consumers = {
        "apps.harness.services",
        "apps.harness.provider_turns",
        "apps.harness.harness_service",
        "apps.harness.engines",
        "apps.harness.model_catalog_service",
        "apps.harness.providers.resolver",
    }
    pending = {"apps.harness.provider_accounts"}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, consumers),
    )
    assert not violations, "\n".join(sorted(violations))


def test_provider_account_service_delegates_transactions_and_locks(
    trees, dependencies
) -> None:
    """Account and OAuth policy use the repository-owned admission fence."""
    violations = []
    for name in ("provider_accounts.py", "provider_oauth.py", "provider_turns.py"):
        path = APPS_ROOT / "harness" / name
        tree = trees[path]
        violations.extend(
            f"{name}: {module}"
            for module in sorted(dependencies[_module_name(path)])
            if module == "django.db" or module.startswith("django.db.")
        )
        violations.extend(
            f"{name}:{node.lineno}: {node.attr}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr in {"save", "asave", "delete", "adelete", "lock_mutex"}
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


def test_external_engines_never_import_the_native_loop(modules, dependencies) -> None:
    """Sibling engines and shared contracts must not depend on native execution."""
    native = "apps.harness.runner"
    roots = {
        "apps.harness.engines.base",
        "apps.harness.tools.invocation",
    } | {
        module
        for module in modules
        if module.startswith(
            ("apps.harness.engines.claude.", "apps.harness.engines.codex.")
        )
    }
    assert {
        "apps.harness.engines.claude.engine",
        "apps.harness.engines.codex.engine",
    } <= roots
    pending = set(roots)
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: imported == native or imported.startswith(f"{native}."),
    )
    assert not violations, "\n".join(sorted(violations))


def test_harness_persistence_never_imports_tools(trees, dependencies) -> None:
    """Repositories depend on domain records instead of their tool consumers."""
    persistence = _persistence_modules(trees)
    assert "apps.harness.todos.repositories" in persistence
    assert "apps.harness.repositories" in persistence
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        if _module_name(path).startswith("apps.harness.")
        and _module_name(path) in persistence
        for module in dependencies[_module_name(path)]
        if module == "apps.harness.tools" or module.startswith("apps.harness.tools.")
    )
    assert not violations, "\n".join(sorted(violations))


def test_todo_domain_never_imports_its_consumers(dependencies) -> None:
    """Todo policy and storage cannot depend on tools or engine orchestration."""
    package = APPS_ROOT / "harness" / "todos"
    consumers = {
        "apps.harness.tools",
        "apps.harness.engines",
        "apps.harness.harness_service",
    }
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in package.rglob("*.py")
        for module in dependencies[_module_name(path)]
        if _within(module, consumers)
    )
    assert not violations, "\n".join(sorted(violations))


def test_todo_tools_and_engines_use_the_service_boundary(dependencies) -> None:
    """Tool adapters and engines must not select or access todo stores directly."""
    harness = APPS_ROOT / "harness"
    consumers = [
        *(harness / "tools").rglob("*.py"),
        *(harness / "engines").rglob("*.py"),
    ]
    stores = {
        "apps.harness.todos.repositories",
    }
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in consumers
        for module in dependencies[_module_name(path)]
        if _within(module, stores)
    )
    assert not violations, "\n".join(sorted(violations))


def test_no_application_module_imports_a_realtime_adapter(trees, dependencies):
    """Only the ASGI entry point mounts the Socket.IO and desktop proxy adapters."""
    violations = sorted(
        f"{_module_name(path)} -> {adapter}"
        for path in trees
        for module in dependencies[_module_name(path)]
        for adapter in REALTIME_ADAPTERS
        if _module_name(path) != adapter
        and (module == adapter or module.startswith(f"{adapter}."))
    )
    assert not violations, "\n".join(sorted(violations))


def test_realtime_adapters_reach_persistence_only_through_services(trees, dependencies):
    """Access rules, presence and telemetry live in services, not in adapters."""
    persistence = _persistence_modules(trees)
    assert {"apps.runners.models", "apps.runners.operations"} <= persistence

    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        if _module_name(path) in REALTIME_ADAPTERS
        for module in dependencies[_module_name(path)]
        if _within(module, persistence)
    )
    assert not violations, "\n".join(sorted(violations))


def test_adapters_reach_image_recipes_only_through_their_service(trees, dependencies):
    """REST and MCP share ``ImageDefinitionService`` for recipes and builds."""
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
        for name in dependencies[_module_name(path)] & persistence
    )
    assert not violations, "\n".join(sorted(violations))


def test_adapters_never_query_the_orm(trees):
    """REST, MCP and realtime adapters call services; only repositories query."""
    adapters = _adapter_modules(trees)
    assert APPS_ROOT / "accounts" / "api.py" in adapters  # discovery sanity check
    assert APPS_ROOT / "runners" / "desktop_proxy.py" in adapters

    violations = sorted(
        f"{_module_name(path)}:{node.lineno}"
        for path in adapters
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute) and node.attr in ADAPTER_QUERY_ATTRIBUTES
    )
    assert not violations, "\n".join(sorted(violations))


def test_adapters_never_resolve_credential_secrets(trees):
    """REST and MCP select credentials by ID; services resolve the secrets."""
    adapters = _adapter_modules(trees)
    assert APPS_ROOT / "runners" / "api.py" in adapters  # discovery sanity check
    assert APPS_ROOT / "mcp_app" / "tools" / "workspaces.py" in adapters

    violations = sorted(
        f"{_module_name(path)}:{node.lineno} -> {node.attr}"
        for path in adapters
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute) and node.attr in CREDENTIAL_RESOLUTION_CALLS
    )
    assert not violations, "\n".join(sorted(violations))


def _is_persistence_module(path: Path) -> bool:
    """Return True for ``models``/``repositories``-style modules."""
    return path.stem in {"models", "repositories"} or path.stem.endswith(
        ("_models", "_repository")
    )


def test_services_query_the_orm_only_through_repositories(trees):
    """Services, tools and engines call repositories; only those query."""
    checked = [
        path
        for path in trees
        if ".".join(_module_name(path).split(".")[:2]) in REPOSITORY_ONLY_APPS
        and not _is_persistence_module(path)
    ]
    assert APPS_ROOT / "harness" / "harness_service.py" in checked  # sanity check
    assert APPS_ROOT / "harness" / "permissions" / "service.py" in checked
    assert APPS_ROOT / "accounts" / "auth_backends.py" in checked
    assert APPS_ROOT / "accounts" / "sso.py" in checked

    violations = sorted(
        f"{_module_name(path)}:{node.lineno}"
        for path in checked
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute) and node.attr in ORM_QUERY_ATTRIBUTES
    )
    assert not violations, "\n".join(sorted(violations))


def test_inventory_persistence_never_imports_inspection_workflows(dependencies) -> None:
    """Storage read composition must not pull workflows into scan persistence."""
    forbidden = {
        "apps.runners.services",
        "apps.runners.activity_repository",
        "apps.runners.retention_repository",
        "apps.runners.deletion_repository",
    }
    pending = {"apps.runners.inventory_repository"}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, forbidden),
    )
    assert not violations, "\n".join(sorted(violations))


def test_retention_persistence_never_imports_workflows(dependencies) -> None:
    """Retention inputs stay independent of scheduling and deletion consumers."""
    forbidden = {"apps.runners.services", "apps.runners.deletion_repository"}
    pending = {"apps.runners.retention_repository", "apps.runners.image_lines"}
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, forbidden),
    )
    assert not violations, "\n".join(sorted(violations))


def test_storage_inspection_queries_only_through_repositories(trees) -> None:
    """Storage services may read loaded records but never issue ORM queries."""
    services = ("storage", "inventory_inspection")
    query_attributes = ORM_QUERY_ATTRIBUTES | {
        "select_related",
        "prefetch_related",
        "values_list",
        "save",
    }
    violations = sorted(
        f"{name}:{node.lineno}: {node.attr}"
        for name in services
        for node in ast.walk(trees[APPS_ROOT / "runners" / "services" / f"{name}.py"])
        if isinstance(node, ast.Attribute) and node.attr in query_attributes
    )
    assert not violations, "\n".join(sorted(violations))


def test_workspace_operation_reads_never_import_phase_coordinators(
    modules, dependencies
) -> None:
    """Reading reservations and admitting tasks must not load phase progression."""
    roots = {
        "apps.runners.workspace_operation_repository",
        "apps.runners.repositories",
        "apps.runners.operations",
    }
    coordinators = {
        "apps.runners.capture_repository",
        "apps.runners.recreate_repository",
        "apps.runners.phase_children",
    }
    assert roots <= modules.keys()
    pending = set(roots)
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, coordinators),
    )
    assert not violations, "\n".join(sorted(violations))


def test_account_services_persist_changes_only_through_repositories(trees):
    """Account policy selects changes; repositories handle explicit/implicit saves."""
    violations = sorted(
        f"{_module_name(path)}:{node.lineno}"
        for path in trees
        if path.is_relative_to(APPS_ROOT / "accounts")
        and not _is_persistence_module(path)
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute)
        and node.attr
        in {
            "save",
            "asave",
            "save_base",
            "check_password",
            "acheck_password",
        }
    )
    assert not violations, "\n".join(sorted(violations))


def test_adapters_reach_repositories_only_through_services(trees, dependencies):
    """Adapters call the owning service instead of importing a repository."""
    adapters = _adapter_modules(trees)
    repositories = _repository_modules(trees)
    assert APPS_ROOT / "accounts" / "api_auth.py" in adapters
    assert APPS_ROOT / "mcp_app" / "server.py" in adapters
    assert {
        "apps.credentials.repositories",
        "apps.runners.disposition_repository",
    } <= repositories

    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in adapters
        for module in dependencies[_module_name(path)]
        if _within(module, repositories)
    )
    assert not violations, "\n".join(sorted(violations))


def test_runner_service_never_imports_the_harness(dependencies):
    """Harness depends on the runner service, never the other way round."""
    package = APPS_ROOT / "runners" / "services"
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in package.rglob("*.py")
        for module in dependencies[_module_name(path)]
        if module == "apps.harness" or module.startswith("apps.harness.")
    )
    assert not violations, "\n".join(sorted(violations))


def test_harness_never_imports_the_apps_built_on_it(trees, dependencies):
    """Consumers subscribe to harness run events; the harness never calls them."""
    consumers = ("apps.scheduled_tasks", "apps.mcp_app")
    package = APPS_ROOT / "harness"
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in trees
        if path.is_relative_to(package)
        for module in dependencies[_module_name(path)]
        if _within(module, consumers)
    )
    assert not violations, "\n".join(sorted(violations))


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


def test_oauth_persistence_and_protocol_never_import_their_consumers(
    modules, dependencies
) -> None:
    """OAuth dependencies point toward shared integrity rules and persistence."""
    services = "apps.credentials.services"
    workflow = "apps.credentials.mcp_oauth"
    grants = "apps.credentials.oauth_grant_service"
    boundaries = {
        "apps.credentials.repositories": {
            services,
            workflow,
            grants,
            "apps.organizations.services",
        },
        workflow: {services, grants},
        grants: {services, workflow},
        "apps.credentials.oauth_metadata": {
            services,
            workflow,
            grants,
            "apps.credentials.repositories",
        },
    }
    assert boundaries.keys() <= modules.keys()
    violations = set()
    for owner, forbidden in boundaries.items():
        pending = {owner}
        violations.update(
            f"{owner}: {edge}"
            for edge in _dependency_violations(
                dependencies,
                pending,
                lambda imported: _within(imported, forbidden),
            )
        )
    assert not violations, "\n".join(sorted(violations))


def test_oauth_grant_service_delegates_transactions_and_model_writes(
    trees, dependencies
) -> None:
    """Authorization policy cannot bypass its transaction-scoped repository."""
    path = APPS_ROOT / "credentials" / "oauth_grant_service.py"
    tree = trees[path]
    violations = sorted(
        module
        for module in dependencies[_module_name(path)]
        if module == "django.db" or module.startswith("django.db.")
    )
    violations.extend(
        f"{node.lineno}: {node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"save", "asave", "delete", "adelete"}
    )
    assert not violations, "\n".join(sorted(violations))


def test_credentials_and_plugins_delegate_queryset_operations_to_repositories(
    trees,
) -> None:
    """Related managers must not bypass the repository-only ORM rule."""
    queryset_operations = {
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
    }
    violations = sorted(
        f"{_module_name(path)}:{node.lineno} accesses {node.attr}"
        for path in trees
        if path.relative_to(APPS_ROOT).parts[0] in {"credentials", "plugins"}
        and not _is_persistence_module(path)
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute) and node.attr in queryset_operations
    )
    assert not violations, "\n".join(sorted(violations))


def test_shared_media_never_imports_its_engine_or_tool_consumers(
    modules, dependencies
) -> None:
    """Media formats, hydration and redaction belong below all harness engines."""
    roots = {
        module
        for module in modules
        if module == "apps.harness.media"
        or module.startswith("apps.harness.media.")
        or module == "apps.harness.redaction"
    }
    assert "apps.harness.media.context" in roots
    # The shared package must also remain internally acyclic.
    tuple(TopologicalSorter({m: dependencies[m] & roots for m in roots}).static_order())
    consumers = {
        "apps.harness.engines",
        "apps.harness.tools",
        "apps.harness.runner",
        "apps.harness.harness_service",
    }
    pending = set(roots)
    violations = _dependency_violations(
        dependencies,
        pending,
        lambda imported: _within(imported, consumers),
    )
    assert not violations, "\n".join(sorted(violations))


def test_engines_never_source_media_policy_from_the_read_tool(dependencies) -> None:
    """MIME detection and ingestion limits have one shared, tool-free owner."""
    engine_root = APPS_ROOT / "harness" / "engines"
    violations = sorted(
        str(path.relative_to(BACKEND_ROOT))
        for path in engine_root.rglob("*.py")
        if "apps.harness.tools.files" in dependencies[_module_name(path)]
    )
    assert not violations, "\n".join(sorted(violations))
