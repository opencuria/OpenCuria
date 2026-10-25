"""Architecture guard: REST router and Socket.IO modules are leaf adapters.

REST routers (``api.py``) and the MCP server (``apps.mcp_app``) are sibling
adapters over the same services. Shared request/response shapes live in ``schemas`` /
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
``apps.credentials.deletion_guards`` and share ``common.schemas``.
Account identity never imports another app; organizations compose it with
memberships and own the member profile adapter mounted at ``/auth/me/``. Skill
selection (ID normalization, visibility, body rendering and merging) is owned
by the skills context; the harness and scheduled tasks call ``SkillService``
and never import skill repositories or models. Workspaces
compose plugins, so workspace-scoped plugin endpoints and guards live in
``apps.runners``. REST, MCP and realtime adapters never query the ORM
themselves and never import repositories; REST and MCP authenticate API keys
through ``apps.accounts.services.APIKeyService``, and
the realtime adapters reach models and repositories only through services.
Below the adapters, services, tools and engines of the repository-only apps
never query the ORM either: model managers and instance reloads appear only
in ``models`` and ``repositories`` modules. Password/JWT authentication and
SSO provisioning share the accounts user repository; profile writes and
password checks (which can save upgraded hashes) stay there as well.
Credential repositories are private to their context; consumers resolve
catalog entries and workspace attachments through credential services.
Harness persistence never imports tool adapters; todo records and their storage
port live in ``apps.harness.todos.domain`` and tools call its todo service.
Provider wire adapters share context-error classification within ``providers``;
neither they nor their dependencies import orchestration or persistence.
Stateless commit suggestions and shared small-model composition are independent
of chat session orchestration; their adapter calls ``CommitMessageService``.
Session metadata generation is independent as well; the lifecycle service only
schedules it and supplies change notifications to ``SessionMetadataService``.
Provider account management never imports its orchestration consumers; account
validation stays with accounts and revocation wakeups use a leaf event module.
Provider-history reconstruction is independent of run orchestration; its caller
supplies persistence ports to ``ProviderHistoryService``.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

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

#: Apps whose non-persistence modules reach the ORM only through repositories.
REPOSITORY_ONLY_APPS = {
    "apps.accounts",
    "apps.credentials",
    "apps.harness",
    "apps.mcp_app",
    "apps.organizations",
    "apps.plugins",
    "apps.scheduled_tasks",
    "apps.skills",
}

#: Model manager and instance-reload attributes that issue queries.
ORM_QUERY_ATTRIBUTES = {"objects", "_default_manager", "refresh_from_db"}


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
    """Adapters never import REST routers; shared code lives in schemas/services."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
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
        for module in _imported_modules(path, tree) & router_modules
        if module != _module_name(path)
    )
    assert violations == [], (
        "REST router modules must only be imported by config/urls.py; move "
        "shared schemas/mappers to a schemas or presenter module:\n"
        + "\n".join(violations)
    )


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


def test_commit_suggestions_are_independent_of_session_orchestration() -> None:
    """Commit generation must not depend on chat lifecycle, even transitively."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    workflow = {
        "apps.harness.commit_message_api",
        "apps.harness.commit_message_service",
        "apps.harness.commit_messages",
        "apps.harness.small_model_service",
    }
    lifecycle = "apps.harness.harness_service"
    assert workflow <= modules.keys()
    pending = set(workflow)
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if imported == lifecycle or imported.startswith(f"{lifecycle}."):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    violations.update(
        f"{lifecycle} -> {imported}"
        for imported in _imported_modules(modules[lifecycle], trees[modules[lifecycle]])
        if any(
            imported == owner or imported.startswith(f"{owner}.")
            for owner in workflow - {"apps.harness.small_model_service"}
        )
    )
    assert violations == set(), (
        "Keep commit suggestions in CommitMessageService and share small-model "
        "composition without coupling it to HarnessService:\n"
        + "\n".join(sorted(violations))
    )


def test_session_metadata_is_independent_of_run_orchestration() -> None:
    """Metadata generation must work without importing the run lifecycle."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    lifecycle = "apps.harness.harness_service"
    metadata = "apps.harness.session_metadata_service"
    assert metadata in modules
    assert metadata in _imported_modules(modules[lifecycle], trees[modules[lifecycle]])
    pending = {metadata}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if imported == lifecycle or imported.startswith(f"{lifecycle}."):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Keep title/summary generation in SessionMetadataService; it and its "
        "dependencies must not import HarnessService:\n" + "\n".join(sorted(violations))
    )


def test_provider_history_is_independent_of_run_orchestration() -> None:
    """Transcript reconstruction must not import live execution, even indirectly."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    history = "apps.harness.provider_history_service"
    lifecycle = "apps.harness.harness_service"
    execution = {
        lifecycle,
        "apps.harness.runner",
        "apps.harness.engines.execution",
        "apps.harness.engines.service_runtime",
    }
    assert history in modules
    assert history in _imported_modules(modules[lifecycle], trees[modules[lifecycle]])
    pending = {history}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in execution
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Keep provider-history reconstruction in ProviderHistoryService; it and "
        "its dependencies must not import run orchestration:\n"
        + "\n".join(sorted(violations))
    )


def test_provider_accounts_never_import_their_orchestration_consumers() -> None:
    """Accounts stay below model discovery, turn orchestration and configuration."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    consumers = {
        "apps.harness.services",
        "apps.harness.provider_turns",
        "apps.harness.harness_service",
        "apps.harness.engines",
        "apps.harness.providers.models_catalog",
        "apps.harness.providers.resolver",
    }
    pending = {"apps.harness.provider_accounts"}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == consumer or imported.startswith(f"{consumer}.")
                for consumer in consumers
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Keep account validation in provider_accounts and publish revocation "
        "wakeups through provider_account_events; account management must not "
        "import its consumers, even transitively or inside functions:\n"
        + "\n".join(sorted(violations))
    )


def test_provider_account_events_remain_independent_of_application_code() -> None:
    """Account publishers and run subscribers share a leaf notification module."""
    path = APPS_ROOT / "harness" / "provider_account_events.py"
    imports = _imported_modules(path, ast.parse(path.read_text()))
    assert {module.split(".")[0] for module in imports} <= sys.stdlib_module_names


def test_provider_wire_adapters_never_import_harness_orchestration() -> None:
    """Provider streams and their dependencies are independent of the control plane."""
    package = "apps.harness.providers"
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
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
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if imported.startswith("apps.") and not (
                imported == package or imported.startswith(f"{package}.")
            ):
                violations.add(f"{module} -> {imported}")
            elif imported == "django" or imported.startswith("django."):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Provider wire adapters and their transitive dependencies must not "
        "import orchestration, workspace access or persistence. Put shared "
        "provider error classification in the provider layer:\n"
        + "\n".join(sorted(violations))
    )


def test_provider_wire_adapters_import_without_django_setup() -> None:
    """A fresh interpreter can load every provider stream without app setup."""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from apps.harness.providers.bedrock import BedrockAdapter\n"
            "from apps.harness.providers.chatgpt import ChatGPTAdapter\n"
            "from apps.harness.providers.openai_compatible "
            "import OpenAICompatibleAdapter\n"
            "from apps.harness.providers.openrouter import OpenRouterAdapter\n",
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_harness_persistence_never_imports_tools() -> None:
    """Repositories depend on domain records instead of their tool consumers."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    persistence = _persistence_modules(trees)
    assert "apps.harness.todos.repositories" in persistence
    assert "apps.harness.repositories" in persistence
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        if _module_name(path).startswith("apps.harness.")
        and _module_name(path) in persistence
        for module in _imported_modules(path, tree)
        if module == "apps.harness.tools" or module.startswith("apps.harness.tools.")
    )
    assert violations == [], (
        "Move shared records and storage ports out of tool adapters; harness "
        "repositories must never import tools (including function-local "
        "imports):\n" + "\n".join(violations)
    )


def test_todo_domain_never_imports_its_consumers() -> None:
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
        for module in _imported_modules(path, ast.parse(path.read_text()))
        if any(module == name or module.startswith(f"{name}.") for name in consumers)
    )
    assert violations == [], (
        "Tools and engines consume the todo service; dependencies must not "
        "point back to those consumers:\n" + "\n".join(violations)
    )


def test_todo_tools_and_engines_use_the_service_boundary() -> None:
    """Tool adapters and engines must not select or access todo stores directly."""
    harness = APPS_ROOT / "harness"
    consumers = [
        *(harness / "tools").rglob("*.py"),
        *(harness / "engines").rglob("*.py"),
    ]
    stores = {
        "apps.harness.todos.repositories",
        "apps.harness.todos.memory_repository",
    }
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path in consumers
        for module in _imported_modules(path, ast.parse(path.read_text()))
        if any(module == store or module.startswith(f"{store}.") for store in stores)
    )
    assert violations == [], (
        "Read and replace todos through apps.harness.todos.service.TodoService "
        "instead of importing its stores from a tool or engine:\n"
        + "\n".join(violations)
    )


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
            module == owner or module.startswith(f"{owner}.") for owner in persistence
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
    adapters = _adapter_modules(trees)
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
        "service from adapters:\n" + "\n".join(violations)
    )


def _is_persistence_module(path: Path) -> bool:
    """Return True for ``models``/``repositories``-style modules."""
    return path.stem in {"models", "repositories"} or path.stem.endswith(
        ("_models", "_repository")
    )


def test_services_query_the_orm_only_through_repositories():
    """Services, tools and engines call repositories; only those query."""
    checked = [
        path
        for path in _source_files()
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
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr in ORM_QUERY_ATTRIBUTES
    )
    assert violations == [], (
        "Query the ORM in the owning app's repositories module and call it "
        "from the service (e.g. HarnessPartRepository, not "
        "self.parts.model.objects):\n" + "\n".join(violations)
    )


def test_account_services_persist_changes_only_through_repositories():
    """Account policy selects changes; repositories handle explicit/implicit saves."""
    violations = sorted(
        f"{_module_name(path)}:{node.lineno}"
        for path in _source_files()
        if path.is_relative_to(APPS_ROOT / "accounts")
        and not _is_persistence_module(path)
        for node in ast.walk(ast.parse(path.read_text()))
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
    assert violations == [], (
        "Persist user changes (including password hash upgrades) through "
        "apps.accounts.repositories.UserRepository instead of saving models "
        "from account services:\n" + "\n".join(violations)
    )


def test_adapters_reach_repositories_only_through_services():
    """Adapters call the owning service instead of importing a repository."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
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
        for module in _imported_modules(path, trees[path])
        if any(
            module == repository or module.startswith(f"{repository}.")
            for repository in repositories
        )
    )
    assert violations == [], (
        "Call the owning app's service instead of its repository:\n"
        + "\n".join(violations)
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


def test_service_only_contexts_are_reached_through_their_services():
    """Consumers call e.g. ``SkillService``; its repositories stay private."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    imports = {
        _module_name(path): _imported_modules(path, tree)
        for path, tree in trees.items()
    }
    assert "apps.skills.services" in imports["apps.harness.harness_service"]

    violations = sorted(
        f"{module} -> {imported}"
        for module, imported_modules in imports.items()
        for context in SERVICE_ONLY_CONTEXTS
        if module != context and not module.startswith(f"{context}.")
        for imported in imported_modules
        if imported.startswith(f"{context}.")
        and imported != f"{context}.services"
        and not imported.startswith(f"{context}.services.")
    )
    assert violations == [], (
        "Call the context's service (e.g. apps.skills.services.SkillService) "
        "instead of importing its repositories, models or helpers:\n"
        + "\n".join(violations)
    )


def test_credential_repositories_are_private_to_their_context() -> None:
    """Plugin definitions and runtime reads use the credential service boundary."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    credentials = APPS_ROOT / "credentials"
    repositories = {
        module
        for module in _repository_modules(trees)
        if module.startswith("apps.credentials.")
    }
    assert "apps.credentials.repositories" in repositories
    violations = sorted(
        f"{_module_name(path)} -> {module}"
        for path, tree in trees.items()
        if not path.is_relative_to(credentials)
        for module in _imported_modules(path, tree)
        if any(module == repo or module.startswith(f"{repo}.") for repo in repositories)
    )
    assert violations == [], (
        "Resolve credential catalog entries and workspace attachments through "
        "apps.credentials.services; credential repositories are private:\n"
        + "\n".join(violations)
    )


def test_credentials_and_plugins_delegate_queryset_operations_to_repositories() -> None:
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
        for path in _source_files()
        if path.relative_to(APPS_ROOT).parts[0] in {"credentials", "plugins"}
        and not _is_persistence_module(path)
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Attribute) and node.attr in queryset_operations
    )
    assert violations == [], (
        "Move related-manager queries and queryset operations into repositories; "
        "credential and plugin services consume their results:\n"
        + "\n".join(violations)
    )
