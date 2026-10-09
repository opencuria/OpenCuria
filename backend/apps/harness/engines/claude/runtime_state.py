"""Materialize private, session-scoped Claude CLI state and launch options."""

from __future__ import annotations

import json
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

    async def ensure_managed_dir(self, path: str) -> None:
        """Create a managed directory and reject symlinked path components."""
        if not path.startswith(f"{self.root}/"):
            raise ValueError("Claude engine directory escaped its managed state root")
        root = self.root
        script = (
            "import os,stat,sys\n"
            "root=os.path.abspath(sys.argv[1]); path=os.path.abspath(sys.argv[2])\n"
            "if os.path.commonpath((root,path))!=root: raise SystemExit(2)\n"
            "current=os.sep\n"
            "for part in os.path.relpath(root,os.sep).split(os.sep):\n"
            " current=os.path.join(current,part)\n"
            " try: os.mkdir(current,0o700)\n"
            " except FileExistsError: pass\n"
            " st=os.lstat(current)\n"
            " if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):\n"
            "  raise SystemExit(3)\n"
            "current=root\n"
            "for part in os.path.relpath(path,root).split(os.sep):\n"
            " current=os.path.join(current,part)\n"
            " try: os.mkdir(current,0o700)\n"
            " except FileExistsError: pass\n"
            " st=os.lstat(current)\n"
            " if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):\n"
            "  raise SystemExit(3)\n"
            " os.chmod(current,0o700)\n"
        )
        result = await self.accessor.exec_wait(
            ["python3", "-c", script, root, path],
            workdir=HARNESS_WORKSPACE_ROOT,
            timeout=15,
        )
        if result.exit_code != 0:
            raise RuntimeError("Could not safely prepare Claude engine state directory")

    async def write_managed_file(self, path: str, payload: bytes) -> None:
        """Write an engine-generated config or prompt file with private mode."""
        if not path.startswith(f"{self.root}/"):
            raise ValueError("Claude engine file escaped its managed state root")
        await self.accessor.write_file(path, payload, mode=0o600)

    async def write_skills_plugin(self, path: str, skills: list[str]) -> dict[str, str]:
        """Write selected skill bodies as a private, session-scoped Claude plugin.

        The bodies supplied by the harness are trusted, pre-authorized skill
        snapshots. They are never interpreted as paths, names, permissions, or
        shell commands. A generated manifest declares only the plugin metadata;
        each body remains the exact authored skill content.
        """
        if not skills:
            return {}
        if len(skills) > MAX_SKILL_COUNT:
            raise ValueError("Claude skill count exceeds the managed limit")
        root = f"{path}/opencuria-skills/.claude-plugin"
        skills_root = f"{path}/opencuria-skills/skills"
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
            if not isinstance(body, str) or not body.strip():
                continue
            if len(body.encode("utf-8")) > MAX_SKILL_BODY_BYTES:
                raise ValueError("Claude skill body exceeds the managed size limit")
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
        value = system.strip()
        if mode == "plan":
            value += (
                "\n\nOpenCuria plan mode cannot switch into build mode. "
                "Do not modify source files without explicit user approval; "
                "use the OpenCuria permission gate for every requested mutation."
            )
        return value

    @staticmethod
    def sdk_system_prompt() -> dict[str, Any]:
        """Retain Claude Code's native preset while SDK applies its options."""
        return {"type": "preset", "preset": "claude_code"}

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
    "MAX_SKILL_BODY_BYTES",
    "MAX_SKILL_COUNT",
    "MANAGED_STATE_PREFIX",
    "ClaudeRuntimeState",
]
