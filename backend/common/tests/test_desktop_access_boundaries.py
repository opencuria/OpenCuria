"""Keep desktop authorization in services for REST and realtime callers."""

from __future__ import annotations

import ast
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_desktop_routes_delegate_workspace_authorization() -> None:
    """Desktop routes may check API key scopes, never workspace owner/admin rules."""
    path = BACKEND_ROOT / "apps/runners/api.py"
    tree = ast.parse(path.read_text())
    routes = [
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and any(
            isinstance(decorator, ast.Call)
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
            and "/desktop/" in str(decorator.args[0].value)
            for decorator in node.decorator_list
        )
    ]
    assert {
        "desktop_clipboard_read",
        "desktop_clipboard_write",
        "take_desktop_control",
    } <= {route.name for route in routes}
    forbidden = {
        "created_by_id",
        "organization_id",
        "role",
        "get_user_role",
        "get_workspace",
    }
    violations = [
        f"{route.name}:{node.lineno}: {node.attr}"
        for route in routes
        for node in ast.walk(route)
        if isinstance(node, ast.Attribute) and node.attr in forbidden
    ]
    assert not violations, "\n".join(violations)


def test_interactive_sessions_leave_access_policy_to_workspace_access() -> None:
    """Lifecycle and clipboard flows must not decide owner/admin access inline."""
    path = BACKEND_ROOT / "apps/runners/services/domains/interactive_sessions.py"
    tree = ast.parse(path.read_text())
    violations = [
        f"{node.lineno}: {node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr in {"created_by_id", "get_user_role", "require_membership"}
    ]
    assert not violations, "\n".join(violations)
