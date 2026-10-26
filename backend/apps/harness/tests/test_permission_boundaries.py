"""Permission policy imports must not initialize application persistence."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[3]


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
