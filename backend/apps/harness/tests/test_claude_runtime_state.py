"""Tests for Claude managed-file safety and skill plugin reconciliation."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from typing import Any

import pytest

from apps.harness.engines.claude.runtime_state import (
    MAX_SKILL_BODY_BYTES,
    MAX_SKILL_COUNT,
    ClaudeRuntimeState,
    validate_claude_skills,
)


class _LocalProcessStream:
    """Execute exactly the secret-free managed scripts on the host filesystem."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    async def send(self, data: bytes) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def send_eof(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
            await self.process.stdin.wait_closed()

    async def receive(self) -> bytes:
        assert self.process.stdout is not None
        return await self.process.stdout.read(64 * 1024)

    async def wait_closed(self) -> int:
        return await self.process.wait()

    async def aclose(self) -> None:
        if self.process.returncode is None:
            self.process.kill()
            await self.process.wait()


class _LocalAccessor:
    """Minimal accessor driving the real no-follow scripts against tmp_path."""

    workspace_id = "managed-state-test"

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str] | None] = []

    async def open_process(
        self,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _LocalProcessStream:
        del workdir, timeout
        self.commands.append(list(command))
        self.environments.append(env)
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return _LocalProcessStream(process)


def _runtime(root: Path) -> ClaudeRuntimeState:
    return ClaudeRuntimeState(
        accessor=_LocalAccessor(),  # type: ignore[arg-type]
        auth_env={"ANTHROPIC_API_KEY": "not-a-real-secret"},
        session_store=None,
        root=str(root),
        sdk_version="test",
    )


@pytest.mark.asyncio
async def test_managed_write_is_atomic_private_and_does_not_follow_file_symlink(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    accessor = _LocalAccessor()
    state = ClaudeRuntimeState(
        accessor=accessor,  # type: ignore[arg-type]
        auth_env={"ANTHROPIC_API_KEY": "not-a-real-secret"},
        session_store=None,
        root=str(root),
        sdk_version="test",
    )
    target = root / "session" / "prompt.md"
    outside = tmp_path / "outside"
    outside.write_text("keep me", encoding="utf-8")
    await state.ensure_managed_dir(str(target.parent))
    target.symlink_to(outside)

    await state.write_managed_file(str(target), b"new prompt")

    assert target.read_bytes() == b"new prompt"
    assert not target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep me"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".opencuria-*.tmp"))
    write_command = next(
        command
        for command in accessor.commands
        if "limit = 10 * 1024 * 1024" in command[2]
    )
    assert write_command[-3:] == [str(root), str(target), "600"]
    assert all("new prompt" not in argument for argument in write_command)
    assert accessor.environments and all(env is None for env in accessor.environments)


@pytest.mark.asyncio
async def test_managed_write_rejects_symlinked_ancestor_and_does_not_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed"
    state = _runtime(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.md"
    victim.write_text("unchanged", encoding="utf-8")
    await state.ensure_managed_dir(str(root / "session"))
    (root / "session" / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="managed workspace operation failed"):
        await state.write_managed_file(
            str(root / "session" / "linked" / "victim.md"), b"overwrite"
        )
    with pytest.raises(RuntimeError, match="managed workspace operation failed"):
        await state.ensure_managed_dir(str(root / "session" / "linked" / "child"))

    assert victim.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.asyncio
async def test_managed_path_traversal_is_rejected_before_runner_call(
    tmp_path: Path,
) -> None:
    state = _runtime(tmp_path / "managed")
    with pytest.raises(ValueError, match="escaped"):
        await state.write_managed_file(str(tmp_path / "elsewhere" / "file"), b"x")
    with pytest.raises(ValueError, match="escaped"):
        await state.ensure_managed_dir(str(tmp_path / "managed" / ".." / "other"))


@pytest.mark.asyncio
async def test_managed_root_ancestor_symlink_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "managed").mkdir()
    link = tmp_path / "linked-root"
    link.symlink_to(outside, target_is_directory=True)
    state = _runtime(link / "managed")

    with pytest.raises(RuntimeError, match="managed workspace operation failed"):
        await state.ensure_managed_dir(str(link / "managed" / "session"))
    assert list((outside / "managed").iterdir()) == []


@pytest.mark.asyncio
async def test_skill_plugin_rejects_symlink_before_removing_plugin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed" / "session"
    state = _runtime(tmp_path / "managed")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("leave", encoding="utf-8")
    await state.ensure_managed_dir(str(root))
    await state.write_skills_plugin(str(root), ["Existing skill"])
    plugin = root / "opencuria-skills"
    (plugin / "outside").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimeError, match="managed workspace operation failed"):
        await state.write_skills_plugin(str(root), ["Replacement skill"])

    assert (plugin / "skills" / "opencuria-skill-001" / "SKILL.md").exists()
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "leave"


@pytest.mark.asyncio
async def test_skill_plugin_reconciles_two_to_one_to_zero_without_touching_siblings(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    root = workspace / ".opencuria" / "harness" / "claude" / "session"
    state_root = workspace / ".opencuria" / "harness" / "claude"
    state = _runtime(state_root)
    unrelated = root / "unrelated.txt"
    outside_plugin_state = workspace / ".opencuria" / "computeruse" / "state.json"
    await state.ensure_managed_dir(str(root))
    await state.write_managed_file(str(unrelated), b"keep")
    outside_plugin_state.parent.mkdir(parents=True)
    outside_plugin_state.write_bytes(b"preserve")

    two = await state.write_skills_plugin(str(root), ["First body", "Second body"])
    plugin = root / "opencuria-skills"
    assert len(two) == 2
    assert (plugin / "skills" / "opencuria-skill-002" / "SKILL.md").exists()

    one = await state.write_skills_plugin(str(root), ["Updated first body"])
    assert len(one) == 1
    assert not (plugin / "skills" / "opencuria-skill-002").exists()
    assert (
        (plugin / "skills" / "opencuria-skill-001" / "SKILL.md")
        .read_text(encoding="utf-8")
        .endswith("Updated first body")
    )

    assert await state.write_skills_plugin(str(root), []) == {}
    assert not plugin.exists()
    assert unrelated.read_bytes() == b"keep"
    assert outside_plugin_state.read_bytes() == b"preserve"


@pytest.mark.asyncio
async def test_invalid_skill_batch_does_not_remove_existing_plugin(
    tmp_path: Path,
) -> None:
    root = tmp_path / "managed" / "session"
    state = _runtime(tmp_path / "managed")
    await state.ensure_managed_dir(str(root))
    await state.write_skills_plugin(str(root), ["Existing skill"])
    plugin = root / "opencuria-skills"

    with pytest.raises(ValueError, match="size"):
        await state.write_skills_plugin(
            str(root), ["Valid", "x" * (MAX_SKILL_BODY_BYTES + 1)]
        )

    assert (plugin / "skills" / "opencuria-skill-001" / "SKILL.md").exists()


def test_skill_validation_is_shared_and_rejects_entire_batch_before_writes() -> None:
    assert validate_claude_skills(["ok", " "]) == ["ok", " "]
    with pytest.raises(ValueError, match="count"):
        validate_claude_skills(["x"] * (MAX_SKILL_COUNT + 1))
    with pytest.raises(ValueError, match="size"):
        validate_claude_skills(["x" * (MAX_SKILL_BODY_BYTES + 1)])
    with pytest.raises(ValueError, match="text"):
        validate_claude_skills(["ok", Any])  # type: ignore[list-item]


def test_sdk_preset_is_live_for_resume_updates() -> None:
    assert ClaudeRuntimeState.sdk_system_prompt() == {
        "type": "preset",
        "preset": "claude_code",
        "snapshot": False,
    }
