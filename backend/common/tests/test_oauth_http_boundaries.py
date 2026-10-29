"""Keep OAuth network security independent of protocol policy and persistence."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

CREDENTIALS = Path(__file__).resolve().parents[2] / "apps" / "credentials"


def _imports(tree: ast.Module) -> set[str]:
    """Resolve imports, including package and function-local imports."""
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name("." * node.level + module, "apps.credentials")
            modules.add(module)
            modules.update(f"{module}.{alias.name}" for alias in node.names)
    return modules


def test_oauth_http_has_no_application_or_persistence_dependencies() -> None:
    """The transport shares only the leaf error contract with its callers."""
    violations = []
    for name in ("oauth_http.py", "oauth_errors.py"):
        imports = _imports(ast.parse((CREDENTIALS / name).read_text()))
        for module in imports:
            if module.split(".")[0] not in {"apps", "common", "config", "django"}:
                continue
            if module == "apps.credentials.oauth_errors" or module.startswith(
                "apps.credentials.oauth_errors."
            ):
                continue
            violations.append(f"{name} imports {module}")
    assert violations == [], (
        "OAuth HTTP must not depend on workflows, services, settings or ORM:\n"
        + "\n".join(sorted(violations))
    )


def test_oauth_workflow_delegates_outbound_connections_to_http_boundary() -> None:
    """Discovery and grants own OAuth policy without creating network clients."""
    tree = ast.parse((CREDENTIALS / "mcp_oauth.py").read_text())
    violations = sorted(
        module
        for module in _imports(tree)
        if module.split(".")[0] in {"httpcore", "socket", "ssl", "ipaddress"}
        or module in {"httpx.Client", "httpx.HTTPTransport"}
    )
    violations.extend(
        f"line {node.lineno}: {node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"Client", "HTTPTransport", "ConnectionPool", "getaddrinfo"}
    )
    assert violations == [], (
        "Use oauth_http for checked DNS, pinned TLS and bounded HTTP requests:\n"
        + "\n".join(violations)
    )


def test_import_guard_resolves_local_and_package_imports() -> None:
    """A function-local import cannot hide a dependency back to the workflow."""
    assert _imports(
        ast.parse(
            "def bad():\n"
            "    from . import mcp_oauth\n"
            "    from .services import CredentialOAuthSvc\n"
            "    import django.db\n"
        )
    ) >= {
        "apps.credentials.mcp_oauth",
        "apps.credentials.services",
        "django.db",
    }
