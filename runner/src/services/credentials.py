"""Credential management: inject / remove persistent workspace secrets.

Canonical home (Step 5) for the ``WORKSPACE_CREDENTIAL_*`` path
constants (Step 4 forward-move, verbatim values previously in
``src.service``) plus the credential operations previously living on
``WorkspaceService`` in ``src.service``:

- :class:`CredentialManager` owning ``_credential_path_helpers``,
  ``_wrap_command_with_persistent_env``,
  ``remove_workspace_credentials``, ``inject_workspace_credentials``
  (moved verbatim from ``src.service``; inject is 183 lines).

``src.service`` re-exports the constants (identical objects) and keeps
thin delegates (same names/signatures/messages) plus a ``credentials``
property, so existing callers and tests keep working.

Dependency direction (no cycle): this module imports the wrap / tar
helpers from :mod:`src.services.exec_kernel` and
:mod:`src.services.files`. It never imports ``src.service``. The exec
kernel never imports this module (the guest env-file path crosses the
boundary as an explicit parameter defaulting to the canonical
constant).
"""

from __future__ import annotations

import shlex
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from ..runtime.base import RuntimeBackend
from .exec_kernel import credential_path_helpers as _kernel_credential_path_helpers
from .exec_kernel import wrap_command_with_persistent_env as _kernel_wrap_command
from .files import build_tar_entries as _build_tar_entries_impl

logger = structlog.get_logger(__name__)

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


class CredentialManager:
    """Owns persistent workspace credential inject / remove operations.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``exec_command``: optional async
      ``(runtime, instance_id, command_dict) -> (exit_code, output)``
      used by :meth:`inject_workspace_credentials` for the
      credential-context probe (see below). When ``None``, the manager
      skips the probe exactly like the facade (the probe was removed
      from ``inject_workspace_credentials`` before Step 5; the hook is
      kept for forward compatibility and for callers that want to pass
      ``ExecKernel.exec_command`` explicitly).
    - ``credential_env_file``: guest path sourced by the exec wrapper.
      ``WorkspaceService`` passes its ``WORKSPACE_CREDENTIAL_ENV_FILE``
      constant; the default matches it.
    """

    def __init__(
        self,
        exec_command: (
            Callable[
                [RuntimeBackend, str, dict[str, Any]],
                Awaitable[tuple[int, str]],
            ]
            | None
        ) = None,
        credential_env_file: str = WORKSPACE_CREDENTIAL_ENV_FILE,
    ) -> None:
        self._exec_command = exec_command
        self._credential_env_file = credential_env_file

    @property
    def credential_env_file(self) -> str:
        """Return the guest credential env-file path used by wrappers."""
        return self._credential_env_file

    @staticmethod
    def _credential_path_helpers() -> list[str]:
        """Return shell helper functions used by inject and remove scripts."""
        return _kernel_credential_path_helpers()

    def _wrap_command_with_persistent_env(self, command: dict) -> dict:
        """Source persistent workspace credentials before running a command."""
        return _kernel_wrap_command(command, self._credential_env_file)

    def _build_tar_entries(
        self,
        files: list[tuple[str, bytes, int]],
    ) -> bytes:
        """Build a tar archive containing multiple files."""
        return _build_tar_entries_impl(files)

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
                '  done < "$manifest"',
                "fi",
                "opencuria_strip_environment_block",
                f"rm -f {shlex.quote(WORKSPACE_CREDENTIAL_ENV_FILE)} "
                f"{shlex.quote(WORKSPACE_CREDENTIAL_PROFILE_D)}",
                f"if [ -f {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)} ]; then",
                "  tmp_bashrc=$(mktemp)",
                f"  grep -vxF {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC_LINE)} "
                f'{shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)} > "$tmp_bashrc" || true',
                f'  cat "$tmp_bashrc" > {shlex.quote(WORKSPACE_CREDENTIAL_BASHRC)}',
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
        return await self._inject_after_remove(
            runtime, instance_id, env_vars, files, ssh_keys, log
        )

    async def _inject_after_remove(
        self,
        runtime: RuntimeBackend,
        instance_id: str,
        env_vars: dict[str, str] | None,
        files: list[dict[str, Any]] | None,
        ssh_keys: list[str] | None,
        log,
    ) -> bool:
        """Materialize credentials after the clean remove already ran.

        Shared remainder of :meth:`inject_workspace_credentials` (verbatim
        facade logic after the leading ``remove_workspace_credentials``
        call). ``WorkspaceService.inject_workspace_credentials`` calls its
        own (overridable/mockable) ``remove_workspace_credentials`` facade
        first and then delegates here, so tests patching the facade
        remove hook keep working while direct manager use removes via
        :meth:`remove_workspace_credentials`.
        """

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
                    f'install -m {mode:o} {shlex.quote(source_abspath)} "$target_path"',
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
        archive_files.append(("manifest", manifest.encode("utf-8"), 0o600))
        archive_files.append(
            ("install.sh", ("\n".join(install_lines) + "\n").encode("utf-8"), 0o700)
        )

        exit_code, output = await runtime.exec_command_wait(
            instance_id,
            command=[
                "mkdir",
                "-p",
                staging_dir,
                f"{staging_dir}/files",
                f"{staging_dir}/ssh",
            ],
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


__all__ = [
    "CredentialManager",
    "WORKSPACE_CREDENTIAL_BASHRC",
    "WORKSPACE_CREDENTIAL_BASHRC_LINE",
    "WORKSPACE_CREDENTIAL_DIR",
    "WORKSPACE_CREDENTIAL_ENV_FILE",
    "WORKSPACE_CREDENTIAL_ENVIRONMENT",
    "WORKSPACE_CREDENTIAL_ENVIRONMENT_END",
    "WORKSPACE_CREDENTIAL_ENVIRONMENT_START",
    "WORKSPACE_CREDENTIAL_MANIFEST",
    "WORKSPACE_CREDENTIAL_PROFILE_D",
]
