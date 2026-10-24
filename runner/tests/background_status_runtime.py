"""Local subprocess runtime shared by status integration tests and benchmarks."""

from __future__ import annotations

import asyncio


class LocalRuntime:
    """Execute the runtime command contract using real local subprocesses."""

    def __init__(self, workdir: str) -> None:
        self.workdir = workdir
        self.delay_s = 0.0
        self.calls = 0

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Run real shell work, optionally adding a simulated per-call delay."""
        self.calls += 1
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert process.returncode is not None
        return process.returncode, output.decode("utf-8", errors="replace")
