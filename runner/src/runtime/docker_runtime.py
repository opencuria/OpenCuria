"""Docker-based runtime backend using the Docker SDK for Python.

Since the ``docker`` Python SDK is synchronous, all blocking calls are
wrapped with ``asyncio.to_thread`` so they integrate cleanly with the
async runner core.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import docker
import structlog
from docker.errors import NotFound as ContainerNotFound
from docker.errors import NotFound as NetworkNotFound

from .base import (
    CommandExecutionError,
    PtyHandle,
    RuntimeBackend,
    RuntimeStatus,
    RuntimeWorkspaceInfo,
    WorkspaceConfig,
)

logger = structlog.get_logger(__name__)


class DockerRuntime(RuntimeBackend):
    """Manage workspaces as Docker containers on the local daemon."""

    _NETWORK_LABELS = {
        "opencuria.runtime-type": "docker",
        "opencuria.isolated-network": "true",
    }

    def __init__(self, base_url: str = "unix:///var/run/docker.sock") -> None:
        self._base_url = base_url
        self._client: docker.DockerClient | None = None

    # -- identity --------------------------------------------------------------

    @property
    def runtime_type(self) -> str:
        return "docker"

    # -- helpers ---------------------------------------------------------------

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.DockerClient(base_url=self._base_url)
        return self._client

    def _container(self, instance_id: str) -> docker.models.containers.Container:
        return self._get_client().containers.get(instance_id)

    def _workspace_network_name(self, workspace_id: str) -> str:
        """Return the dedicated Docker network name for a workspace."""
        return f"opencuria-ws-{workspace_id}"

    def _ensure_workspace_network(self, workspace_id: str) -> str:
        """Create or reuse an isolated bridge network for the workspace."""
        client = self._get_client()
        network_name = self._workspace_network_name(workspace_id)
        try:
            client.networks.get(network_name)
            return network_name
        except NetworkNotFound:
            pass

        client.networks.create(
            name=network_name,
            driver="bridge",
            check_duplicate=True,
            internal=False,
            labels={
                **self._NETWORK_LABELS,
                "opencuria.workspace-id": workspace_id,
            },
        )
        return network_name

    def _remove_workspace_network(self, workspace_id: str) -> None:
        """Remove the isolated Docker network for a workspace if it exists."""
        network_name = self._workspace_network_name(workspace_id)
        try:
            network = self._get_client().networks.get(network_name)
        except NetworkNotFound:
            return

        try:
            network.remove()
        except Exception:
            logger.warning(
                "workspace_network_remove_failed",
                workspace_id=workspace_id,
                network=network_name,
                exc_info=True,
            )
            raise

    # -- lifecycle -------------------------------------------------------------

    async def create_workspace(self, config: WorkspaceConfig) -> str:
        def _create() -> str:
            client = self._get_client()
            container_name = f"opencuria-workspace-{config.workspace_id}"
            network_name = self._ensure_workspace_network(config.workspace_id)
            try:
                container = client.containers.run(
                    image=config.image,
                    name=container_name,
                    detach=True,
                    environment=config.env_vars,
                    volumes=config.volumes,
                    network=network_name,
                    labels={
                        **(config.labels or {}),
                        "opencuria.workspace-network": network_name,
                    },
                    # Default CMD in Dockerfile is tail -f /dev/null, keeps alive
                    stdin_open=True,
                    tty=True,
                )
            except Exception:
                self._remove_workspace_network(config.workspace_id)
                raise
            return container.id

        instance_id = await asyncio.to_thread(_create)
        logger.info(
            "workspace_created",
            workspace_id=config.workspace_id,
            instance_id=instance_id[:12],
        )
        return instance_id

    async def stop_workspace(self, instance_id: str) -> None:
        def _stop() -> None:
            self._container(instance_id).stop(timeout=10)

        await asyncio.to_thread(_stop)
        logger.info("workspace_stopped", instance_id=instance_id[:12])

    async def start_workspace(self, instance_id: str) -> None:
        def _start() -> None:
            self._container(instance_id).start()

        await asyncio.to_thread(_start)
        logger.info("workspace_started", instance_id=instance_id[:12])

    async def remove_workspace(self, instance_id: str) -> None:
        def _remove() -> None:
            workspace_id: str | None = None
            try:
                container = self._container(instance_id)
                container.reload()
                labels = container.labels or {}
                workspace_id = labels.get("opencuria.workspace-id") or None
                container.remove(force=True)
            except ContainerNotFound:
                logger.info("workspace_container_already_absent", instance_id=instance_id[:12])

            if workspace_id:
                self._remove_workspace_network(workspace_id)

        await asyncio.to_thread(_remove)
        logger.info("workspace_removed", instance_id=instance_id[:12])

    # -- inspection ------------------------------------------------------------

    async def workspace_exists(self, instance_id: str) -> bool:
        def _exists() -> bool:
            try:
                self._container(instance_id)
                return True
            except ContainerNotFound:
                return False

        return await asyncio.to_thread(_exists)

    async def get_workspace_status(self, instance_id: str) -> RuntimeStatus:
        def _status() -> RuntimeStatus:
            container = self._container(instance_id)
            container.reload()
            return RuntimeStatus(
                instance_id=container.id,
                status=container.status,
                name=container.name,
            )

        return await asyncio.to_thread(_status)

    async def list_workspaces(self) -> list[RuntimeWorkspaceInfo]:
        """List all opencuria workspace containers by label."""

        def _list() -> list[RuntimeWorkspaceInfo]:
            client = self._get_client()
            containers = client.containers.list(
                all=True,
                filters={"label": "opencuria.workspace-id"},
            )
            results: list[RuntimeWorkspaceInfo] = []
            for container in containers:
                container.reload()
                labels = container.labels or {}
                workspace_id = labels.get("opencuria.workspace-id", "")
                results.append(
                    RuntimeWorkspaceInfo(
                        workspace_id=workspace_id,
                        instance_id=container.id,
                        status=container.status,
                        name=container.name,
                    )
                )
            return results

        return await asyncio.to_thread(_list)

    # -- execution -------------------------------------------------------------

    async def exec_command(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Execute a command inside the container and stream output line by line."""

        # docker SDK exec_run with stream=True returns a blocking generator,
        # so we run the iteration in a thread and push lines into an async queue.
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        state: dict[str, object] = {
            "exit_code": 0,
            "error": None,
        }

        def _run() -> None:
            try:
                container = self._container(instance_id)
                api = self._get_client().api
                exec_id = api.exec_create(
                    container.id,
                    cmd=command,
                    workdir=workdir,
                    environment=env,
                    stdout=True,
                    stderr=True,
                    stdin=False,
                    tty=False,
                )["Id"]
                output_stream = api.exec_start(
                    exec_id,
                    stream=True,
                    demux=False,
                )
                buffer = ""
                for chunk in output_stream:
                    text = chunk.decode("utf-8", errors="replace")
                    buffer += text
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        loop.call_soon_threadsafe(queue.put_nowait, line)
                if buffer:
                    loop.call_soon_threadsafe(queue.put_nowait, buffer)

                inspect = api.exec_inspect(exec_id)
                state["exit_code"] = int(inspect.get("ExitCode") or 0)
            except Exception as exc:
                state["error"] = exc
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        task = loop.run_in_executor(None, _run)

        while True:
            line = await queue.get()
            if line is None:
                break
            yield line

        # Ensure the thread finished cleanly
        await asyncio.wrap_future(task)  # type: ignore[arg-type]
        error = state["error"]
        if error is not None:
            raise RuntimeError("Failed to stream command output") from error
        exit_code = int(state["exit_code"])
        if exit_code != 0:
            raise CommandExecutionError(exit_code)

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Execute a command and wait for it to complete."""

        def _run() -> tuple[int, str]:
            container = self._container(instance_id)
            exit_code, output = container.exec_run(
                cmd=command,
                workdir=workdir,
                environment=env,
                stream=False,
                demux=False,
            )
            return exit_code, output.decode("utf-8", errors="replace")

        return await asyncio.to_thread(_run)

    async def put_archive(
        self,
        instance_id: str,
        path: str,
        data: bytes,
    ) -> None:
        """Extract a tar archive stream into *path* inside the container."""

        def _run() -> None:
            container = self._container(instance_id)
            ok = container.put_archive(path, data)
            if not ok:
                raise RuntimeError("Failed to put archive into container")

        await asyncio.to_thread(_run)

    # -- PTY / interactive terminal --------------------------------------------

    async def exec_pty(
        self,
        instance_id: str,
        cols: int = 80,
        rows: int = 24,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> PtyHandle:
        """Create a PTY-enabled exec instance and return a handle."""

        def _create() -> PtyHandle:
            api = self._get_client().api
            environment = {"TERM": "xterm-256color"}
            if env:
                environment.update(env)
            exec_id = api.exec_create(
                instance_id,
                cmd=["/bin/bash", "-l"],
                tty=True,
                stdin=True,
                stdout=True,
                stderr=True,
                workdir=workdir,
                environment=environment,
            )["Id"]
            sock = api.exec_start(exec_id, socket=True, tty=True)
            # Resize to initial dimensions
            api.exec_resize(exec_id, height=rows, width=cols)
            return PtyHandle(
                instance_id=instance_id,
                handle=sock,
                metadata={"exec_id": exec_id},
            )

        handle = await asyncio.to_thread(_create)
        logger.info(
            "pty_created",
            instance_id=instance_id[:12],
            exec_id=handle.metadata["exec_id"][:12],
        )
        return handle

    async def pty_read(self, handle: PtyHandle, size: int = 4096) -> bytes:
        """Read raw bytes from the PTY socket."""
        if handle.closed:
            return b""

        def _read() -> bytes:
            try:
                raw = handle.handle._sock.recv(size)
                return raw
            except OSError:
                return b""

        return await asyncio.to_thread(_read)

    async def pty_write(self, handle: PtyHandle, data: bytes) -> None:
        """Write raw bytes to the PTY socket (stdin)."""
        if handle.closed:
            return

        def _write() -> None:
            handle.handle._sock.sendall(data)

        await asyncio.to_thread(_write)

    async def pty_resize(
        self, handle: PtyHandle, cols: int, rows: int
    ) -> None:
        """Resize the PTY window."""
        if handle.closed:
            return

        def _resize() -> None:
            self._get_client().api.exec_resize(
                handle.metadata["exec_id"], height=rows, width=cols
            )

        await asyncio.to_thread(_resize)

    async def pty_close(self, handle: PtyHandle) -> None:
        """Close the PTY socket."""
        if handle.closed:
            return
        handle.closed = True

        def _close() -> None:
            try:
                handle.handle._sock.close()
            except Exception:
                pass

        await asyncio.to_thread(_close)
        logger.info("pty_closed", exec_id=handle.metadata["exec_id"][:12])
