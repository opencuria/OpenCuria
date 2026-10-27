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
Credential and organization repositories are private to their contexts;
consumers resolve catalog entries and workspace attachments through credential
services and check organizations and active memberships through
``OrganizationService``. Adapters select credentials by ID and never resolve
(decrypt) them; the service that applies a selection resolves it.
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
Permission and question interactions own pending requests and live waiters in
``InteractionService``. It uses event and run-status ports, never imports run
orchestration or tool adapters, and leaves no forwarding API in HarnessService.
Saved conversation feeds, unread/attention state and notice dismissal belong to
``ConversationService``. It and the shared conversation event publisher never
import live run orchestration, even through their dependencies.
OAuth endpoint, registration and grant integrity rules live in
``apps.credentials.oauth_metadata``; persistence and protocol workflows never
import their service consumers, even through function-local imports.
OAuth grant authorization belongs to OAuthGrantService; repositories never
call application services, and the grant service delegates transactions and
model writes to its repository.
Engine execution contracts live in ``apps.harness.engines.base``; external
engines and shared tool dispatch never import the native inference loop,
even through their dependencies.
Inventory persistence never imports storage inspection, activity, retention or
deletion workflows. InventoryInspectionService composes their read models and
queries only through repositories, including fully loaded inventory records.
Runner service domains and infrastructure never import their composition facade;
shared RPC correlation records belong to the infrastructure that consumes them.
Workspace reservation reads and task admission never import capture/recreate
coordinators. Their shared read boundary is WorkspaceOperationRepository.
Lifecycle outbox persistence never imports task allocation or workflow coordinators.
RecoveryService supplies domain delivery approval, checked under the existing
runner locks before command expiry and lease allocation.
Scheduled-task restart recovery coordinates ledger outcomes through services;
harness turn, descendant and input cleanup belongs to RunRecoveryService, which
never imports live orchestration or its scheduling consumers. Scheduled tasks
persist only their own ledger: workspace launch state, ownership, activity and
the single auto-stop policy come from the runner service, and unstarted chats
are rolled back through ConversationService.
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
    "apps.organizations",
    "apps.plugins",
    "apps.scheduled_tasks",
    "apps.skills",
}

#: Model manager and instance-reload attributes that issue queries.
ORM_QUERY_ATTRIBUTES = {"objects", "_default_manager", "refresh_from_db"}

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


def test_runner_domains_and_infrastructure_never_import_composition_facade() -> None:
    """Domain and infrastructure dependencies must point away from the facade."""
    services = APPS_ROOT / "runners" / "services"
    facade = "apps.runners.services"
    violations = sorted(
        f"{_module_name(path)} -> {facade}"
        for package in (services / "domains", services / "infra")
        for path in package.rglob("*.py")
        if facade in _imported_modules(path, ast.parse(path.read_text()))
    )
    assert violations == [], (
        "RunnerService composes domains and infrastructure; they must not "
        "import it, including through function-local imports. Put shared "
        "contracts in the module that owns them:\n" + "\n".join(violations)
    )


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


def test_lifecycle_outbox_never_imports_its_consumers() -> None:
    """Command claiming must not import the workflows that allocate commands."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    consumers = {
        "apps.runners.repositories",
        "apps.runners.deletion_repository",
        "apps.runners.capture_repository",
        "apps.runners.recreate_repository",
        "apps.runners.services",
    }
    pending = {"apps.runners.operations"}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in consumers
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "RecoveryService must supply domain delivery approval to the lifecycle "
        "outbox; persistence and its dependencies must not import task allocation "
        "or workflow coordination, even through local imports:\n"
        + "\n".join(sorted(violations))
    )


def test_scheduled_restart_recovery_reaches_harness_only_through_its_service() -> None:
    """The schedule workflow must not read or mutate harness storage directly."""
    path = APPS_ROOT / "scheduled_tasks" / "recovery.py"
    service = "apps.harness.run_recovery_service"
    imported = _imported_modules(path, ast.parse(path.read_text()))
    assert service in imported
    violations = sorted(
        module
        for module in imported
        if module.startswith("apps.harness.")
        and module != service
        and not module.startswith(f"{service}.")
    )
    assert violations == [], (
        "Restart recovery must call RunRecoveryService instead of importing "
        "harness models, repositories or live execution:\n" + "\n".join(violations)
    )


def test_scheduled_tasks_persist_only_their_ledger() -> None:
    """Schedules reach workspaces, chats and members through their services."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
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
        for module in _imported_modules(path, tree)
        if module in foreign
    )
    assert violations == [], (
        "Scheduled tasks persist only their schedule ledger. Read workspace "
        "state, activity and auto-stop through apps.runners.services, members "
        "through OrganizationService and roll back unstarted chats through "
        "ConversationService instead of importing their models, repositories "
        "or locks, even function-locally:\n" + "\n".join(violations)
    )


def test_harness_restart_recovery_is_independent_of_live_orchestration() -> None:
    """Restart cleanup must stay independent even through local imports."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    pending = {"apps.harness.run_recovery_service"}
    forbidden = (
        "apps.harness.harness_service",
        "apps.harness.runner",
        "apps.harness.tools",
        "apps.scheduled_tasks",
    )
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in forbidden
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Harness restart recovery and its dependencies must not import live "
        "orchestration, tools or scheduling consumers:\n"
        + "\n".join(sorted(violations))
    )


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


def test_user_interactions_are_independent_of_run_orchestration() -> None:
    """All engines and adapters share interactions without a reverse dependency."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
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
    assert interactions in _imported_modules(
        modules[lifecycle], trees[modules[lifecycle]]
    )
    pending = {interactions}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in forbidden
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "InteractionService must own user gates independently of run orchestration "
        "and tool adapters, including through local imports:\n"
        + "\n".join(sorted(violations))
    )

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


def test_saved_session_state_is_independent_of_run_orchestration() -> None:
    """Saved-state queries, navigation and settings must not load agent execution."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    lifecycle = "apps.harness.harness_service"
    owners = {
        "apps.harness.conversation_service",
        "apps.harness.conversation_events",
        "apps.harness.session_settings_service",
    }
    execution = {
        lifecycle,
        "apps.harness.runner",
        "apps.harness.engines.execution",
        "apps.harness.engines.service_runtime",
    }
    assert owners <= modules.keys()
    pending = set(owners)
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
        "ConversationService, ConversationEvents and SessionSettingsService "
        "must work independently "
        "of live run orchestration:\n" + "\n".join(sorted(violations))
    )

    moved_operations = {
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
    assert not {
        node.name
        for node in ast.walk(trees[modules[lifecycle]])
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    } & moved_operations, "Keep saved conversations and settings out of HarnessService"


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


def test_external_engines_never_import_the_native_loop() -> None:
    """Sibling engines and shared contracts must not depend on native execution."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
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
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if imported == native or imported.startswith(f"{native}."):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Keep RunOptions/RunResult in engines.base; external engines and "
        "shared tool dispatch must not import the native loop, including "
        "transitively and inside functions:\n" + "\n".join(sorted(violations))
    )


def test_engine_contracts_import_without_application_setup() -> None:
    """The engine protocol's annotations resolve without loading application code."""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "from typing import get_type_hints\n"
            "from apps.harness.engines.base "
            "import HarnessEngine, RunOptions, RunResult\n"
            "hints = get_type_hints(HarnessEngine.run)\n"
            "assert hints['opts'] is RunOptions\n"
            "assert hints['return'] is RunResult\n"
            "assert not {'apps.harness.runner', 'apps.harness.harness_service', "
            "'django'} & sys.modules.keys()\n",
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


def test_adapters_never_resolve_credential_secrets():
    """REST and MCP select credentials by ID; services resolve the secrets."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    adapters = _adapter_modules(trees)
    assert APPS_ROOT / "runners" / "api.py" in adapters  # discovery sanity check
    assert APPS_ROOT / "mcp_app" / "tools" / "workspaces.py" in adapters

    violations = sorted(
        f"{_module_name(path)}:{node.lineno} -> {node.attr}"
        for path in adapters
        for node in ast.walk(trees[path])
        if isinstance(node, ast.Attribute) and node.attr in CREDENTIAL_RESOLUTION_CALLS
    )
    assert violations == [], (
        "Adapters must pass credential IDs to the service that uses them "
        "(e.g. RunnerService.create_workspace(credential_ids=...)) instead of "
        "resolving decrypted credentials themselves:\n" + "\n".join(violations)
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


def test_inventory_persistence_never_imports_inspection_workflows() -> None:
    """Storage read composition must not pull workflows into scan persistence."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    forbidden = {
        "apps.runners.services",
        "apps.runners.activity_repository",
        "apps.runners.retention_repository",
        "apps.runners.deletion_repository",
    }
    pending = {"apps.runners.inventory_repository"}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in forbidden
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Keep storage inspection composition in InventoryInspectionService; "
        "inventory persistence must not import its consumers, even transitively "
        "or inside functions:\n" + "\n".join(sorted(violations))
    )


def test_retention_persistence_never_imports_workflows() -> None:
    """Retention inputs stay independent of scheduling and deletion consumers."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
    forbidden = {"apps.runners.services", "apps.runners.deletion_repository"}
    pending = {"apps.runners.retention_repository", "apps.runners.image_lines"}
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in forbidden
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Keep retention decisions and recovery in ImageRetentionService; "
        "input repositories must not import their consumers, even transitively "
        "or inside functions:\n" + "\n".join(sorted(violations))
    )


def test_storage_inspection_queries_only_through_repositories() -> None:
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
        for node in ast.walk(
            ast.parse((APPS_ROOT / "runners" / "services" / f"{name}.py").read_text())
        )
        if isinstance(node, ast.Attribute) and node.attr in query_attributes
    )
    assert violations == [], (
        "Materialize storage inspection inputs in repositories, then compose "
        "their user-visible state in services:\n" + "\n".join(violations)
    )


def test_workspace_operation_reads_never_import_phase_coordinators() -> None:
    """Reading reservations and admitting tasks must not load phase progression."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
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
    checked: set[str] = set()
    violations = set()
    while pending:
        module = pending.pop()
        checked.add(module)
        path = modules[module]
        for imported in _imported_modules(path, trees[path]):
            if any(
                imported == owner or imported.startswith(f"{owner}.")
                for owner in coordinators
            ):
                violations.add(f"{module} -> {imported}")
            elif imported in modules and imported not in checked:
                pending.add(imported)
    assert violations == set(), (
        "Read durable reservations through WorkspaceOperationRepository; "
        "task admission and status projection must not import capture/recreate "
        "phase progression, even transitively or inside functions:\n"
        + "\n".join(sorted(violations))
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


def test_context_repositories_are_private_to_their_context() -> None:
    """Consumers use the owning context's service, never its repositories."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
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
            for module in _imported_modules(path, tree)
            if any(
                module == repo or module.startswith(f"{repo}.") for repo in repositories
            )
        )
    assert violations == [], (
        "Repositories are private to their bounded context; call its service "
        "instead:\n" + "\n".join(sorted(violations))
    )


def test_oauth_persistence_and_protocol_never_import_their_consumers() -> None:
    """OAuth dependencies point toward shared integrity rules and persistence."""
    trees = {path: ast.parse(path.read_text()) for path in _source_files()}
    modules = {_module_name(path): path for path in trees}
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
        checked: set[str] = set()
        while pending:
            module = pending.pop()
            checked.add(module)
            path = modules[module]
            for imported in _imported_modules(path, trees[path]):
                if any(
                    imported == target or imported.startswith(f"{target}.")
                    for target in forbidden
                ):
                    violations.add(f"{owner}: {module} -> {imported}")
                elif imported in modules and imported not in checked:
                    pending.add(imported)
    assert violations == set(), (
        "Share OAuth validation through apps.credentials.oauth_metadata; "
        "keep grant authorization in OAuthGrantService and keep persistence "
        "independent of application services and protocol workflows:\n"
        + "\n".join(sorted(violations))
    )


def test_oauth_grant_service_delegates_transactions_and_model_writes() -> None:
    """Authorization policy cannot bypass its transaction-scoped repository."""
    path = APPS_ROOT / "credentials" / "oauth_grant_service.py"
    tree = ast.parse(path.read_text())
    violations = sorted(
        module
        for module in _imported_modules(path, tree)
        if module == "django.db" or module.startswith("django.db.")
    )
    violations.extend(
        f"{node.lineno}: {node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"save", "asave", "delete", "adelete"}
    )
    assert violations == [], (
        "Keep grant transactions and model writes in McpOAuthRepository:\n"
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
