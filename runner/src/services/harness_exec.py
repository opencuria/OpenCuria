"""Structured harness command execution with separated output streams.

:class:`HarnessExecService` wraps commands and decodes tagged stdout/stderr
using the shared execution and path helpers.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend
from .capture_fence import CaptureFence, live_interaction
from .credentials import WORKSPACE_CREDENTIAL_ENV_FILE
from .workspace_helpers import sanitize_exec_workdir as _sanitize_exec_workdir

logger = structlog.get_logger(__name__)


class HarnessExecService:
    """Owns structured harness command execution.

    Runtime backends and workspace lookup functions are injected by the
    runner composition root. The workdir sanitizer and credential env-file
    path default to their canonical runner values.
    """

    def __init__(
        self,
        runtime: RuntimeBackend | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        sanitize_exec_workdir: Callable[[str], str] | None = None,
        credential_env_file: str | None = None,
    ) -> None:
        self.capture_fence: CaptureFence | None = None
        self._runtime = runtime
        self._get_cached = get_cached
        self._get_runtime = get_runtime
        self._sanitize_exec_workdir = (
            sanitize_exec_workdir
            if sanitize_exec_workdir is not None
            else _sanitize_exec_workdir
        )
        self._credential_env_file = (
            WORKSPACE_CREDENTIAL_ENV_FILE
            if credential_env_file is None
            else credential_env_file
        )

    @live_interaction
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
        safe_workdir = self._sanitize_exec_workdir(workdir)
        assert self._get_cached is not None and self._get_runtime is not None
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
            f"if [ -f {shlex.quote(self._credential_env_file)} ]; then "
            f". {shlex.quote(self._credential_env_file)}; fi; "
        )
        wrapper = (
            f"{source}"
            f"__oc_out=$(mktemp); __oc_err=$(mktemp); "
            f'sh -c {shlex.quote(inner)} >"$__oc_out" 2>"$__oc_err"; '
            f"__oc_code=$?; "
            f'echo {marker_out}; base64 "$__oc_out"; '
            f'echo {marker_err}; base64 "$__oc_err"; '
            f'echo "EXIT:$__oc_code"; rm -f "$__oc_out" "$__oc_err"; '
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


__all__ = ["HarnessExecService"]
