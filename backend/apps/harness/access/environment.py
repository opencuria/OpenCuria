"""Run-local execution environment without mutating a shared workspace accessor."""

from __future__ import annotations

from typing import Any


class ExecutionEnvironmentAccessor:
    """Delegate workspace access, adding defaults only to agent execution paths.

    MCP servers receive their declared environments via the original accessor.
    Isolated helper processes must retain their strictly empty environment.
    Caller-provided values override defaults, e.g. for an explicit Xvfb display.
    """

    def __init__(self, accessor: Any, environment: dict[str, str]) -> None:
        self._accessor = accessor
        self._environment = dict(environment)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._accessor, name)

    async def exec_wait(
        self,
        command: list[str] | str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Supply the binding to ordinary shell commands."""
        return await self._accessor.exec_wait(
            command,
            workdir=workdir,
            env={**self._environment, **(env or {})},
            timeout=timeout,
        )

    async def process_start(
        self,
        command: str,
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        name: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Persist the binding with background processes for subsequent restarts."""
        return await self._accessor.process_start(
            command,
            workdir=workdir,
            env={**self._environment, **(env or {})},
            name=name,
            **kwargs,
        )

    async def open_process(
        self,
        command: list[str],
        workdir: str = "/workspace",
        env: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> Any:
        """Set the environment before launching Codex/Claude and their shells."""
        environment = dict(env or {})
        if kwargs.get("process_profile") != "helper" and (
            "CODEX_HOME" in environment or "CLAUDE_CONFIG_DIR" in environment
        ):
            environment = {**self._environment, **environment}
        return await self._accessor.open_process(
            command, workdir, environment, timeout=timeout, **kwargs
        )
