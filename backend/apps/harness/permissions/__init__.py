"""Permissions package for the agent harness."""

from __future__ import annotations

from .evaluator import (
    ALLOW,
    ASK,
    DEFAULT_GLOBAL_RULES,
    DENY,
    Evaluation,
    PermissionContext,
    PermissionEvaluator,
    compile_rules,
)
from .scopes import (
    is_permission_scope,
    permission_display_action,
    permission_scope,
    permission_scope_matches,
)
from .service import (
    AllowlistRepository,
    PermissionRequestRepository,
    PermissionService,
    ResolveResult,
)

__all__ = [
    "ALLOW",
    "ASK",
    "DEFAULT_GLOBAL_RULES",
    "DENY",
    "AllowlistRepository",
    "Evaluation",
    "PermissionContext",
    "PermissionEvaluator",
    "PermissionRequestRepository",
    "PermissionService",
    "ResolveResult",
    "compile_rules",
    "is_permission_scope",
    "permission_display_action",
    "permission_scope",
    "permission_scope_matches",
    "is_permission_scope",
    "permission_display_action",
    "permission_scope",
    "permission_scope_matches",
]
