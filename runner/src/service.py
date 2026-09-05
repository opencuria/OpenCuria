"""Central business logic for workspace management.

The runner is a "dumb executor" — it runs lifecycle, terminal, file,
and harness exec operations requested by the backend.  All agentic
knowledge (providers, tools, permissions, prompts) lives in the
backend harness.

The runner has no local database — all workspace state is derived from
the runtime backends (Docker, QEMU/KVM) and cached in memory.
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import shlex
import tarfile
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import structlog

from .config import RunnerSettings
from .models import DesktopSession, WorkspaceInfo
from .runtime.base import ImageArtifactInfo, PtyHandle, RuntimeBackend, WorkspaceConfig

logger = structlog.get_logger(__name__)

FILE_READ_DEFAULT_MAX_SIZE = 5 * 1024 * 1024  # 5 MB
FILE_READ_ABSOLUTE_MAX_SIZE = 100 * 1024 * 1024  # 100 MB
FILE_UPLOAD_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
_SHELL_OPERATOR_TOKENS = {
    "|",
    "||",
    "&",
    "&&",
    ";",
    ";;",
    "(",
    ")",
    "<",
    "<<",
    "<<<",
    ">",
    ">>",
    ">|",
    "&>",
    "&>>",
}
_REDIRECTION_RE = re.compile(
    r"^\d*(?:>>?|<<?|<>|>&|<&|&>>?)(?:\d+|[^\s].*)?$"
)

WORKSPACE_CREDENTIAL_DIR = "/root/.opencuria-credentials"
WORKSPACE_CREDENTIAL_MANIFEST = "/root/.opencuria-credentials/manifest"
WORKSPACE_CREDENTIAL_ENV_FILE = "/root/.opencuria-env.sh"
WORKSPACE_CREDENTIAL_PROFILE_D = "/etc/profile.d/opencuria-env.sh"
WORKSPACE_CREDENTIAL_BASHRC = "/root/.bashrc"
WORKSPACE_CREDENTIAL_BASHRC_LINE = (
    "test -f /root/.opencuria-env.sh && . /root/.opencuria-env.sh"
)
WORKSPACE_CREDENTIAL_ENVIRONMENT = "/etc/environment"
WORKSPACE_CREDENTIAL_ENVIRONMENT_START = "# OPENCURIA_CREDENTIALS_START"
WORKSPACE_CREDENTIAL_ENVIRONMENT_END = "# OPENCURIA_CREDENTIALS_END"


@dataclass
class TerminalSession:
    """Runtime PTY handle for an interactive terminal session."""

    handle: PtyHandle
    runtime: RuntimeBackend


class WorkspaceService:
    """Orchestrates workspace lifecycle and command execution.

    This is the *single* business-logic layer.  It is intentionally
    agnostic of the transport (WebSocket) and supports multiple
    runtime backends (Docker, QEMU/KVM) simultaneously.

    State management:
        - Each runtime backend is the point of truth for its workspaces.
        - An in-memory ``_cache`` dict maps ``workspace_id`` →
          ``WorkspaceInfo`` for fast lookups.
        - On startup, ``sync_from_runtime()`` rebuilds the cache by
          querying all registered runtimes.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend],
        settings: RunnerSettings,
    ) -> None:
        self._runtimes = runtimes
        self._settings = settings
        self._cache: dict[uuid.UUID, WorkspaceInfo] = {}
        self._terminals: dict[str, TerminalSession] = {}
        self._desktop_sessions: dict[uuid.UUID, DesktopSession] = {}
        # Limit concurrent file-read SSH channels per workspace to avoid
        # exhausting the SSH server's MaxSessions limit (default: 10).
        # Each read_file call opens at most 1 SSH channel, so a limit of 4
        # keeps peak channel usage well below 10.
        self._file_read_semaphores: dict[uuid.UUID, asyncio.Semaphore] = {}
        # Self-healing: tracks when each workspace was first found unreachable.
        # Cleared once the workspace becomes reachable again.
        self._unreachable_since: dict[uuid.UUID, float] = {}

    # -- cache management ------------------------------------------------------

    async def sync_from_runtime(self) -> None:
        """Rebuild the in-memory cache from live runtime state.

        Called at startup and can be called periodically to reconcile
        the cache with actual runtime state (e.g. workspaces killed
        externally).  Queries all registered runtime backends.
        """
        new_cache: dict[uuid.UUID, WorkspaceInfo] = {}

        for runtime_type, runtime in self._runtimes.items():
            infos = await runtime.list_workspaces()
            for info in infos:
                try:
                    ws_id = uuid.UUID(info.workspace_id)
                except ValueError:
                    logger.warning(
                        "skipping_invalid_workspace_id",
                        raw_id=info.workspace_id,
                    )
                    continue

                existing = self._cache.get(ws_id)

                new_cache[ws_id] = WorkspaceInfo(
                    workspace_id=ws_id,
                    instance_id=info.instance_id,
                    status=info.status,
                    runtime_type=runtime_type,
                    created_at=(
                        existing.created_at
                        if existing
                        else datetime.now(timezone.utc)
                    ),
                )

        # Preserve "creating" entries that are not yet visible to the runtime.
        # A workspace in the "creating" state has been registered by the service
        # layer but runtime.create_workspace() is still in progress (e.g. the
        # QEMU VM is booting).  Dropping it from the cache would cause the
        # next heartbeat to omit it and the backend to mark it as failed.
        for ws_id, existing in self._cache.items():
            if existing.status == "creating" and ws_id not in new_cache:
                new_cache[ws_id] = existing

        self._cache = new_cache
        logger.info(
            "cache_synced_from_runtime",
            workspace_count=len(self._cache),
        )

    def _get_cached(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Look up a workspace in the cache or raise."""
        info = self._cache.get(workspace_id)
        if info is None:
            raise ValueError(f"Workspace {workspace_id} not found")
        return info

    def _get_runtime(self, workspace_id: uuid.UUID) -> RuntimeBackend:
        """Return the runtime backend for a workspace."""
        info = self._get_cached(workspace_id)
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' not available for "
                f"workspace {workspace_id}"
            )
        return runtime

    def _get_runtime_by_type(self, runtime_type: str) -> RuntimeBackend:
        """Return the runtime backend by type name."""
        runtime = self._runtimes.get(runtime_type)
        if runtime is None:
            raise RuntimeError(f"Runtime '{runtime_type}' is not enabled")
        return runtime

    @property
    def supported_runtimes(self) -> list[str]:
        """Return the list of enabled runtime type names."""
        return list(self._runtimes.keys())

    # -- command execution helpers ---------------------------------------------

    def _normalise_command_args(self, raw_args: list[str] | str) -> list[str]:
        """Return command args suitable for runtime execution.

        Commands are primarily modelled as argv lists. However, configure
        commands are sometimes authored with shell operators (e.g. ``|``,
        ``&&``) split into individual args. Such operators are treated as
        literal argv tokens by Docker/SSH exec and therefore fail.

        To keep backend data backwards-compatible, detect shell operators and
        redirections (including forms with attached targets like
        ``2>/dev/null``) and route execution through ``bash -lc`` with a safely
        re-constructed command string.
        """
        if isinstance(raw_args, str):
            return ["bash", "-lc", raw_args]

        args = [str(arg) for arg in raw_args]
        if len(args) >= 2 and args[0] in {"bash", "sh"} and args[1] in {
            "-c",
            "-lc",
        }:
            return args

        if not any(
            token in _SHELL_OPERATOR_TOKENS or _REDIRECTION_RE.match(token)
            for token in args
        ):
            return args

        command_str = " ".join(
            token
            if token in _SHELL_OPERATOR_TOKENS or _REDIRECTION_RE.match(token)
            else shlex.quote(token)
            for token in args
        )
        return ["bash", "-lc", command_str]

    async def _exec_command(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> tuple[int, str]:
        """Execute a structured command dict inside a workspace.

        Args:
            runtime: The runtime backend to use.
            instance_id: Runtime-specific instance ID.
            command: Dict with keys ``args``, ``workdir``, ``env``,
                ``description``.

        Returns:
            Tuple of (exit_code, output).
        """
        wrapped_command = self._wrap_command_with_persistent_env(command)
        command_args = self._normalise_command_args(wrapped_command["args"])
        return await runtime.exec_command_wait(
            instance_id,
            command=command_args,
            workdir=wrapped_command.get("workdir"),
            env=wrapped_command.get("env"),
        )

    async def _exec_command_stream(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        command: dict,
    ) -> AsyncIterator[str]:
        """Execute a structured command dict and stream output lines.

        Args:
            runtime: The runtime backend to use.
            instance_id: Runtime-specific instance ID.
            command: Dict with keys ``args``, ``workdir``, ``env``,
                ``description``.

        Yields:
            Raw output lines from the command.
        """
        wrapped_command = self._wrap_command_with_persistent_env(command)
        command_args = self._normalise_command_args(wrapped_command["args"])
        async for line in runtime.exec_command(
            instance_id,
            command=command_args,
            workdir=wrapped_command.get("workdir"),
            env=wrapped_command.get("env"),
        ):
            yield line

    # -- persistent workspace credentials -------------------------------------

    @staticmethod
    def _build_tar_entries(
        files: list[tuple[str, bytes, int]],
    ) -> bytes:
        """Build a tar archive containing multiple files."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for filename, content, mode in files:
                info = tarfile.TarInfo(name=filename)
                info.size = len(content)
                info.mode = mode
                tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    @staticmethod
    def _credential_path_helpers() -> list[str]:
        """Return shell helper functions used by inject and remove scripts."""

        return [
            'opencuria_credential_home="${HOME:-/root}"',
            "opencuria_resolve_credential_path() {",
            '  raw_path="$1"',
            "  tilde_prefix='~/'",
            "  home_prefix='${HOME}/'",
            '  if [ "$raw_path" = "~" ] || [ "$raw_path" = "${HOME}" ] || [ "$raw_path" = "${opencuria_credential_home}" ]; then',
            '    printf "%s\\n" "$opencuria_credential_home"',
            '    return',
            "  fi",
            '  if [ "${raw_path#"$tilde_prefix"}" != "$raw_path" ]; then',
            '    printf "%s/%s\\n" "$opencuria_credential_home" "${raw_path#"$tilde_prefix"}"',
            '    return',
            "  fi",
            '  if [ "${raw_path#"$home_prefix"}" != "$raw_path" ]; then',
            '    printf "%s/%s\\n" "$opencuria_credential_home" "${raw_path#"$home_prefix"}"',
            '    return',
            "  fi",
            '  if [ "${raw_path#/}" != "$raw_path" ]; then',
            '    printf "%s\\n" "$raw_path"',
            '    return',
            "  fi",
            '  printf "%s/%s\\n" "$opencuria_credential_home" "$raw_path"',
            "}",
            "opencuria_strip_environment_block() {",
            f"  env_file={shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}",
            '  if [ ! -f "$env_file" ]; then',
            "    return",
            "  fi",
            '  tmp_env=$(mktemp)',
            f"  awk '/{WORKSPACE_CREDENTIAL_ENVIRONMENT_START}/{{skip=1}} "
            f"/{WORKSPACE_CREDENTIAL_ENVIRONMENT_END}/{{skip=0; next}} !skip' "
            '"$env_file" > "$tmp_env" || true',
            '  cat "$tmp_env" > "$env_file"',
            '  rm -f "$tmp_env"',
            "}",
        ]

    def _wrap_command_with_persistent_env(self, command: dict) -> dict:
        """Source persistent workspace credentials before running a command."""

        normalised_args = self._normalise_command_args(command["args"])
        extra_env = command.get("env") or {}
        extra_exports = "; ".join(
            f"export {key}={shlex.quote(str(value))}"
            for key, value in extra_env.items()
        )
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi"
        )
        if extra_exports:
            source = f"{source}; {extra_exports}"
        wrapper = f'{source}; exec "$@"'
        return {
            **command,
            "args": [
                "bash",
                "-lc",
                wrapper,
                "opencuria-exec",
                *normalised_args,
            ],
            "env": {},
        }

    async def remove_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        log,
    ) -> None:
        """Idempotently remove persisted credential material from a workspace."""

        cleanup_script = "\n".join(
            [
                "#!/bin/sh",
                "set -eu",
                *self._credential_path_helpers(),
                f"manifest={shlex.quote(WORKSPACE_CREDENTIAL_MANIFEST)}",
                'if [ -f "$manifest" ]; then',
                '  while IFS= read -r file_path || [ -n "$file_path" ]; do',
                '    [ -z "$file_path" ] && continue',
                '    rm -f "$(opencuria_resolve_credential_path "$file_path")"',
                "  done < \"$manifest\"",
                "fi",
                "opencuria_strip_environment_block",
                f"rm -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} "
                f"{shlex.quote(WORKSPACE_CREDENTIAL_PROFILE_D)}",
                f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)} ]; then",
                "  tmp_bashrc=$(mktemp)",
                f"  grep -vxF {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f"{shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)} > \"$tmp_bashrc\" || true",
                f"  cat \"$tmp_bashrc\" > {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}",
                '  rm -f "$tmp_bashrc"',
                "fi",
                "rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519_*",
                "rm -f /root/.ssh/config /root/.ssh/known_hosts",
                f"rm -rf {shlex.quote(WORKSPACE_CREDENTIAL_DIR)}",
                "rm -rf /tmp/opencuria-op-*",
                "find /var/lib/cloud/instances -type f "
                "\\( -name 'user-data.txt' -o -name 'user-data.txt.i' "
                "-o -name 'cloud-config.txt' -o -path '*/scripts/runcmd' \\) "
                "-delete 2>/dev/null || true",
            ]
        )
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", cleanup_script],
            workdir="/root",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to remove workspace credentials: {output}")
        log.info("workspace_credentials_removed")

    async def inject_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        files: list[dict[str, Any]] | None,
        ssh_keys: list[str] | None,
        log,
    ) -> bool:
        """Persist credentials on the workspace disk, replacing any previous set.

        Returns True when credential material was written, False when the
        workspace has no attached secrets after a clean remove.
        """

        await self.remove_workspace_credentials(runtime, instance_id, log)

        env_vars = env_vars or {}
        credential_files = files or []
        ssh_keys = ssh_keys or []
        if not env_vars and not credential_files and not ssh_keys:
            return False

        staging_dir = WORKSPACE_CREDENTIAL_DIR
        files_dir = f"{staging_dir}/files"
        ssh_dir = f"{staging_dir}/ssh"
        install_path = f"{staging_dir}/install.sh"
        archive_files: list[tuple[str, bytes, int]] = []
        installed_paths: list[str] = [
            WORKSPACE_CREDENTIAL_ENV_FILE,
            WORKSPACE_CREDENTIAL_PROFILE_D,
            WORKSPACE_CREDENTIAL_MANIFEST,
        ]
        helper_lines = self._credential_path_helpers()
        install_lines = [
            "#!/bin/sh",
            "set -eu",
            *helper_lines,
            f"mkdir -p {shlex.quote(WORKSPACE_CREDENTIAL_DIR)} /root/.ssh /etc/profile.d",
            f"install -m 600 {shlex.quote(staging_dir + '/env.sh')} "
            f"{shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}",
            f"install -m 644 {shlex.quote(staging_dir + '/profile.d.sh')} "
            f"{shlex.quote(WORKSPACE_CREDENTIAL_PROFILE_D)}",
            "opencuria_strip_environment_block",
            f"printf '%s\\n' {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT_START)} "
            f">> {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}",
        ]

        env_export_lines = [
            "#!/bin/sh",
            'export PATH="/root/.local/bin:$PATH"',
        ]
        for key, value in env_vars.items():
            env_export_lines.append(f"export {key}={shlex.quote(str(value))}")
            install_lines.append(
                "printf '%s\\n' "
                f"{shlex.quote(f'{key}={value}')} "
                f">> {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}"
            )
        install_lines.append(
            f"printf '%s\\n' {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT_END)} "
            f">> {shlex.quote(WORKSPACE_CREDENTIAL_ENVIRONMENT)}"
        )
        archive_files.append(
            ("env.sh", ("\n".join(env_export_lines) + "\n").encode("utf-8"), 0o600)
        )
        archive_files.append(
            (
                "profile.d.sh",
                (f"{WORKSPACE_CREDENTIAL_BASHRC_LINE}\n").encode("utf-8"),
                0o644,
            )
        )

        install_lines.extend(
            [
                f"touch {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}",
                f"if ! grep -qxF {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f"{shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}; then",
                f"  printf '%s\\n' {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f">> {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}",
                "fi",
            ]
        )

        for index, credential_file in enumerate(credential_files, start=1):
            source_relpath = f"files/credential_{index}"
            source_abspath = f"{files_dir}/credential_{index}"
            target_path = str(credential_file["target_path"])
            mode = int(credential_file.get("mode", 0o600))
            content = str(credential_file.get("content", ""))
            archive_files.append((source_relpath, content.encode("utf-8"), 0o600))
            install_lines.extend(
                [
                    "target_path=$(opencuria_resolve_credential_path "
                    f"{shlex.quote(target_path)})",
                    'mkdir -p "$(dirname "$target_path")"',
                    f"install -m {mode:o} {shlex.quote(source_abspath)} "
                    '"$target_path"',
                ]
            )
            installed_paths.append(target_path)

        if ssh_keys:
            config_lines = [
                "Host *",
                "    StrictHostKeyChecking accept-new",
                "    UserKnownHostsFile /root/.ssh/known_hosts",
                "    IdentitiesOnly yes",
            ]
            for index, key_pem in enumerate(ssh_keys):
                key_name = "id_ed25519" if index == 0 else f"id_ed25519_{index + 1}"
                archive_files.append(
                    (
                        f"ssh/{key_name}",
                        key_pem.rstrip().encode("utf-8") + b"\n",
                        0o600,
                    )
                )
                install_lines.append(
                    f"install -m 600 {shlex.quote(ssh_dir + '/' + key_name)} "
                    f"{shlex.quote('/root/.ssh/' + key_name)}"
                )
                config_lines.append(f"    IdentityFile /root/.ssh/{key_name}")
                installed_paths.append(f"/root/.ssh/{key_name}")
            archive_files.append(("ssh/known_hosts", b"", 0o600))
            archive_files.append(
                (
                    "ssh/config",
                    ("\n".join(config_lines) + "\n").encode("utf-8"),
                    0o600,
                )
            )
            install_lines.extend(
                [
                    f"install -m 600 {shlex.quote(ssh_dir + '/config')} /root/.ssh/config",
                    f"install -m 600 {shlex.quote(ssh_dir + '/known_hosts')} "
                    "/root/.ssh/known_hosts",
                ]
            )
            installed_paths.extend(["/root/.ssh/config", "/root/.ssh/known_hosts"])

        manifest = "".join(f"{path}\n" for path in installed_paths)
        archive_files.append(
            ("manifest", manifest.encode("utf-8"), 0o600)
        )
        archive_files.append(
            ("install.sh", ("\n".join(install_lines) + "\n").encode("utf-8"), 0o700)
        )

        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["mkdir", "-p", staging_dir, f"{staging_dir}/files", f"{staging_dir}/ssh"],
            workdir="/root",
        )
        if exit_code != 0:
            raise RuntimeError(
                f"Failed to create credential staging directory: {output}"
            )

        archive_data = self._build_tar_entries(archive_files)
        await runtime.put_archive(instance_id, staging_dir, archive_data)
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", f". {shlex.quote(install_path)}"],
            workdir="/root",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to inject workspace credentials: {output}")

        log.info(
            "workspace_credentials_injected",
            has_env=bool(env_vars),
            file_count=len(credential_files),
            ssh_key_count=len(ssh_keys),
        )
        return True

    # -- workspace lifecycle ---------------------------------------------------

    async def create_workspace(
        self,
        repos: list[str],
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
        workspace_id: uuid.UUID | None = None,
        runtime_type: str = "docker",
        image_tag: str | None = None,
        base_image_path: str | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Create a new workspace, inject credentials, and clone repos.

        Args:
            repos: Git repository URLs to clone into the workspace.
            env_vars: Environment variables persisted in the workspace
                until a controlled stop.
            files: Credential files persisted in the workspace until a
                controlled stop.
            ssh_keys: SSH private keys persisted in the workspace until a
                controlled stop.
            workspace_id: Workspace ID assigned by the backend.
            runtime_type: Which runtime to use (``"docker"`` or ``"qemu"``).

        Returns the workspace UUID and whether credentials were injected.
        """
        if workspace_id is None:
            workspace_id = uuid.uuid4()

        runtime = self._get_runtime_by_type(runtime_type)

        log = logger.bind(
            workspace_id=str(workspace_id),
            runtime=runtime_type,
        )
        log.info("creating_workspace", repos=repos)

        # Build runtime-appropriate config
        if runtime_type == "docker":
            if not image_tag:
                raise RuntimeError("Docker workspace creation requires an image tag")
            volume_name = f"opencuria-workspace-{workspace_id}"
            config = WorkspaceConfig(
                workspace_id=str(workspace_id),
                image=image_tag,
                env_vars={},
                volumes={volume_name: {"bind": "/workspace", "mode": "rw"}},
                network=self._settings.docker_network,
                labels={"opencuria.workspace-id": str(workspace_id)},
            )
        else:
            if not base_image_path:
                raise RuntimeError("QEMU workspace creation requires a base image path")
            # QEMU — image is base QCOW2 path, no Docker volumes
            config = WorkspaceConfig(
                workspace_id=str(workspace_id),
                image=base_image_path,
                env_vars={},
                network=self._settings.qemu_network,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                labels={"opencuria.workspace-id": str(workspace_id)},
            )

        # Register a "creating" cache entry *before* calling
        # runtime.create_workspace() so that heartbeat syncs during VM boot
        # (which can take 60 s+ for QEMU) do not drop this workspace and
        # cause the backend to mark it as failed.  instance_id is unknown at
        # this point — it will be updated once create_workspace() returns.
        self._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id="",
            status="creating",
            runtime_type=runtime_type,
        )

        try:
            instance_id = await runtime.create_workspace(config)
        except Exception:
            log.exception("workspace_creation_failed")
            self._cache.pop(workspace_id, None)
            raise

        # Update cache with the real instance_id now that the runtime has assigned it.
        self._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id,
            instance_id=instance_id,
            status="creating",
            runtime_type=runtime_type,
        )

        credentials_present = await self.inject_workspace_credentials(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

        for repo_url in repos:
            log.info("cloning_repo", repo=repo_url)
            exit_code, output = await self._exec_command(
                runtime,
                instance_id,
                {
                    "args": ["git", "clone", repo_url],
                    "workdir": "/workspace",
                    "env": {},
                    "description": f"Clone repository: {repo_url}",
                },
            )
            if exit_code != 0:
                log.warning("repo_clone_failed", repo=repo_url, output=output)
            else:
                log.info("repo_cloned", repo=repo_url)

        self._cache[workspace_id].status = "running"

        log.info("workspace_ready", credentials_present=credentials_present)
        return workspace_id, credentials_present

    async def stop_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Remove credentials then stop a running workspace.

        Returns False because credentials are stripped before the instance
        is stopped. Raises if credential removal fails so the workspace
        stays running with secrets still present.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        await self.remove_workspace_credentials(runtime, info.instance_id, log)
        await runtime.stop_workspace(info.instance_id)
        info.status = "exited"
        log.info("workspace_stopped")
        return False

    async def resume_workspace(
        self,
        workspace_id: uuid.UUID,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Resume a stopped workspace and re-inject persistent credentials."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        if info.runtime_type == "qemu":
            if qemu_vcpus is None or qemu_memory_mb is None or qemu_disk_size_gb is None:
                raise RuntimeError("Missing QEMU resource settings for resume")
            await runtime.reconfigure_workspace(
                info.instance_id,
                qemu_vcpus=qemu_vcpus,
                qemu_memory_mb=qemu_memory_mb,
                qemu_disk_size_gb=qemu_disk_size_gb,
                restart=False,
            )

        await runtime.start_workspace(info.instance_id)
        info.status = "running"
        credentials_present = await self.inject_workspace_credentials(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        log.info("workspace_resumed", credentials_present=credentials_present)
        return credentials_present

    async def inject_credentials(
        self,
        workspace_id: uuid.UUID,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> bool:
        """Replace persistent credentials on a running workspace."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        return await self.inject_workspace_credentials(
            runtime,
            info.instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )

    async def update_workspace_resources(
        self,
        workspace_id: uuid.UUID,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> None:
        """Reconfigure resources for an existing QEMU workspace."""
        info = self._get_cached(workspace_id)
        if info.runtime_type != "qemu":
            raise RuntimeError("Workspace runtime does not support resource reconfiguration")
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        await runtime.reconfigure_workspace(
            info.instance_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            restart=True,
        )
        info.status = "running"

    async def remove_workspace(self, workspace_id: uuid.UUID) -> None:
        """Remove a workspace and clean up resources."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._cache.pop(workspace_id, None)

        if info and info.instance_id:
            runtime = self._runtimes.get(info.runtime_type)
            if runtime:
                await runtime.remove_workspace(info.instance_id)

        log.info("workspace_removed")

    async def cleanup_unknown_workspace(self, workspace_id: uuid.UUID) -> bool:
        """Best-effort cleanup for a runtime workspace unknown to the backend.

        Returns ``True`` when a cached runtime instance was found and cleanup
        was attempted. Returns ``False`` when the workspace was already absent.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._cache.pop(workspace_id, None)
        self._unreachable_since.pop(workspace_id, None)

        if info is None:
            log.info("unknown_workspace_already_absent")
            return False

        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' not available for cleanup"
            )

        if info.instance_id:
            await runtime.remove_workspace(info.instance_id)

        log.warning(
            "unknown_workspace_cleaned",
            runtime_type=info.runtime_type,
            instance_id=info.instance_id,
        )
        return True

    # -- self-healing SSH health check -----------------------------------------

    async def _check_workspace_reachable(
        self, workspace_id: uuid.UUID, info: WorkspaceInfo
    ) -> bool:
        """Return True if the workspace responds to a lightweight exec probe.

        Uses a short timeout so the loop does not block for a long time.
        """
        runtime = self._runtimes.get(info.runtime_type)
        if runtime is None or not info.instance_id:
            return True  # cannot check — assume reachable to avoid false restarts

        try:
            exit_code, _ = await asyncio.wait_for(
                runtime.exec_command_wait(
                    info.instance_id,
                    command=["echo", "ok"],
                ),
                timeout=15,
            )
            return exit_code == 0
        except Exception:
            return False

    async def run_health_check_loop(self) -> None:
        """Periodically probe running workspaces and restart unreachable ones.

        Runs indefinitely; cancel the task to stop it.

        A workspace is restarted when it has been continuously unreachable for
        more than ``settings.ssh_unreachable_timeout`` seconds.  After a
        restart, the unreachable timer is cleared so the workspace gets a
        fresh chance to come up.
        """
        interval = self._settings.ssh_health_check_interval
        timeout = self._settings.ssh_unreachable_timeout

        log = logger.bind(loop="health_check")
        log.info(
            "health_check_loop_started",
            check_interval_s=interval,
            unreachable_timeout_s=timeout,
        )

        while True:
            try:
                await asyncio.sleep(interval)

                # Snapshot the cache — do not hold it across awaits.
                candidates = [
                    (ws_id, info)
                    for ws_id, info in self._cache.items()
                    if info.status == "running"
                ]

                for ws_id, info in candidates:
                    reachable = await self._check_workspace_reachable(ws_id, info)

                    if reachable:
                        # Clear any existing failure timer.
                        self._unreachable_since.pop(ws_id, None)
                        continue

                    # Workspace is unreachable.
                    first_failure = self._unreachable_since.setdefault(ws_id, time.monotonic())
                    unreachable_for = time.monotonic() - first_failure

                    log.warning(
                        "workspace_unreachable",
                        workspace_id=str(ws_id),
                        unreachable_for_s=round(unreachable_for),
                        threshold_s=timeout,
                    )

                    if unreachable_for >= timeout:
                        log.error(
                            "workspace_self_healing_restart",
                            workspace_id=str(ws_id),
                            runtime=info.runtime_type,
                        )
                        try:
                            runtime = self._runtimes.get(info.runtime_type)
                            if runtime and info.instance_id:
                                await runtime.restart_workspace(info.instance_id)
                                # Reset status and clear the failure timer.
                                if ws_id in self._cache:
                                    self._cache[ws_id].status = "running"
                                self._unreachable_since.pop(ws_id, None)
                                log.info(
                                    "workspace_self_healed",
                                    workspace_id=str(ws_id),
                                )
                        except Exception:
                            log.exception(
                                "workspace_self_heal_failed",
                                workspace_id=str(ws_id),
                            )

            except asyncio.CancelledError:
                log.info("health_check_loop_stopped")
                break
            except Exception:
                log.exception("health_check_loop_error")

    async def list_workspaces(self) -> list[WorkspaceInfo]:
        """Return all known workspaces, refreshing from the runtime."""
        await self.sync_from_runtime()
        return list(self._cache.values())

    async def get_workspace(self, workspace_id: uuid.UUID) -> WorkspaceInfo:
        """Return a single workspace by ID, checking live status."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        # Refresh status from runtime
        if info.instance_id:
            try:
                status = await runtime.get_workspace_status(
                    info.instance_id
                )
                info.status = status.status
            except Exception:
                info.status = "unknown"

        return info

    def get_workspace_statuses(self) -> list[dict]:
        """Return lightweight status list for heartbeat reporting.

        Reads from the in-memory cache without hitting the runtime,
        so it's fast enough for periodic heartbeats.
        """
        return [
            {
                "workspace_id": str(info.workspace_id),
                "status": info.status,
                "runtime_type": info.runtime_type,
            }
            for info in self._cache.values()
        ]

    async def _is_desktop_session_live(self, workspace_id: uuid.UUID) -> bool:
        """Return whether the cached desktop session still accepts connections."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        exit_code, _ = await runtime.exec_command_wait(
            info.instance_id,
            [
                "sh",
                "-lc",
                (
                    "if command -v python3 >/dev/null 2>&1; then "
                    "python3 -c \"import socket,sys; "
                    "sock=socket.socket(); sock.settimeout(1); "
                    "rc=sock.connect_ex(('127.0.0.1',6901)); sock.close(); "
                    "sys.exit(0 if rc == 0 else 1)\"; "
                    "else "
                    "pgrep -f 'Xvnc.*:1|Xtigervnc.*:1' >/dev/null; "
                    "fi"
                ),
            ],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        return exit_code == 0

    async def get_workspace_heartbeat_statuses(self) -> list[dict]:
        """Return workspace heartbeat payload including live desktop sessions."""
        payload: list[dict] = []
        for info in self._cache.values():
            workspace_id = info.workspace_id
            item = {
                "workspace_id": str(workspace_id),
                "status": info.status,
                "runtime_type": info.runtime_type,
            }

            session = self._desktop_sessions.get(workspace_id)
            if session is not None:
                try:
                    if await self._is_desktop_session_live(workspace_id):
                        item["desktop"] = {
                            "port": session.port,
                            "container_ip": self.get_desktop_container_ip(workspace_id),
                            "network_name": self.get_desktop_network_name(workspace_id),
                        }
                    else:
                        self._desktop_sessions.pop(workspace_id, None)
                        item["desktop"] = None
                        logger.warning(
                            "desktop_session_pruned_from_cache",
                            workspace_id=str(workspace_id),
                        )
                except Exception:
                    self._desktop_sessions.pop(workspace_id, None)
                    item["desktop"] = None
                    logger.exception(
                        "desktop_session_health_check_failed",
                        workspace_id=str(workspace_id),
                    )

            payload.append(item)

        return payload

    async def recover_desktop_sessions_from_runtime(self) -> None:
        """Rebuild in-memory desktop sessions from live runtime state."""
        for workspace_id, info in self._cache.items():
            if info.status != "running" or workspace_id in self._desktop_sessions:
                continue

            try:
                if not await self._is_desktop_session_live(workspace_id):
                    continue
            except Exception:
                logger.exception(
                    "desktop_session_recovery_failed",
                    workspace_id=str(workspace_id),
                )
                continue

            self._desktop_sessions[workspace_id] = DesktopSession(
                workspace_id=workspace_id,
                instance_id=info.instance_id,
            )
            logger.info(
                "desktop_session_recovered",
                workspace_id=str(workspace_id),
            )

    async def get_vm_metrics(self) -> dict[str, dict[str, Any]]:
        """Collect host-observed metrics for QEMU workspaces."""
        qemu_runtime = self._runtimes.get("qemu")
        if qemu_runtime is None:
            return {}

        get_workspace_usage = getattr(qemu_runtime, "get_workspace_usage", None)
        if not callable(get_workspace_usage):
            return {}

        metrics: dict[str, dict[str, Any]] = {}
        for workspace_id, info in self._cache.items():
            if info.runtime_type != "qemu":
                continue
            try:
                usage = await get_workspace_usage(info.instance_id)
            except Exception:
                logger.exception(
                    "vm_metrics_collect_failed",
                    workspace_id=str(workspace_id),
                )
                continue

            if usage is None:
                continue

            metrics[str(workspace_id)] = usage

        return metrics

    # -- interactive terminal --------------------------------------------------

    async def start_terminal(
        self,
        workspace_id: uuid.UUID,
        cols: int = 80,
        rows: int = 24,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        Persistent workspace credentials are sourced via a login shell.
        """
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if not await runtime.workspace_exists(info.instance_id):
            self._cache.pop(workspace_id, None)
            raise RuntimeError("Workspace instance no longer exists")

        terminal_command = [
            "/bin/bash",
            "-lc",
            (
                f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
                f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} "
                ">/dev/null 2>&1; fi; exec /bin/bash -l"
            ),
        ]

        handle = await runtime.exec_pty(
            info.instance_id,
            cols=cols,
            rows=rows,
            workdir="/workspace",
            env={"TERM": "xterm-256color"},
            command=terminal_command,
        )

        terminal_id = str(uuid.uuid4())
        self._terminals[terminal_id] = TerminalSession(
            handle=handle,
            runtime=runtime,
        )
        log.info("terminal_started", terminal_id=terminal_id)
        return terminal_id

    async def read_terminal(self, terminal_id: str) -> AsyncIterator[bytes]:
        """Yield raw bytes from the PTY as they arrive.

        Stops when the PTY is closed or returns empty data (EOF).
        """
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime

        while not handle.closed:
            data = await runtime.pty_read(handle)
            if not data:
                break
            yield data

    async def write_terminal(self, terminal_id: str, data: bytes) -> None:
        """Write raw bytes (user input) to the PTY stdin."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime
        await runtime.pty_write(handle, data)

    async def resize_terminal(
        self, terminal_id: str, cols: int, rows: int
    ) -> None:
        """Resize the PTY window."""
        entry = self._terminals.get(terminal_id)
        if entry is None:
            raise ValueError(f"Terminal {terminal_id} not found")
        handle = entry.handle
        runtime = entry.runtime
        await runtime.pty_resize(handle, cols, rows)

    async def close_terminal(self, terminal_id: str) -> None:
        """Close a PTY session and release resources."""
        entry = self._terminals.pop(terminal_id, None)
        if entry is None:
            return
        await entry.runtime.pty_close(entry.handle)
        logger.info("terminal_closed", terminal_id=terminal_id)

    # -- desktop session (KasmVNC) -----------------------------------------

    async def start_desktop(
        self,
        workspace_id: uuid.UUID,
    ) -> DesktopSession:
        """Start a KasmVNC desktop session inside the workspace container.

        Idempotent: if a session is already running, returns the existing one.
        """
        existing = self._desktop_sessions.get(workspace_id)
        if existing is not None:
            if await self._is_desktop_session_live(workspace_id):
                logger.info("desktop_already_running", workspace_id=str(workspace_id))
                return existing
            self._desktop_sessions.pop(workspace_id, None)
            logger.warning(
                "desktop_cached_session_stale",
                workspace_id=str(workspace_id),
            )

        if await self._is_desktop_session_live(workspace_id):
            recovered = DesktopSession(
                workspace_id=workspace_id,
                instance_id=self._get_cached(workspace_id).instance_id,
            )
            self._desktop_sessions[workspace_id] = recovered
            logger.info(
                "desktop_session_recovered_on_start",
                workspace_id=str(workspace_id),
            )
            return recovered

        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        log = logger.bind(workspace_id=str(workspace_id))

        # Execute the start script inside the container
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            ["/usr/local/bin/opencuria-desktop-start"],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        if exit_code != 0:
            log.error("desktop_start_failed", exit_code=exit_code, output=output)
            raise RuntimeError(f"Failed to start desktop session: {output}")

        session = DesktopSession(
            workspace_id=workspace_id,
            instance_id=info.instance_id,
        )
        self._desktop_sessions[workspace_id] = session
        log.info("desktop_started", port=session.port)
        return session

    async def stop_desktop(self, workspace_id: uuid.UUID) -> None:
        """Stop a running desktop session."""
        session = self._desktop_sessions.pop(workspace_id, None)
        if session is None:
            return

        log = logger.bind(workspace_id=str(workspace_id))
        try:
            runtime = self._get_runtime(workspace_id)
            info = self._get_cached(workspace_id)
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                ["/usr/local/bin/opencuria-desktop-stop"],
            )
            if exit_code != 0:
                log.warning("desktop_stop_nonzero", exit_code=exit_code, output=output)
        except Exception:
            log.exception("desktop_stop_failed")

        log.info("desktop_stopped")

    async def write_desktop_clipboard(self, workspace_id: uuid.UUID, text: str) -> None:
        """Write plain text into the desktop clipboard inside the workspace VM/container."""
        if not await self._is_desktop_session_live(workspace_id):
            raise RuntimeError("Desktop session is not active")

        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        encoded_text = base64.b64encode(text.encode("utf-8")).decode("ascii")
        command = (
            "set -e;"
            "TOOL='';"
            "if command -v xsel >/dev/null 2>&1; then TOOL='xsel'; "
            "elif command -v xclip >/dev/null 2>&1; then TOOL='xclip'; "
            "elif command -v apt-get >/dev/null 2>&1; then "
            "apt-get update >/tmp/opencuria-clipboard-apt.log 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y xclip xsel "
            ">>/tmp/opencuria-clipboard-apt.log 2>&1 || true; "
            "if command -v xsel >/dev/null 2>&1; then TOOL='xsel'; "
            "elif command -v xclip >/dev/null 2>&1; then TOOL='xclip'; fi; "
            "fi; "
            "if [ -z \"$TOOL\" ]; then echo 'clipboard tool missing' >&2; exit 127; fi; "
            f"printf %s '{encoded_text}' | base64 -d | "
            "if [ \"$TOOL\" = 'xsel' ]; then "
            "DISPLAY=:1 xsel --clipboard --input; "
            "else DISPLAY=:1 timeout 3 xclip -selection clipboard -in >/dev/null 2>&1 || true; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            ["sh", "-lc", command],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to write desktop clipboard: {output}")

    async def read_desktop_clipboard(self, workspace_id: uuid.UUID) -> str:
        """Read plain text from the desktop clipboard inside the workspace VM/container."""
        if not await self._is_desktop_session_live(workspace_id):
            raise RuntimeError("Desktop session is not active")

        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        command = (
            "set -e;"
            "TOOL='';"
            "if command -v xclip >/dev/null 2>&1; then TOOL='xclip'; "
            "elif command -v xsel >/dev/null 2>&1; then TOOL='xsel'; "
            "elif command -v apt-get >/dev/null 2>&1; then "
            "apt-get update >/tmp/opencuria-clipboard-apt.log 2>&1 && "
            "DEBIAN_FRONTEND=noninteractive apt-get install -y xclip xsel "
            ">>/tmp/opencuria-clipboard-apt.log 2>&1 || true; "
            "if command -v xclip >/dev/null 2>&1; then TOOL='xclip'; "
            "elif command -v xsel >/dev/null 2>&1; then TOOL='xsel'; fi; "
            "fi; "
            "if [ -z \"$TOOL\" ]; then echo 'clipboard tool missing' >&2; exit 127; fi; "
            "if [ \"$TOOL\" = 'xclip' ]; then "
            "DISPLAY=:1 xclip -selection clipboard -o 2>/dev/null || true; "
            "else DISPLAY=:1 xsel --clipboard --output 2>/dev/null || true; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            ["sh", "-lc", command],
            env={"HOME": "/root", "DISPLAY": ":1"},
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to read desktop clipboard: {output}")
        return output

    def get_desktop_session(self, workspace_id: uuid.UUID) -> DesktopSession | None:
        """Return the active desktop session if any."""
        return self._desktop_sessions.get(workspace_id)

    def get_desktop_container_ip(self, workspace_id: uuid.UUID) -> str:
        """Get the upstream IP address for the workspace desktop proxy."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not hasattr(runtime, "get_container_ip"):
            raise RuntimeError("Runtime does not support desktop proxy")
        return runtime.get_container_ip(info.instance_id, str(workspace_id))

    def get_desktop_network_name(self, workspace_id: uuid.UUID) -> str:
        """Get the backend-attachable network for a workspace desktop proxy."""
        runtime = self._get_runtime(workspace_id)
        if not hasattr(runtime, "get_workspace_network_name"):
            raise RuntimeError("Runtime does not support desktop networking")
        return runtime.get_workspace_network_name(str(workspace_id))

    # -- file operations -------------------------------------------------------

    @staticmethod
    def _sanitize_path(path: str) -> str:
        """Ensure *path* is under ``/workspace`` and prevent traversal."""
        normalized = os.path.normpath(path)
        if normalized != "/workspace" and not normalized.startswith("/workspace/"):
            raise ValueError(f"Path must be under /workspace: {path}")
        return normalized

    @staticmethod
    def _sanitize_filename(filename: str) -> str:
        """Validate and return a safe filename for workspace uploads."""
        if not filename:
            raise ValueError("Filename must not be empty")

        if filename != os.path.basename(filename):
            raise ValueError("Filename must not contain path separators")

        if filename in {".", ".."}:
            raise ValueError("Invalid filename")

        return filename

    @staticmethod
    def _build_single_file_tar(filename: str, content: bytes) -> bytes:
        """Build a tar archive containing exactly one file."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    @staticmethod
    def _convert_archive_to_tar(content: bytes) -> bytes:
        """Convert an uploaded archive payload to a plain tar stream."""
        source = io.BytesIO(content)
        target = io.BytesIO()

        with tarfile.open(fileobj=source, mode="r:*") as src_tar:
            with tarfile.open(fileobj=target, mode="w") as dst_tar:
                for member in src_tar.getmembers():
                    if member.name.startswith("/") or ".." in member.name.split("/"):
                        raise ValueError("Archive contains unsafe paths")
                    if member.issym() or member.islnk():
                        raise ValueError("Archive contains unsafe links")

                    extracted = None
                    if member.isfile():
                        extracted = src_tar.extractfile(member)
                    dst_tar.addfile(member, extracted)

        return target.getvalue()

    async def list_files(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> list[dict]:
        """List files and directories at *path* inside the workspace.

        Returns a list of dicts with ``name``, ``path``, ``type``, ``size``.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=[
                "find", safe_path,
                "-maxdepth", "1", "-mindepth", "1",
                "-printf", r"%y\t%s\t%p\n",
            ],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to list files: {output}")

        entries: list[dict] = []
        for line in output.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            file_type_char, size_str, file_path = parts
            entries.append({
                "name": os.path.basename(file_path),
                "path": file_path,
                "type": "directory" if file_type_char == "d" else "file",
                "size": int(size_str) if size_str.isdigit() else 0,
            })

        # Sort: directories first, then alphabetically
        entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))
        return entries

    async def read_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        max_size: int | None = None,
    ) -> dict:
        """Read a file from the workspace container.

        Returns a dict with ``content`` (base64), ``size``, ``truncated``,
        and ``mime_type``.

        Concurrent reads are throttled via a per-workspace semaphore to
        avoid exceeding the SSH server's MaxSessions limit when many images
        are fetched simultaneously.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # One semaphore per workspace; created lazily.
        sem = self._file_read_semaphores.get(workspace_id)
        if sem is None:
            sem = asyncio.Semaphore(4)
            self._file_read_semaphores[workspace_id] = sem

        if max_size is None:
            read_limit = FILE_READ_DEFAULT_MAX_SIZE
        else:
            read_limit = int(max_size)
            if read_limit <= 0:
                raise ValueError("max_size must be a positive integer")
            if read_limit > FILE_READ_ABSOLUTE_MAX_SIZE:
                raise ValueError(
                    f"max_size exceeds allowed maximum ({FILE_READ_ABSOLUTE_MAX_SIZE} bytes)"
                )

        async with sem:
            # Combine stat + read into a single SSH exec to halve the number
            # of SSH channels opened compared to two sequential commands.
            # Output format:
            #   line 1 = file size (bytes)
            #   line 2 = MIME type
            #   rest   = base64 content
            # Single quotes in safe_path are already prevented by _sanitize_path
            # (normpath keeps paths clean), so direct interpolation is safe here.
            shell_cmd = (
                # Guard: exit 1 immediately if the file does not exist.
                # Without this, the else-branch's `head | base64` pipeline
                # exits 0 even on a missing file, causing a ValueError when
                # we try to parse the empty first line as an integer.
                f"test -f '{safe_path}' || exit 1; "
                f"SZ=$(stat -c '%s' '{safe_path}'); "
                f"MT=$(file --mime-type -b '{safe_path}' 2>/dev/null || echo 'application/octet-stream'); "
                f"echo \"$SZ\"; "
                f"echo \"$MT\"; "
                f"if [ \"$SZ\" -le {read_limit} ]; then "
                f"  base64 '{safe_path}'; "
                f"else "
                f"  head -c {read_limit} '{safe_path}' | base64; "
                f"fi"
            )
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["sh", "-c", shell_cmd],
                workdir="/workspace",
            )

        if exit_code != 0:
            raise RuntimeError(f"Failed to read file: {output}")

        # Parse output: first line is size, second line MIME type, remainder base64.
        lines = output.splitlines()
        if len(lines) < 2:
            raise RuntimeError("Invalid file read response format")
        file_size = int(lines[0].strip())
        mime_type = lines[1].strip() or "application/octet-stream"
        content_output = "\n".join(lines[2:]) if len(lines) > 2 else ""
        truncated = file_size > read_limit

        return {
            "content": content_output.strip(),
            "size": file_size,
            "truncated": truncated,
            "mime_type": mime_type,
        }

    async def upload_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
        filename: str,
        content_b64: str,
        is_directory: bool = False,
    ) -> None:
        """Upload a file into the workspace container.

        Args:
            workspace_id: Target workspace.
            path: Directory path to upload into.
            filename: Name of the file to create.
            content_b64: Base64-encoded file content.
            is_directory: If True, content is a tar.gz archive to extract.
        """
        safe_path = self._sanitize_path(path)
        safe_filename = self._sanitize_filename(filename)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # Check upload size
        raw_size = len(content_b64) * 3 // 4  # approximate decoded size
        if raw_size > FILE_UPLOAD_MAX_SIZE:
            raise ValueError(
                f"Upload exceeds maximum size of {FILE_UPLOAD_MAX_SIZE} bytes"
            )

        # Ensure target directory exists
        await runtime.exec_command_wait(
            info.instance_id,
            command=["mkdir", "-p", safe_path],
            workdir="/workspace",
        )

        try:
            decoded_content = base64.b64decode(content_b64, validate=True)
        except Exception as exc:  # pragma: no cover - safety net
            raise ValueError("Invalid base64 upload payload") from exc

        if is_directory:
            archive_data = self._convert_archive_to_tar(decoded_content)
        else:
            archive_data = self._build_single_file_tar(safe_filename, decoded_content)

        await runtime.put_archive(
            info.instance_id,
            safe_path,
            archive_data,
        )

        logger.info(
            "file_uploaded",
            workspace_id=str(workspace_id),
            path=safe_path,
            filename=safe_filename,
        )

    async def download_file(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Download a file or directory from the workspace container.

        Returns a dict with ``content`` (base64), ``filename``, ``is_archive``.
        For directories, the content is a tar.gz archive.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        # Check if it's a directory
        exit_code, _ = await runtime.exec_command_wait(
            info.instance_id,
            command=["test", "-d", safe_path],
            workdir="/workspace",
        )
        is_dir = exit_code == 0

        if is_dir:
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=[
                    "sh", "-c",
                    f"tar czf - -C '{os.path.dirname(safe_path)}' "
                    f"'{os.path.basename(safe_path)}' | base64",
                ],
                workdir="/workspace",
            )
            filename = os.path.basename(safe_path) + ".tar.gz"
        else:
            exit_code, output = await runtime.exec_command_wait(
                info.instance_id,
                command=["base64", safe_path],
                workdir="/workspace",
            )
            filename = os.path.basename(safe_path)

        if exit_code != 0:
            raise RuntimeError(f"Failed to download: {output}")

        return {
            "content": output.strip(),
            "filename": filename,
            "is_archive": is_dir,
        }

    async def stat_path(
        self,
        workspace_id: uuid.UUID,
        path: str,
    ) -> dict:
        """Stat a path inside the workspace container.

        Returns a dict with ``path``, ``is_dir``, ``size``, ``mime_type``.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        shell_cmd = (
            f"if [ -e '{safe_path}' ]; then "
            f"if [ -d '{safe_path}' ]; then echo 'dir'; "
            f"du -sb '{safe_path}' | cut -f1; "
            f"echo 'inode/directory'; "
            f"else stat -c '%s' '{safe_path}'; "
            f"file --mime-type -b '{safe_path}' 2>/dev/null "
            "|| echo 'application/octet-stream'; "
            f"fi; else echo 'missing'; fi"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["sh", "-c", shell_cmd],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to stat path: {output}")
        lines = output.strip().splitlines()
        if not lines or lines[0].strip() == "missing":
            raise FileNotFoundError(f"No such file or directory: {path}")
        is_dir = lines[0].strip() == "dir"
        size = int(lines[1].strip()) if len(lines) > 1 else 0
        mime_type = (
            lines[2].strip() if len(lines) > 2 else "application/octet-stream"
        )
        return {
            "path": safe_path,
            "is_dir": is_dir,
            "size": size,
            "mime_type": mime_type,
        }

    async def write_file_content(
        self,
        workspace_id: uuid.UUID,
        path: str,
        content_b64: str,
        mode: int = 0o644,
    ) -> None:
        """Write file content atomically inside the workspace container.

        Args:
            workspace_id: Target workspace.
            path: Absolute path under ``/workspace``.
            content_b64: Base64-encoded file content.
            mode: File permission bits applied after the write.
        """
        safe_path = self._sanitize_path(path)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        try:
            decoded = base64.b64decode(content_b64, validate=True)
        except Exception as exc:
            raise ValueError("Invalid base64 file payload") from exc
        if mode < 0 or mode > 0o777:
            raise ValueError(f"Invalid file mode: {mode!r}")

        archive = self._build_single_file_tar(
            os.path.basename(safe_path), decoded
        )
        await runtime.put_archive(
            info.instance_id,
            os.path.dirname(safe_path) or "/workspace",
            archive,
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["chmod", format(mode, "o"), safe_path],
            workdir="/workspace",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to set file mode: {output}")
        logger.info(
            "file_written",
            workspace_id=str(workspace_id),
            path=safe_path,
        )

    async def exec_harness_command(
        self,
        workspace_id: uuid.UUID,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Execute a harness command with separated stdout and stderr.

        Runs the command via a shell wrapper that multiplexes the two
        streams into tagged base64 frames, then decodes them back into
        separate buffers. Returns ``(exit_code, stdout, stderr)``.
        """
        safe_workdir = self._sanitize_path(workdir)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if isinstance(command, str):
            argv: list[str] = ["bash", "-lc", command]
        else:
            argv = [str(arg) for arg in command]
            if not argv:
                raise ValueError("command must not be empty")
        marker_out = "OPENCURIA_STDOUT"
        marker_err = "OPENCURIA_STDERR"
        inner = " ".join(shlex.quote(arg) for arg in argv)
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi; "
        )
        wrapper = (
            f"{source}"
            f"__oc_out=$(mktemp); __oc_err=$(mktemp); "
            f"sh -c {shlex.quote(inner)} >\"$__oc_out\" 2>\"$__oc_err\"; "
            f"__oc_code=$?; "
            f"echo {marker_out}; base64 \"$__oc_out\"; "
            f"echo {marker_err}; base64 \"$__oc_err\"; "
            f"echo \"EXIT:$__oc_code\"; rm -f \"$__oc_out\" \"$__oc_err\"; "
            f"exit $__oc_code"
        )
        exit_code, output = await runtime.exec_command_wait(
            info.instance_id,
            command=["sh", "-lc", wrapper],
            workdir=safe_workdir,
            env=env,
        )
        stdout, stderr = self._parse_harness_exec_output(output)
        return exit_code, stdout, stderr

    async def exec_harness_command_stream(
        self,
        workspace_id: uuid.UUID,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
    ):
        """Execute a harness command and yield ``(stream, data)`` chunks.

        Yields ``("stdout", text)`` / ``("stderr", text)`` tuples while the
        command runs, then a final ``("exit", str(exit_code))`` tuple.
        """
        safe_workdir = self._sanitize_path(workdir)
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if isinstance(command, str):
            argv: list[str] = ["bash", "-lc", command]
        else:
            argv = [str(arg) for arg in command]
            if not argv:
                raise ValueError("command must not be empty")
        marker_out = "OPENCURIA_LINE_STDOUT:"
        marker_err = "OPENCURIA_LINE_STDERR:"
        inner = " ".join(shlex.quote(arg) for arg in argv)
        source = (
            f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} ]; then "
            f". {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)}; fi; "
        )
        # Portable fifo-based streaming wrapper: multiplexes the child
        # stdout/stderr into tagged lines on the combined output stream,
        # then reports the exit code on the last line.
        portable = (
            f"{source}"
            "__oc_dir=$(mktemp -d); "
            "__oc_o=$__oc_dir/o; __oc_e=$__oc_dir/e; "
            "mkfifo \"$__oc_o\" \"$__oc_e\"; "
            f"(sh -c {shlex.quote(inner)} "
            ">\"$__oc_o\" 2>\"$__oc_e\"; echo $? >\"$__oc_dir/code\") & "
            "__oc_pid=$!; "
            "(while IFS= read -r __oc_l; do "
            f"printf '{marker_out}%s\\n' \"$__oc_l\"; "
            "done <\"$__oc_o\" & "
            "while IFS= read -r __oc_m; do "
            f"printf '{marker_err}%s\\n' \"$__oc_m\"; "
            "done <\"$__oc_e\" & wait); "
            "wait $__oc_pid; __oc_code=$(cat \"$__oc_dir/code\"); "
            "rm -rf \"$__oc_dir\"; "
            "echo \"OPENCURIA_EXIT:$__oc_code\""
        )
        exit_code = 0
        async for line in runtime.exec_command(
            info.instance_id,
            command=["sh", "-lc", portable],
            workdir=safe_workdir,
            env=env,
        ):
            if line.startswith(marker_out):
                yield ("stdout", line[len(marker_out):])
            elif line.startswith(marker_err):
                yield ("stderr", line[len(marker_err):])
            elif line.startswith("OPENCURIA_EXIT:"):
                exit_code = int(line.split(":", 1)[1].strip() or 0)
                yield ("exit", str(exit_code))
            else:
                yield ("stdout", line)

    @staticmethod
    def _parse_harness_exec_output(output: str) -> tuple[str, str]:
        """Split tagged exec wrapper output into (stdout, stderr)."""
        marker_out = "OPENCURIA_STDOUT"
        marker_err = "OPENCURIA_STDERR"
        if marker_out not in output or marker_err not in output:
            return output, ""
        stdout_b64 = output.split(marker_out, 1)[1].split(marker_err, 1)[0]
        remainder = output.split(marker_err, 1)[1]
        stderr_b64 = remainder.split("EXIT:", 1)[0]
        import base64 as _b64

        def _decode(payload: str) -> str:
            cleaned = "".join(payload.split())
            if not cleaned:
                return ""
            return _b64.b64decode(cleaned).decode("utf-8", errors="replace")

        return _decode(stdout_b64), _decode(stderr_b64)

    # ── Image artifact operations ─────────────────────────────────────

    async def build_image(
        self,
        *,
        runtime_type: str,
        build_job_id: str,
        dockerfile_content: str = "",
        image_tag: str = "",
        base_distro: str = "",
        init_script: str = "",
        image_path: str = "",
        progress_callback=None,
    ) -> dict[str, str]:
        """Build runtime image from definition payload.

        Returns a dict containing ``image_tag`` and/or ``image_path``.
        """
        if runtime_type == "docker":
            if not dockerfile_content.strip():
                raise RuntimeError("dockerfile_content is required for docker image builds")
            if not image_tag.strip():
                raise RuntimeError("image_tag is required for docker image builds")
            try:
                import docker  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("docker SDK is not available") from exc

            context_stream = io.BytesIO()
            with tarfile.open(fileobj=context_stream, mode="w") as tar:
                df_bytes = dockerfile_content.encode("utf-8")
                df_info = tarfile.TarInfo(name="Dockerfile")
                df_info.size = len(df_bytes)
                tar.addfile(df_info, io.BytesIO(df_bytes))

            context_stream.seek(0)
            client = docker.from_env()
            image, logs = await asyncio.to_thread(
                client.images.build,
                fileobj=context_stream,
                custom_context=True,
                rm=True,
                tag=image_tag,
                pull=False,
                forcerm=True,
            )
            for entry in logs:
                if progress_callback is None:
                    continue
                line = ""
                if isinstance(entry, dict):
                    line = str(entry.get("stream") or entry.get("status") or "").strip()
                else:
                    line = str(entry).strip()
                if line:
                    await progress_callback(line)
            return {"image_tag": image_tag}

        if runtime_type == "qemu":
            if not image_path.strip():
                raise RuntimeError("image_path is required for qemu image builds")
            runtime = self._get_runtime_by_type("qemu")
            build_image = getattr(runtime, "build_image", None)
            if build_image is None:
                raise RuntimeError("QEMU runtime does not support image builds")
            return await build_image(
                base_distro=base_distro,
                init_script=init_script,
                image_path=image_path,
                progress_callback=progress_callback,
            )

        raise RuntimeError(f"Unsupported runtime_type for image build: {runtime_type}")

    async def create_image_artifact(
        self,
        workspace_id: uuid.UUID,
        name: str,
    ) -> "ImageArtifactInfo":
        """Create an image artifact from a workspace.

        The runtime must support artifact capture.
        """
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' does not support image artifact capture"
            )
        artifact = await runtime.create_image_artifact(info.instance_id, name)
        logger.info(
            "image_artifact_created",
            workspace_id=str(workspace_id),
            image_artifact_id=artifact.artifact_id,
            name=name,
        )
        return artifact

    async def list_image_artifacts(
        self,
        workspace_id: uuid.UUID,
    ) -> list["ImageArtifactInfo"]:
        """List all captured image artifacts for a workspace."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            return []
        return await runtime.list_image_artifacts(info.instance_id)

    async def delete_image_artifact(
        self,
        workspace_id: uuid.UUID,
        image_artifact_id: str,
    ) -> None:
        """Delete a captured image artifact."""
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{info.runtime_type}' does not support image artifact deletion"
            )
        await runtime.delete_image_artifact(image_artifact_id)
        logger.info(
            "image_artifact_deleted",
            workspace_id=str(workspace_id),
            image_artifact_id=image_artifact_id,
        )

    async def delete_image_reference(
        self,
        *,
        runtime_type: str,
        image_ref: str,
    ) -> str:
        """Delete a concrete runtime image reference without requiring a workspace.

        Returns 'deleted' or 'already_absent' to indicate the result.
        """
        if runtime_type == "docker":
            if not image_ref.strip():
                raise RuntimeError("image_ref is required for docker image deletion")
            try:
                import docker  # type: ignore[import-not-found]
                from docker.errors import ImageNotFound  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("docker SDK is not available") from exc

            client = docker.from_env()
            try:
                await asyncio.to_thread(client.images.remove, image=image_ref, force=True)
                logger.info("docker_image_deleted", image_ref=image_ref)
                return "deleted"
            except ImageNotFound:
                logger.info("docker_image_already_absent", image_ref=image_ref)
                return "already_absent"

        if runtime_type == "qemu":
            if not image_ref.strip():
                raise RuntimeError("image_ref is required for qemu image deletion")
            runtime = self._get_runtime_by_type("qemu")
            try:
                await runtime.delete_image_artifact(image_ref)
                logger.info("qemu_image_deleted", image_ref=image_ref)
                return "deleted"
            except FileNotFoundError:
                logger.info("qemu_image_already_absent", image_ref=image_ref)
                return "already_absent"

        raise RuntimeError(f"Unsupported runtime_type for image deletion: {runtime_type}")

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
        runtime_type: str,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        files: list[dict[str, Any]] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> tuple[uuid.UUID, bool]:
        """Create a workspace from an image artifact and inject credentials.

        Credentials remain on disk until a controlled stop.
        """
        runtime = self._get_runtime_by_type(runtime_type)
        if not runtime.supports_image_artifacts:
            raise RuntimeError(
                f"Runtime '{runtime_type}' does not support image artifact cloning"
            )

        instance_id = await runtime.create_workspace_from_image_artifact(
            image_artifact_id,
            str(new_workspace_id),
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )

        self._cache[new_workspace_id] = WorkspaceInfo(
            workspace_id=new_workspace_id,
            instance_id=instance_id,
            status="running",
            runtime_type=runtime_type,
        )

        log = logger.bind(
            workspace_id=str(new_workspace_id),
            image_artifact_id=image_artifact_id,
            runtime_type=runtime_type,
        )
        log.info("workspace_created_from_image_artifact")

        credentials_present = await self.inject_workspace_credentials(
            runtime,
            instance_id,
            env_vars,
            files,
            ssh_keys,
            log,
        )
        return new_workspace_id, credentials_present
