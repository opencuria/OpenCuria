"""Offline contracts for the optional guest harness; never contacts libvirt."""

from __future__ import annotations

import ast
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "e2e/integration/guest_smoke.py"


def test_guest_resume_always_supplies_explicit_resources() -> None:
    """Both resume paths must satisfy WorkspaceLifecycle's QEMU contract."""
    tree = ast.parse(SCRIPT.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "resume_workspace"
    ]
    assert len(calls) == 2
    for call in calls:
        values = {
            kw.arg: kw.value.value
            for kw in call.keywords
            if isinstance(kw.value, ast.Constant)
        }
        assert values["qemu_vcpus"] == 1
        assert values["qemu_memory_mb"] == 1024
        assert values["qemu_disk_size_gb"] == 20


@pytest.mark.asyncio
async def test_guest_credential_checks_use_installed_paths(monkeypatch) -> None:
    """Presence sources the installed env; absence checks both files and env block."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("integration_guest_smoke", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rt = AsyncMock()
    rt.exec_command_wait.return_value = (0, "")
    wid = uuid.uuid4()
    await module.assert_credential_files(rt, wid, present=True)
    args = rt.exec_command_wait.call_args.args
    assert args[0] == str(wid)
    assert args[1][:2] == ["bash", "-lc"]
    script = args[1][2]
    assert f". {module.WORKSPACE_CREDENTIAL_ENV_FILE}" in script
    assert module.WORKSPACE_CREDENTIAL_PROFILE_D in script
    assert "test -n" in script
    await module.assert_credential_files(rt, wid, present=False)
    script = rt.exec_command_wait.call_args.args[1][2]
    assert f"test ! -e {module.WORKSPACE_CREDENTIAL_ENV_FILE}" in script
    assert module.WORKSPACE_CREDENTIAL_PROFILE_D in script
    assert module.WORKSPACE_CREDENTIAL_ENVIRONMENT in script
    rt.exec_command_wait.return_value = (
        1,
        "ignored output, never included in assertion",
    )
    with pytest.raises(AssertionError, match="Managed credential"):
        await module.assert_credential_files(rt, wid, present=True)
