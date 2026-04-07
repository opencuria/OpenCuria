"""Central business logic for workspace management.

The runner is a "dumb executor" — it receives structured commands
from the backend and runs them inside workspace environments.  All agent
knowledge (configure commands, run command templates) lives in the
backend database.

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
from .models import WorkspaceInfo
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


@dataclass(frozen=True)
class OperationCredentialContext:
    """Per-operation credential material prepared inside a workspace."""

    directory: str
    bootstrap_script: str
    environment: dict[str, str]

    def build_bootstrap_snippet(
        self,
        extra_env: dict[str, str] | None = None,
    ) -> str:
        """Return a shell snippet which activates this credential context."""

        exports = [
            f"export OPENCURIA_CREDENTIAL_CONTEXT_DIR={shlex.quote(self.directory)}",
            f". {shlex.quote(self.bootstrap_script)}",
        ]
        for key, value in (extra_env or {}).items():
            exports.append(f"export {key}={shlex.quote(str(value))}")
        return "; ".join(exports)


@dataclass
class TerminalSession:
    """Runtime PTY handle plus optional credential context."""

    handle: PtyHandle
    runtime: RuntimeBackend
    credential_context: OperationCredentialContext | None = None


@dataclass
class PreparedOperation:
    """Resolved workspace target plus reusable credential context."""

    workspace_id: uuid.UUID
    instance_id: str
    runtime: RuntimeBackend
    log: Any
    credential_context: OperationCredentialContext | None = None


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
        credential_context: OperationCredentialContext | None = None,
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
        wrapped_command = self._wrap_command_with_context(
            command,
            credential_context,
        )
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
        credential_context: OperationCredentialContext | None = None,
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
        wrapped_command = self._wrap_command_with_context(
            command,
            credential_context,
        )
        command_args = self._normalise_command_args(wrapped_command["args"])
        async for line in runtime.exec_command(
            instance_id,
            command=command_args,
            workdir=wrapped_command.get("workdir"),
            env=wrapped_command.get("env"),
        ):
            yield line

    # -- operation-scoped credentials -----------------------------------------

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

    async def _create_operation_credential_context(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        ssh_keys: list[str] | None,
        log,
    ) -> OperationCredentialContext | None:
        """Create a temporary credential context for a single operation."""

        env_vars = env_vars or {}
        ssh_keys = ssh_keys or []
        if not env_vars and not ssh_keys:
            return None

        context_id = str(uuid.uuid4())
        context_dir = f"/tmp/opencuria-op-{context_id}"
        ssh_dir = f"{context_dir}/ssh"
        bin_dir = f"{context_dir}/bin"
        bootstrap_path = f"{context_dir}/bootstrap.sh"
        files: list[tuple[str, bytes, int]] = []
        operation_env = {
            "OPENCURIA_CREDENTIAL_CONTEXT_DIR": context_dir,
        }

        bootstrap_lines = [
            "#!/bin/sh",
            "set -eu",
            'export PATH="/root/.local/bin:$PATH"',
        ]
        operation_env["PATH"] = (
            "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        for key, value in env_vars.items():
            bootstrap_lines.append(
                f"export {key}={shlex.quote(str(value))}"
            )
            operation_env[key] = str(value)

        if ssh_keys:
            known_hosts_path = f"{ssh_dir}/known_hosts"
            config_path = f"{ssh_dir}/config"
            config_lines = [
                "Host *",
                "    StrictHostKeyChecking accept-new",
                f"    UserKnownHostsFile {known_hosts_path}",
                "    IdentitiesOnly yes",
            ]
            for index, key_pem in enumerate(ssh_keys):
                key_name = "id_ed25519" if index == 0 else f"id_ed25519_{index + 1}"
                key_relpath = f"ssh/{key_name}"
                key_abspath = f"{ssh_dir}/{key_name}"
                config_lines.append(f"    IdentityFile {key_abspath}")
                files.append((key_relpath, key_pem.rstrip().encode("utf-8") + b"\n", 0o600))

            files.append(("ssh/known_hosts", b"", 0o600))
            files.append(
                ("ssh/config", ("\n".join(config_lines) + "\n").encode("utf-8"), 0o600)
            )

            ssh_wrapper = (
                "#!/bin/sh\n"
                f"exec /usr/bin/ssh -F {shlex.quote(config_path)} \"$@\"\n"
            ).encode("utf-8")
            scp_wrapper = (
                "#!/bin/sh\n"
                f"exec /usr/bin/scp -F {shlex.quote(config_path)} \"$@\"\n"
            ).encode("utf-8")
            sftp_wrapper = (
                "#!/bin/sh\n"
                f"exec /usr/bin/sftp -F {shlex.quote(config_path)} \"$@\"\n"
            ).encode("utf-8")
            files.extend(
                [
                    ("bin/ssh", ssh_wrapper, 0o755),
                    ("bin/scp", scp_wrapper, 0o755),
                    ("bin/sftp", sftp_wrapper, 0o755),
                ]
            )
            bootstrap_lines.extend(
                [
                    f'export PATH={shlex.quote(bin_dir)}:"$PATH"',
                    f"export GIT_SSH_COMMAND={shlex.quote(f'{bin_dir}/ssh')}",
                    "export GIT_SSH_VARIANT=ssh",
                ]
            )
            operation_env.update(
                {
                    "PATH": (
                        f"{bin_dir}:/root/.local/bin:/usr/local/sbin:"
                        "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                    ),
                    "GIT_SSH_COMMAND": f"{bin_dir}/ssh",
                    "GIT_SSH_VARIANT": "ssh",
                }
            )

        bootstrap_content = ("\n".join(bootstrap_lines) + "\n").encode("utf-8")
        files.append(("bootstrap.sh", bootstrap_content, 0o700))

        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["mkdir", "-p", context_dir],
            workdir="/tmp",
        )
        if exit_code != 0:
            raise RuntimeError(f"Failed to create credential context: {output}")

        archive_data = self._build_tar_entries(files)
        await runtime.put_archive(instance_id, context_dir, archive_data)
        log.info(
            "operation_credential_context_created",
            has_env=bool(env_vars),
            ssh_key_count=len(ssh_keys),
            context_dir=context_dir,
        )
        return OperationCredentialContext(
            directory=context_dir,
            bootstrap_script=bootstrap_path,
            environment=operation_env,
        )

    async def _cleanup_operation_credential_context(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        context: OperationCredentialContext | None,
        log,
    ) -> None:
        """Best-effort cleanup of a temporary operation credential context."""

        if context is None:
            return

        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["rm", "-rf", context.directory],
            workdir="/tmp",
        )
        if exit_code != 0:
            log.warning(
                "operation_credential_context_cleanup_failed",
                context_dir=context.directory,
                output=output,
            )
            return

        log.info(
            "operation_credential_context_removed",
            context_dir=context.directory,
        )

    async def _cleanup_legacy_workspace_credentials(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        log,
    ) -> None:
        """Remove legacy persistent credential files left by older runners."""

        cleanup_script = """
rm -f /root/.opencuria-env.sh /etc/profile.d/opencuria-env.sh
if [ -f /root/.bashrc ]; then
  tmp_bashrc=$(mktemp)
  grep -vxF 'test -f /root/.opencuria-env.sh && . /root/.opencuria-env.sh' /root/.bashrc > "$tmp_bashrc" || true
  cat "$tmp_bashrc" > /root/.bashrc
  rm -f "$tmp_bashrc"
fi
rm -f /root/.ssh/id_ed25519 /root/.ssh/id_ed25519_*
rm -f /root/.ssh/config /root/.ssh/known_hosts
find /var/lib/cloud/instances -type f \\( -name 'user-data.txt' -o -name 'user-data.txt.i' -o -name 'cloud-config.txt' -o -path '*/scripts/runcmd' \\) -delete 2>/dev/null || true
"""
        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=["sh", "-lc", cleanup_script],
            workdir="/root",
        )
        if exit_code != 0:
            log.warning("legacy_credential_cleanup_failed", output=output)
            return
        log.info("legacy_credential_cleanup_complete")

    def _wrap_command_with_context(
        self,
        command: dict,
        credential_context: OperationCredentialContext | None,
    ) -> dict:
        """Wrap command execution so the credential context is sourced first."""

        if credential_context is None:
            return command

        normalised_args = self._normalise_command_args(command["args"])
        wrapper = (
            f"{credential_context.build_bootstrap_snippet(command.get('env') or {})}; "
            "exec \"$@\""
        )
        return {
            **command,
            "args": [
                "bash",
                "-lc",
                wrapper,
                "opencuria-operation",
                *normalised_args,
            ],
            "env": {},
        }

    # -- workspace lifecycle ---------------------------------------------------

    async def create_workspace(
        self,
        repos: list[str],
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        configure_commands: list[dict] | None = None,
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        workspace_id: uuid.UUID | None = None,
        runtime_type: str = "docker",
        image_tag: str | None = None,
        base_image_path: str | None = None,
    ) -> uuid.UUID:
        """Create a new workspace, clone repos, run configure commands.

        Args:
            repos: Git repository URLs to clone into the workspace.
            configure_commands: List of structured command dicts from the
                backend, each with ``args``, ``workdir``, ``env``,
                ``description`` keys.
            env_vars: Optional environment variables available during initial
                repository clone/configure steps.
            ssh_keys: Optional SSH private keys available during initial
                repository clone/configure steps.
            workspace_id: Workspace ID assigned by the backend.
            runtime_type: Which runtime to use (``"docker"`` or ``"qemu"``).

        Returns the workspace UUID.
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

        await self._cleanup_legacy_workspace_credentials(
            runtime,
            instance_id,
            log,
        )

        credential_context = await self._create_operation_credential_context(
            runtime,
            instance_id,
            env_vars,
            ssh_keys,
            log,
        )

        try:
            # Clone repositories
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
                    credential_context=credential_context,
                )
                if exit_code != 0:
                    log.warning("repo_clone_failed", repo=repo_url, output=output)
                else:
                    log.info("repo_cloned", repo=repo_url)

            # Run configure commands from the backend
            for cmd in configure_commands or []:
                log.info("configure_step", description=cmd.get("description", ""))
                exit_code, output = await self._exec_command(
                    runtime,
                    instance_id,
                    cmd,
                    credential_context=credential_context,
                )
                if exit_code != 0:
                    log.warning(
                        "configure_step_failed",
                        description=cmd.get("description", ""),
                        output=output,
                    )
        finally:
            await self._cleanup_operation_credential_context(
                runtime,
                instance_id,
                credential_context,
                log,
            )

        # Update cache status
        self._cache[workspace_id].status = "running"

        log.info("workspace_ready")
        return workspace_id

    async def prepare_operation(
        self,
        workspace_id: uuid.UUID,
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> PreparedOperation:
        """Resolve runtime target and create a reusable credential context."""

        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")
        if not await runtime.workspace_exists(info.instance_id):
            self._cache.pop(workspace_id, None)
            raise RuntimeError("Workspace instance no longer exists")

        await self._cleanup_legacy_workspace_credentials(
            runtime,
            info.instance_id,
            log,
        )

        credential_context = await self._create_operation_credential_context(
            runtime,
            info.instance_id,
            env_vars,
            ssh_keys,
            log,
        )
        return PreparedOperation(
            workspace_id=workspace_id,
            instance_id=info.instance_id,
            runtime=runtime,
            log=log,
            credential_context=credential_context,
        )

    async def cleanup_operation(self, prepared: PreparedOperation) -> None:
        """Remove any temporary credential material for a prepared operation."""

        await self._cleanup_operation_credential_context(
            prepared.runtime,
            prepared.instance_id,
            prepared.credential_context,
            prepared.log,
        )

    async def run_configure_commands(
        self,
        workspace_id: uuid.UUID,
        configure_commands: list[dict],
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        prepared: PreparedOperation | None = None,
    ) -> None:
        """Run configure commands in an existing workspace (agent first-time setup).

        This is called before the first prompt of a new agent in a workspace,
        allowing the agent to be installed/configured without a workspace restart.

        Args:
            workspace_id: Target workspace UUID.
            configure_commands: List of structured command dicts from the backend.
        """
        own_prepared = prepared is None
        prepared = prepared or await self.prepare_operation(
            workspace_id,
            env_vars=env_vars,
            ssh_keys=ssh_keys,
        )
        log = prepared.log

        try:
            for cmd in configure_commands:
                log.info("configure_step", description=cmd.get("description", ""))
                exit_code, output = await self._exec_command(
                    prepared.runtime,
                    prepared.instance_id,
                    cmd,
                    credential_context=prepared.credential_context,
                )
                if exit_code != 0:
                    log.warning(
                        "configure_step_failed",
                        description=cmd.get("description", ""),
                        output=output,
                    )
            log.info("configure_commands_complete", count=len(configure_commands))
        finally:
            if own_prepared:
                await self.cleanup_operation(prepared)

    async def run_command(
        self,
        workspace_id: uuid.UUID,
        command: dict,
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        prepared: PreparedOperation | None = None,
    ) -> AsyncIterator[str]:
        """Execute a command in a workspace and stream output lines."""
        own_prepared = prepared is None
        prepared = prepared or await self.prepare_operation(
            workspace_id,
            env_vars=env_vars,
            ssh_keys=ssh_keys,
        )
        log = prepared.log

        log.info(
            "running_command",
            description=command.get("description", ""),
        )
        try:
            async for line in self._exec_command_stream(
                prepared.runtime,
                prepared.instance_id,
                command,
                credential_context=prepared.credential_context,
            ):
                yield line
            log.info("command_completed")
        finally:
            if own_prepared:
                await self.cleanup_operation(prepared)

    async def run_command_wait(
        self,
        workspace_id: uuid.UUID,
        command: dict,
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        prepared: PreparedOperation | None = None,
    ) -> tuple[int, str]:
        """Execute a command in a workspace and return exit code + output.

        Args:
            workspace_id: Target workspace UUID.
            command: Structured command dict from the backend with
                ``args``, ``workdir``, ``env``, ``description`` keys.
        """
        own_prepared = prepared is None
        prepared = prepared or await self.prepare_operation(
            workspace_id,
            env_vars=env_vars,
            ssh_keys=ssh_keys,
        )
        log = prepared.log

        log.info(
            "running_command",
            description=command.get("description", ""),
        )
        try:
            exit_code, output = await self._exec_command(
                prepared.runtime,
                prepared.instance_id,
                command,
                credential_context=prepared.credential_context,
            )
            log.info("command_completed", exit_code=exit_code)
            return exit_code, output
        finally:
            if own_prepared:
                await self.cleanup_operation(prepared)

    async def terminate_prompt_process(
        self,
        workspace_id: uuid.UUID,
        pid_file: str,
    ) -> None:
        """Terminate a tracked prompt process by PID file."""
        kill_script = self._build_prompt_termination_script(pid_file)
        await self.run_command_wait(
            workspace_id,
            {
                "args": ["sh", "-lc", kill_script],
                "description": "Terminate active prompt process",
            },
        )

    @staticmethod
    def _build_prompt_termination_script(pid_file: str) -> str:
        """Return shell script that terminates a prompt PID and its process group."""

        quoted_pid_file = shlex.quote(pid_file)
        return (
            f"pid_file={quoted_pid_file}; "
            "if [ ! -f \"$pid_file\" ]; then exit 0; fi; "
            "pid=$(tr -d '[:space:]' < \"$pid_file\" 2>/dev/null || true); "
            "if [ -z \"$pid\" ]; then rm -f \"$pid_file\"; exit 0; fi; "
            "pgid=$(ps -o pgid= -p \"$pid\" 2>/dev/null | tr -d '[:space:]' || true); "
            "if [ -n \"$pgid\" ]; then "
            "/bin/kill -TERM -- \"-$pgid\" 2>/dev/null || true; "
            "pkill -TERM -g \"$pgid\" 2>/dev/null || true; "
            "fi; "
            "/bin/kill -TERM \"$pid\" 2>/dev/null || true; "
            "sleep 1; "
            "if [ -n \"$pgid\" ]; then "
            "/bin/kill -KILL -- \"-$pgid\" 2>/dev/null || true; "
            "pkill -KILL -g \"$pgid\" 2>/dev/null || true; "
            "fi; "
            "/bin/kill -KILL \"$pid\" 2>/dev/null || true; "
            "rm -f \"$pid_file\""
        )

    async def cleanup_prompt_process_tracking(
        self,
        workspace_id: uuid.UUID,
        pid_file: str,
    ) -> None:
        """Remove prompt PID tracking file if it still exists."""
        cleanup_script = f"rm -f '{pid_file}'"
        await self.run_command_wait(
            workspace_id,
            {
                "args": ["sh", "-lc", cleanup_script],
                "description": "Cleanup prompt PID tracking",
            },
        )

    async def stop_workspace(self, workspace_id: uuid.UUID) -> None:
        """Stop a running workspace."""
        log = logger.bind(workspace_id=str(workspace_id))
        info = self._get_cached(workspace_id)
        runtime = self._get_runtime(workspace_id)

        if not info.instance_id:
            raise RuntimeError("Workspace has no instance assigned")

        await runtime.stop_workspace(info.instance_id)
        info.status = "exited"
        log.info("workspace_stopped")

    async def resume_workspace(
        self,
        workspace_id: uuid.UUID,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
    ) -> None:
        """Resume (start) a previously stopped workspace."""
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
        log.info("workspace_resumed")

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
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
        prepared: PreparedOperation | None = None,
    ) -> str:
        """Open an interactive PTY shell in the workspace.

        Returns a ``terminal_id`` that identifies this PTY session.
        """
        prepared = prepared or await self.prepare_operation(
            workspace_id,
            env_vars=env_vars,
            ssh_keys=ssh_keys,
        )
        log = prepared.log

        handle = await prepared.runtime.exec_pty(
            prepared.instance_id,
            cols=cols,
            rows=rows,
            workdir="/workspace",
            env={
                "TERM": "xterm-256color",
                **(
                    prepared.credential_context.environment
                    if prepared.credential_context
                    else {}
                ),
            },
        )

        terminal_id = str(uuid.uuid4())
        self._terminals[terminal_id] = TerminalSession(
            handle=handle,
            runtime=prepared.runtime,
            credential_context=prepared.credential_context,
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
        await self._cleanup_operation_credential_context(
            entry.runtime,
            entry.handle.instance_id,
            entry.credential_context,
            logger.bind(terminal_id=terminal_id),
        )
        logger.info("terminal_closed", terminal_id=terminal_id)

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

    # ── Image artifact operations ─────────────────────────────────────

    async def build_image(
        self,
        *,
        runtime_type: str,
        runner_image_build_id: str,
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

    async def create_workspace_from_image_artifact(
        self,
        image_artifact_id: str,
        new_workspace_id: uuid.UUID,
        runtime_type: str,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        env_vars: dict[str, str] | None = None,
        ssh_keys: list[str] | None = None,
    ) -> uuid.UUID:
        """Create a workspace from an image artifact.

        Creates a new workspace backed by the artifact's disk state.
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

        logger.info(
            "workspace_created_from_image_artifact",
            workspace_id=str(new_workspace_id),
            image_artifact_id=image_artifact_id,
            runtime_type=runtime_type,
        )
        return new_workspace_id
