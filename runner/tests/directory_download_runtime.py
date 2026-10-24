"""Local subprocess runtime for directory-download tests and measurements."""

from __future__ import annotations

import asyncio
import os


class LocalFileRuntime:
    """Execute the real guest argv locally, without VM/SSH transport."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.env = env or {}
        self.calls: list[list[str]] = []
        self.output_bytes: list[int] = []
        self.processes: list[asyncio.subprocess.Process] = []

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Match the runtime's combined stdout/stderr result contract."""
        self.calls.append(list(command))
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=workdir,
            env={**os.environ, **self.env, **(env or {})},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.append(process)
        stdout, stderr = await process.communicate()
        self.output_bytes.append(len(stdout) + len(stderr))
        assert process.returncode is not None
        return process.returncode, (stdout + stderr).decode()
