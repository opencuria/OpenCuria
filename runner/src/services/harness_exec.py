"""Harness command execution: structured exec requested by the backend.

Canonical home (Step 4) for ``exec_harness_command``,
``exec_harness_command_stream`` and ``_parse_harness_exec_output``
previously living on ``WorkspaceService`` in ``src.service``. Built on
the exec kernel; owns no lifecycle or session state.

``WorkspaceService`` keeps thin delegates (same names/signatures/
messages) plus a ``harness`` property onto the manager, so existing
callers and tests keep working.

Workspace resolution is injected so this module never imports
``src.service`` (no dependency cycle). The credential env-file path is
imported canonically from :mod:`src.services.credentials`.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import AsyncIterator, Callable

import structlog

from ..models import WorkspaceInfo
from ..runtime.base import RuntimeBackend
from .credentials import WORKSPACE_CREDENTIAL_ENV_FILE
from .exec_kernel import sanitize_exec_workdir as _sanitize_exec_workdir

logger = structlog.get_logger(__name__)


class HarnessExecService:
    """Owns structured harness command execution.

    Injected dependencies (all mirror ``WorkspaceService`` helpers):

    - ``runtimes``: runtime backends by type (kept for introspection;
      resolution itself goes through the callables below).
    - ``get_cached``: ``(workspace_id) -> WorkspaceInfo``; raises
      ``ValueError("... not found")`` for unknown ids.
    - ``get_runtime``: ``(workspace_id) -> RuntimeBackend``; raises
      ``RuntimeError`` for unknown runtimes.
    - ``sanitize_exec_workdir``: ``(path) -> str``; defaults to the
      canonical ``src.services.exec_kernel.sanitize_exec_workdir``.
    - ``credential_env_file``: guest path sourced by the shell wrappers.
      ``WorkspaceService`` passes its ``WORKSPACE_CREDENTIAL_ENV_FILE``
      constant; the default matches it.
    """

    def __init__(
        self,
        runtimes: dict[str, RuntimeBackend] | None = None,
        get_cached: Callable[[uuid.UUID], WorkspaceInfo] | None = None,
        get_runtime: Callable[[uuid.UUID], RuntimeBackend] | None = None,
        sanitize_exec_workdir: Callable[[str], str] | None = None,
        credential_env_file: str | None = None,
    ) -> None:
        self._runtimes = runtimes if runtimes is not None else {}
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

    async def exec_harness_command_stream(
        self,
        workspace_id: uuid.UUID,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[tuple[str, str]]:
        """Execute a harness command and yield ``(stream, data)`` chunks.

        Yields ``("stdout", text)`` / ``("stderr", text)`` tuples while the
        command runs, then a final ``("exit", str(exit_code))`` tuple.
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
        marker_out = "OPENCURIA_LINE_STDOUT:"
        marker_err = "OPENCURIA_LINE_STDERR:"
        inner = " ".join(shlex.quote(arg) for arg in argv)
        source = (
            f"if [ -f {shlex.quote(self._credential_env_file)} ]; then "
            f". {shlex.quote(self._credential_env_file)}; fi; "
        )
        # Portable fifo-based streaming wrapper: multiplexes the child
        # stdout/stderr into tagged lines on the combined output stream,
        # then reports the exit code on the last line.
        portable = (
            f"{source}"
            "__oc_dir=$(mktemp -d); "
            "__oc_o=$__oc_dir/o; __oc_e=$__oc_dir/e; "
            'mkfifo "$__oc_o" "$__oc_e"; '
            f"(sh -c {shlex.quote(inner)} "
            '>"$__oc_o" 2>"$__oc_e"; echo $? >"$__oc_dir/code") & '
            "__oc_pid=$!; "
            "(while IFS= read -r __oc_l; do "
            f"printf '{marker_out}%s\\n' \"$__oc_l\"; "
            'done <"$__oc_o" & '
            "while IFS= read -r __oc_m; do "
            f"printf '{marker_err}%s\\n' \"$__oc_m\"; "
            'done <"$__oc_e" & wait); '
            'wait $__oc_pid; __oc_code=$(cat "$__oc_dir/code"); '
            'rm -rf "$__oc_dir"; '
            'echo "OPENCURIA_EXIT:$__oc_code"'
        )
        exit_code = 0
        async for line in runtime.exec_command(
            info.instance_id,
            command=["sh", "-lc", portable],
            workdir=safe_workdir,
            env=env,
        ):
            if line.startswith(marker_out):
                yield ("stdout", line[len(marker_out) :])
            elif line.startswith(marker_err):
                yield ("stderr", line[len(marker_err) :])
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


__all__ = ["HarnessExecService"]
