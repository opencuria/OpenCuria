"""Permission policy stays in one leaf module without application persistence."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]
PERMISSIONS_ROOT = BACKEND_ROOT / "apps" / "harness" / "permissions"


@pytest.mark.parametrize(
    "module",
    [
        "apps.harness.permissions",
        "apps.harness.permissions.evaluator",
        "apps.harness.permissions.scopes",
    ],
)
def test_permission_policy_imports_without_application_setup(module: str) -> None:
    """Check each entry point before Django or another import can mask coupling."""
    env = os.environ.copy()
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import importlib, sys\n"
            "importlib.import_module(sys.argv[1])\n"
            "forbidden = {\n"
            "    'apps.harness.permissions.models',\n"
            "    'apps.harness.permissions.repositories',\n"
            "    'apps.harness.permissions.service',\n"
            "    'django.db',\n"
            "}\n"
            "assert not forbidden & sys.modules.keys(), "
            "sorted(forbidden & sys.modules.keys())\n",
            module,
        ],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _policy_merge_offenders(tree: ast.Module) -> list[str]:
    """Return local decision-merge helpers and ad-hoc agent evaluators."""
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.lstrip("_") == "combine_decisions":
                offenders.append(f"{node.lineno}: defines {node.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "PermissionEvaluator" and any(
                keyword.arg == "agent_rules" for keyword in node.keywords
            ):
                offenders.append(f"{node.lineno}: builds an agent evaluator")
    return offenders


def test_policy_merge_guard_detects_local_helpers_and_agent_evaluators() -> None:
    """Private helpers and attribute-qualified constructors are both caught."""
    tree = ast.parse(
        "def _combine_decisions(*d): ...\n"
        "evaluator.PermissionEvaluator(agent_rules={})\n"
        "PermissionEvaluator(global_rules={})\n"
    )
    assert _policy_merge_offenders(tree) == [
        "1: defines _combine_decisions",
        "2: builds an agent evaluator",
    ]


def test_agent_permission_merging_lives_in_the_permissions_package() -> None:
    """Engines and services merge agent rules only through ``AgentPolicy``."""
    offenders = [
        f"{path.relative_to(BACKEND_ROOT)}:{offender}"
        for path in sorted((BACKEND_ROOT / "apps").rglob("*.py"))
        if not {"tests", "migrations"} & set(path.relative_to(BACKEND_ROOT).parts)
        and not path.is_relative_to(PERMISSIONS_ROOT)
        for offender in _policy_merge_offenders(ast.parse(path.read_text()))
    ]
    assert not offenders, (
        "Use PermissionEvaluator.for_agent() and combine_decisions() from "
        f"apps.harness.permissions.evaluator: {offenders}"
    )
