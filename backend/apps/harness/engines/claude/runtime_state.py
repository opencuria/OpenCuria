"""Materialize private, session-scoped Claude CLI state and launch options."""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from typing import Any

from ...access.base import (
    HARNESS_WORKSPACE_ROOT,
    WorkspaceAccessor,
    validate_harness_env,
)

MANAGED_STATE_PREFIX = f"{HARNESS_WORKSPACE_ROOT}/.opencuria/harness/claude/"
AUTH_ENV_KEYS = frozenset({"ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"})
MAX_SKILL_COUNT = 128
MAX_SKILL_BODY_BYTES = 64 * 1024
MAX_MANAGED_FILE_BYTES = 10 * 1024 * 1024
# ``engine_state`` key holding the CLI's cumulative cost after the last turn.
CLI_TOTAL_COST_KEY = "cli_total_cost_usd"

# Keep content off argv, environment variables, and runner logs. Paths and this
# constant script are the only command arguments; file bytes travel on stdin.
_SAFE_MKDIR_SCRIPT = """
import os
import sys

root = os.path.abspath(sys.argv[1])
path = os.path.abspath(sys.argv[2])
if os.path.commonpath((root, path)) != root:
    raise SystemExit(2)
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(os.sep, flags)
try:
    parts = root.lstrip(os.sep).split(os.sep)
    for index, part in enumerate(parts):
        try:
            child = os.open(part, flags, dir_fd=fd)
        except FileNotFoundError:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, flags, dir_fd=fd)
        os.close(fd)
        fd = child
        if index == len(parts) - 1:
            os.fchmod(fd, 0o700)
    relative = os.path.relpath(path, root)
    if relative != ".":
        for part in relative.split(os.sep):
            try:
                child = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
            os.fchmod(fd, 0o700)
finally:
    os.close(fd)
"""

_SAFE_WRITE_SCRIPT = """
import os
import sys
import uuid

root = os.path.abspath(sys.argv[1])
target = os.path.abspath(sys.argv[2])
mode = int(sys.argv[3], 8)
if os.path.commonpath((root, target)) != root or target == root:
    raise SystemExit(2)
limit = 10 * 1024 * 1024
parent = os.path.dirname(target)
leaf = os.path.basename(target)
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(os.sep, flags)
for part in parent.lstrip(os.sep).split(os.sep):
    if not part:
        continue
    child = os.open(part, flags, dir_fd=fd)
    os.close(fd)
    fd = child
if not leaf or leaf in (".", ".."):
    raise SystemExit(2)
tmp = ".opencuria-" + uuid.uuid4().hex + ".tmp"
file_fd = os.open(
    tmp,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
    dir_fd=fd,
)
count = 0
try:
    while True:
        block = sys.stdin.buffer.read(65536)
        if not block:
            break
        count += len(block)
        if count > limit:
            raise SystemExit(4)
        view = memoryview(block)
        while view:
            written = os.write(file_fd, view)
            view = view[written:]
    os.fchmod(file_fd, mode)
    os.fsync(file_fd)
    os.close(file_fd)
    file_fd = -1
    # replace affects a symlink leaf itself, never its target.
    os.replace(tmp, leaf, src_dir_fd=fd, dst_dir_fd=fd)
    os.fsync(fd)
finally:
    if file_fd >= 0:
        os.close(file_fd)
    try:
        os.unlink(tmp, dir_fd=fd)
    except FileNotFoundError:
        pass
    os.close(fd)
"""

_SAFE_REMOVE_TREE_SCRIPT = """
import os
import stat
import sys

root = os.path.abspath(sys.argv[1])
target = os.path.abspath(sys.argv[2])
if os.path.commonpath((root, target)) != root or target == root:
    raise SystemExit(2)
flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(os.sep, flags)
parent = os.path.dirname(target)
leaf = os.path.basename(target)
for part in parent.lstrip(os.sep).split(os.sep):
    if not part:
        continue
    child = os.open(part, flags, dir_fd=fd)
    os.close(fd)
    fd = child
try:
    info = os.stat(leaf, dir_fd=fd, follow_symlinks=False)
except FileNotFoundError:
    os.close(fd)
    raise SystemExit(0)
if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
    raise SystemExit(3)
tree = os.open(leaf, flags, dir_fd=fd)
def verify(directory):
    for name in os.listdir(directory):
        item = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(item.st_mode):
            raise SystemExit(3)
        if stat.S_ISDIR(item.st_mode):
            child = os.open(name, flags, dir_fd=directory)
            verify(child)
            os.close(child)
def clear(directory):
    for name in os.listdir(directory):
        item = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(item.st_mode):
            raise SystemExit(3)
        if stat.S_ISDIR(item.st_mode):
            child = os.open(name, flags, dir_fd=directory)
            clear(child)
            os.close(child)
            os.rmdir(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)
verify(tree)
clear(tree)
os.close(tree)
os.rmdir(leaf, dir_fd=fd)
os.close(fd)
"""


def validate_claude_skills(skills: list[str]) -> list[str]:
    """Validate all selected Claude skill bodies before managed writes begin."""
    if not isinstance(skills, list):
        raise ValueError("Claude skills must be a list")
    if len(skills) > MAX_SKILL_COUNT:
        raise ValueError("Claude skill count exceeds the managed limit")
    for body in skills:
        if not isinstance(body, str):
            raise ValueError("Claude skill bodies must be text")
        try:
            body_bytes = body.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("Claude skill body is not valid UTF-8 text") from None
        if len(body_bytes) > MAX_SKILL_BODY_BYTES:
            raise ValueError("Claude skill body exceeds the managed size limit")
    return list(skills)


class ClaudeRuntimeState:
    """Own managed CLI files, environment, and deterministic launch settings."""

    def __init__(
        self,
        *,
        accessor: WorkspaceAccessor,
        auth_env: dict[str, str],
        session_store: Any | None,
        root: str,
        sdk_version: str,
    ) -> None:
        self.accessor = accessor
        self.auth_env = auth_env
        self.session_store = session_store
        self.root = root.rstrip("/")
        self.sdk_version = sdk_version

    def _validate_managed_path(self, path: str, *, allow_root: bool = False) -> str:
        """Require an absolute, normalized path below the managed state root."""
        if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
            raise ValueError("Claude engine path is invalid")
        normalized = os.path.normpath(path)
        try:
            contained = os.path.commonpath((self.root, normalized)) == self.root
        except ValueError:
            contained = False
        if (
            not contained
            or normalized != path
            or normalized == self.root
            and not allow_root
        ):
            raise ValueError("Claude engine path escaped its managed state root")
        return normalized

    async def _run_managed_script(
        self, script: str, args: list[str], *, payload: bytes = b""
    ) -> None:
        """Run bounded, secret-free Python code and send content over stdin."""
        command = ["python3", "-c", script, *args]
        if len(command) > 8 or any(len(arg) > 4096 for arg in command):
            raise ValueError("Claude managed operation exceeds the command limit")
        stream = await self.accessor.open_process(
            command, workdir=HARNESS_WORKSPACE_ROOT, timeout=30
        )
        try:
            for offset in range(0, len(payload), 64 * 1024):
                await stream.send(payload[offset : offset + 64 * 1024])
            await stream.send_eof()
            # The scripts are silent; drain anyway so a future small diagnostic
            # cannot block child exit. Never include remote output in exceptions.
            while await stream.receive():
                pass
            exit_code = await stream.wait_closed()
            if exit_code != 0:
                raise RuntimeError("Claude managed workspace operation failed")
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def ensure_managed_dir(self, path: str) -> None:
        """Create a managed directory while refusing symlinked path components."""
        path = self._validate_managed_path(path)
        await self._run_managed_script(_SAFE_MKDIR_SCRIPT, [self.root, path])

    async def write_managed_file(self, path: str, payload: bytes) -> None:
        """Atomically write a private managed file without following symlinks."""
        path = self._validate_managed_path(path)
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError("Claude managed file content must be bytes")
        raw = bytes(payload)
        if len(raw) > MAX_MANAGED_FILE_BYTES:
            raise ValueError("Claude managed file exceeds the size limit")
        await self._run_managed_script(
            _SAFE_WRITE_SCRIPT,
            [self.root, path, "600"],
            payload=raw,
        )

    async def _remove_managed_tree(self, path: str) -> None:
        """Remove one generated directory tree without crossing symlinks."""
        path = self._validate_managed_path(path)
        await self._run_managed_script(_SAFE_REMOVE_TREE_SCRIPT, [self.root, path])

    async def write_skills_plugin(self, path: str, skills: list[str]) -> dict[str, str]:
        """Write selected skill bodies as a private, session-scoped Claude plugin.

        The bodies supplied by the harness are trusted, pre-authorized skill
        snapshots. They are never interpreted as paths, names, permissions, or
        shell commands. A generated manifest declares only the plugin metadata;
        each body remains the exact authored skill content.
        """
        skills = validate_claude_skills(skills)
        plugin_dir = self._validate_managed_path(f"{path}/opencuria-skills")
        root = f"{plugin_dir}/.claude-plugin"
        skills_root = f"{plugin_dir}/skills"
        # This is an exclusively generated directory below this harness
        # session's managed root; clear it even when the new skill set is empty.
        await self._remove_managed_tree(plugin_dir)
        if not any(body.strip() for body in skills):
            return {}
        await self.ensure_managed_dir(root)
        await self.ensure_managed_dir(skills_root)
        manifest = {
            "name": "opencuria-managed-skills",
            "version": "1.0.0",
            "description": "Session-scoped skills selected in OpenCuria",
        }
        await self.write_managed_file(
            f"{root}/plugin.json", json.dumps(manifest, separators=(",", ":")).encode()
        )
        names: dict[str, str] = {}
        for index, body in enumerate(skills):
            if not body.strip():
                continue
            name = f"opencuria-skill-{index + 1:03d}"
            skill_dir = f"{skills_root}/{name}"
            await self.ensure_managed_dir(skill_dir)
            payload = body.strip()
            if not payload.startswith("---\n"):
                payload = (
                    "---\n"
                    + "name: "
                    + name
                    + "\ndescription: OpenCuria selected skill\n---\n\n"
                    + payload
                )
            await self.write_managed_file(f"{skill_dir}/SKILL.md", payload.encode())
            names[f"opencuria-managed-skills:{name}"] = name
        return names

    def cli_environment(self, config_dir: str) -> dict[str, str]:
        """Build the allowlisted CLI environment for the isolated SDK session."""
        clean_auth = validate_harness_env(self.auth_env)
        if set(clean_auth) - AUTH_ENV_KEYS:
            raise ValueError("Claude auth_env contains an unsupported credential name")
        if len(clean_auth) != 1:
            raise ValueError("Claude auth_env must provide exactly one credential")
        return {
            **clean_auth,
            "CLAUDE_CONFIG_DIR": config_dir,
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": self.sdk_version,
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "DISABLE_UPDATES": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            # Forks inherit the parent's tools instead of an OpenCuria child scope.
            "CLAUDE_CODE_FORK_SUBAGENT": "0",
        }

    @staticmethod
    def mcp_cli_config() -> dict[str, Any]:
        """Return the sole SDK server descriptor permitted by this engine."""
        return {"mcpServers": {"opencuria": {"type": "sdk", "name": "opencuria"}}}

    @staticmethod
    def valid_external_session_id(value: str) -> str:
        """Require Claude external transcript identifiers to be UUIDs."""
        try:
            return str(uuid.UUID((value or "").strip()))
        except (ValueError, AttributeError):
            raise ValueError("Claude external session id must be a UUID") from None

    @staticmethod
    def claude_system_prompt(system: str, mode: str) -> str:
        """Compose OpenCuria run context to append to native Claude Code."""
        del mode
        return system.strip()

    @staticmethod
    def sdk_system_prompt() -> dict[str, Any]:
        """Retain Claude Code's native preset while SDK applies its options."""
        return {"type": "preset", "preset": "claude_code", "snapshot": False}

    @staticmethod
    def settings(mode: str) -> dict[str, Any]:
        """Configure native features without bypassing permission hooks."""
        del mode
        return {
            "permissions": {"deny": ["ExitPlanMode", "AskUserQuestion", "TodoWrite"]},
            "autoMemoryEnabled": False,
            "env": {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"},
        }

    def build_command(
        self,
        *,
        binary: str,
        config_dir: str,
        mcp_path: str,
        prompt_path: str,
        model: str,
        effort: str,
        permission_mode: str,
        native_tools: list[str],
        external_session_id: str,
        fresh_session_id: str,
        has_session_store: bool,
        plugin_dir: str | None = None,
        settings_path: str | None = None,
    ) -> list[str]:
        """Build bounded CLI arguments while preserving the SDK's session mode."""
        del mcp_path
        session_id = (
            self.valid_external_session_id(external_session_id)
            if external_session_id
            else ""
        )
        argv = [
            binary,
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
            "--setting-sources=",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(self.mcp_cli_config(), separators=(",", ":")),
            "--include-partial-messages",
            "--include-hook-events",
            "--permission-prompts",
            "host",
            "--permission-prompt-tool",
            "stdio",
            "--no-chrome",
            "--permission-mode",
            permission_mode,
            "--append-system-prompt-file",
            prompt_path,
            "--model",
            model,
        ]
        argv.extend(["--tools", ",".join(native_tools)])
        if effort:
            argv.extend(["--effort", effort])
        if external_session_id:
            argv.append(f"--resume={session_id}")
        else:
            fresh_id = self.valid_external_session_id(fresh_session_id)
            argv.append(f"--session-id={fresh_id}")
        if has_session_store:
            argv.append("--session-mirror")
        if plugin_dir:
            argv.extend(["--plugin-dir", plugin_dir])
        if settings_path:
            argv.extend(["--settings", settings_path])
        argv.extend(["--disallowedTools", "AskUserQuestion,ExitPlanMode,TodoWrite"])
        if len(argv) > 64 or any(len(item) > 4096 for item in argv):
            raise ValueError("Claude CLI argv exceeds the bounded command limit")
        return argv


__all__ = [
    "AUTH_ENV_KEYS",
    "CLI_TOTAL_COST_KEY",
    "MAX_SKILL_BODY_BYTES",
    "MAX_SKILL_COUNT",
    "MANAGED_STATE_PREFIX",
    "ClaudeRuntimeState",
    "validate_claude_skills",
]
