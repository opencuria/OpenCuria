"""Docker-based runtime backend using the Docker SDK for Python.

Since the ``docker`` Python SDK is synchronous, all blocking calls are
wrapped with ``asyncio.to_thread`` so they integrate cleanly with the
async runner core.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue as _queue
import threading
import uuid
from collections.abc import AsyncIterator

import docker
import structlog
from docker.errors import NotFound as ContainerNotFound
from docker.errors import NotFound as NetworkNotFound

from .base import (
    CommandExecutionError,
    ProcessHandle,
    PtyHandle,
    RuntimeBackend,
    RuntimeStatus,
    RuntimeWorkspaceInfo,
    WorkspaceConfig,
)
from .docker_frames import (
    DOCKER_FRAME_MAX_SIZE,
    DOCKER_QUEUE_CHUNK_SIZE,
    DockerFrameError,
    DockerFrameParser,
)
from .stream_wrapper import stream_wrapper_argv

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

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        """Return the container IP on its workspace network."""
        container = self._container(instance_id)
        network_name = self._workspace_network_name(workspace_id)
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        net = networks.get(network_name)
        if net and net.get("IPAddress"):
            return net["IPAddress"]
        # Fallback: return first available IP
        for net_info in networks.values():
            if net_info.get("IPAddress"):
                return net_info["IPAddress"]
        raise RuntimeError(f"No IP address found for container {instance_id}")

    def get_workspace_network_name(self, workspace_id: str) -> str:
        """Return the Docker network name for a workspace."""
        return self._workspace_network_name(workspace_id)

    def connect_to_network(self, container_name: str, network_name: str) -> None:
        """Connect a container to a Docker network (idempotent)."""
        client = self._get_client()
        try:
            network = client.networks.get(network_name)
        except Exception:
            raise RuntimeError(f"Network {network_name} not found")
        try:
            network.connect(container_name)
        except Exception as exc:
            # Already connected is fine
            if "already exists" in str(exc).lower() or "endpoint with name" in str(exc).lower():
                return
            raise

    def disconnect_from_network(self, container_name: str, network_name: str) -> None:
        """Disconnect a container from a Docker network (idempotent)."""
        client = self._get_client()
        try:
            network = client.networks.get(network_name)
            network.disconnect(container_name, force=True)
        except Exception:
            pass  # Already disconnected or network gone

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
        """Execute a command inside the container and stream output line by line.

        Note: the Docker SDK offers no reliable ``exec_kill`` for a running
        exec, so a cancelled consumer stops draining but the remote command
        runs to completion; the thread always terminates and ``exec_inspect``
        state is never leaked.
        """

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

    # -- Generic non-TTY bidirectional process streams ----------------------

    #: Bounded internal queue depth for demuxed stream output (frames of
    #: at most ``DOCKER_FRAME_MAX_SIZE`` bytes each).
    _STREAM_QUEUE_DEPTH = 64

    def _stream_pidfile(self) -> str:
        """Return a fresh pidfile path for one stream process."""
        return f"/tmp/opencuria-stream-{uuid.uuid4().hex}.pid"

    def _build_stream_kill_script(self, pidfile: str) -> list[str]:
        """Build a static kill script argv for one stream pidfile.

        Reads the session pid from *pidfile* (runner-generated path) and
        delivers TERM to the process group, waits briefly, then KILLs the
        group.  No user values are interpolated: the only variable part
        is the pidfile path, passed as ``$1``.
        """
        script = (
            "pidfile=\"$1\"; "
            "pid=\"\"; "
            "if [ -f \"$pidfile\" ]; then "
            "pid=$(cat \"$pidfile\" 2>/dev/null); fi; "
            "case \"$pid\" in ''|*[!0-9]*) exit 0;; esac; "
            "kill -TERM -\"$pid\" 2>/dev/null || "
            "kill -TERM \"$pid\" 2>/dev/null || true; "
            "for _ in 1 2 3 4 5 6 7 8 9 10; do "
            "kill -0 \"$pid\" 2>/dev/null || break; "
            "sleep 0.2; "
            "done; "
            "kill -KILL -\"$pid\" 2>/dev/null || "
            "kill -KILL \"$pid\" 2>/dev/null || true; "
            "rm -f \"$pidfile\""
        )
        return ["sh", "-c", script, "opencuria-stream-kill", pidfile]

    def _drain_docker_stream_socket(
        self,
        sock: object,
        stdout_queue: _queue.Queue[bytes | None],
        stderr_queue: _queue.Queue[bytes | None],
        stop: threading.Event,
        handle: ProcessHandle | None = None,
    ) -> None:
        """Pump a Docker exec socket into two bounded queues (worker thread).

        Parses Docker multiplex framing incrementally (frames up to
        ``DOCKER_FRAME_MAX_SIZE``/16MiB accepted); each complete frame
        payload is split losslessly into ``<=DOCKER_QUEUE_CHUNK_SIZE``
        queue items so no data is ever truncated.  A poison ``None``
        marks EOF on each queue exactly once.  Framing errors also
        terminate with ``None`` and record ``frame_error`` on the handle
        metadata (when a handle is passed) so ``process_wait`` surfaces
        the corruption as an unknown exit (``None``) instead of a bogus
        exit 0. No raw payload bytes are ever logged.

        Queue-full never blocks the pump forever: items are offered
        best-effort while running and dropped once ``stop`` is set
        (close path) so ``process_close`` always terminates.
        """
        parser = DockerFrameParser(max_frame_size=DOCKER_FRAME_MAX_SIZE)
        raw = getattr(sock, "_sock", sock)

        def _offer(target: _queue.Queue[bytes | None], item: bytes) -> bool:
            """Offer one chunk; drop (when stopping) instead of blocking."""
            while True:
                try:
                    target.put(item, timeout=0.2)
                    return True
                except _queue.Full:
                    if stop.is_set():
                        return False
                    continue

        def _offer_eof(target: _queue.Queue[bytes | None]) -> None:
            """Deliver the EOF sentinel exactly once, best-effort.

            On close races the consumer may be gone; ``put_nowait`` in a
            bounded queue is best-effort — the socket shutdown in
            ``process_close`` is what reliably releases the pump.
            """
            with contextlib.suppress(_queue.Full):
                target.put_nowait(None)

        try:
            while not stop.is_set():
                try:
                    chunk = raw.recv(self.STREAM_CHUNK_SIZE)
                except OSError:
                    break
                if not chunk:
                    break
                try:
                    frames = parser.feed(bytes(chunk))
                except DockerFrameError:
                    logger.warning("docker_stream_frame_error")
                    if handle is not None:
                        handle.metadata.setdefault("frame_error", "frame_error")
                    break
                for stream, payload in frames:
                    target = (
                        stdout_queue
                        if stream == "stdout"
                        else stderr_queue
                    )
                    # Lossless re-chunk: never truncate payloads >64KiB.
                    for offset in range(
                        0, len(payload), DOCKER_QUEUE_CHUNK_SIZE
                    ):
                        piece = payload[
                            offset : offset + DOCKER_QUEUE_CHUNK_SIZE
                        ]
                        if not piece:
                            continue
                        if stop.is_set():
                            break
                        if not _offer(target, piece):
                            break
                    if stop.is_set():
                        break
        finally:
            try:
                parser.feed_eof()
            except DockerFrameError:
                logger.warning("docker_stream_truncated_frame")
                if handle is not None:
                    handle.metadata.setdefault("frame_error", "truncated_frame")
            _offer_eof(stdout_queue)
            _offer_eof(stderr_queue)

    async def spawn_process(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        """Spawn a non-TTY bidirectional process (stdin/stdout/stderr)."""
        if not command or not all(isinstance(part, str) for part in command):
            raise ValueError("command must be a non-empty argv list of str")
        if any("\x00" in part for part in command):
            raise ValueError("command must not contain NUL bytes")
        pidfile = self._stream_pidfile()
        argv = stream_wrapper_argv(pidfile, workdir, env, list(command))

        def _spawn() -> tuple[object, str]:
            api = self._get_client().api
            environment = {"TERM": "xterm-256color"}
            exec_id = api.exec_create(
                instance_id,
                cmd=argv,
                tty=False,
                stdin=True,
                stdout=True,
                stderr=True,
                workdir=None,
                environment=environment,
            )["Id"]
            sock = api.exec_start(exec_id, socket=True, tty=False)
            return sock, exec_id

        sock, exec_id = await asyncio.to_thread(_spawn)
        stdout_queue: _queue.Queue[bytes | None] = _queue.Queue(
            maxsize=self._STREAM_QUEUE_DEPTH
        )
        stderr_queue: _queue.Queue[bytes | None] = _queue.Queue(
            maxsize=self._STREAM_QUEUE_DEPTH
        )
        stop = threading.Event()
        handle = ProcessHandle(instance_id=instance_id, handle=sock)
        pump = threading.Thread(
            target=self._drain_docker_stream_socket,
            args=(sock, stdout_queue, stderr_queue, stop, handle),
            daemon=True,
            name=f"opencuria-docker-stream-{exec_id[:12]}",
        )
        pump.start()
        handle.metadata.update(
            {
                "exec_id": exec_id,
                "pidfile": pidfile,
                "stdout_queue": stdout_queue,
                "stderr_queue": stderr_queue,
                "stop": stop,
                "pump": pump,
                "loop": asyncio.get_running_loop(),
            }
        )
        logger.info(
            "stream_process_spawned",
            exec_id=exec_id[:12],
            pidfile=pidfile,
            # argv[0] only: full args may embed flags/paths that echo
            # secret-adjacent material.
            command=next(iter(command), ""),
        )
        return handle

    async def _stream_queue_read(
        self, handle: ProcessHandle, stream: str, size: int
    ) -> bytes:
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"unknown stream: {stream!r}")
        size = max(1, min(int(size), self.STREAM_CHUNK_SIZE))
        key = "stdout_queue" if stream == "stdout" else "stderr_queue"
        pending_key = f"pending_{stream}"
        pending: bytes = handle.metadata.get(pending_key, b"")
        out = bytearray()
        out += pending[:size]
        pending = pending[size:]
        handle.metadata[pending_key] = pending
        if out:
            return bytes(out)
        q: _queue.Queue[bytes | None] = handle.metadata[key]

        def _get() -> bytes | None:
            return q.get()

        chunk = await asyncio.to_thread(_get)
        if chunk is None:
            return b""
        out += chunk[:size]
        handle.metadata[pending_key] = chunk[size:]
        return bytes(out)

    async def process_read(
        self,
        handle: ProcessHandle,
        stream: str = "stdout",
        size: int = 65536,
    ) -> bytes:
        """Read raw bytes from one demuxed stream (``b""`` on EOF)."""
        if handle.closed:
            return b""
        return await self._stream_queue_read(handle, stream, size)

    async def process_write(self, handle: ProcessHandle, data: bytes) -> None:
        """Write raw bytes to the process stdin."""
        if handle.closed or not data:
            return

        def _write() -> None:
            raw = getattr(handle.handle, "_sock", handle.handle)
            raw.sendall(bytes(data))

        await asyncio.to_thread(_write)

    async def process_write_eof(self, handle: ProcessHandle) -> None:
        """Half-close stdin (shutdown write side; fall back to close)."""
        if handle.closed:
            return

        def _eof() -> None:
            raw = getattr(handle.handle, "_sock", handle.handle)
            shutdown = getattr(raw, "shutdown", None)
            if shutdown is None:
                return
            import socket as _socket

            try:
                shutdown(_socket.SHUT_WR)
            except OSError:
                pass

        await asyncio.to_thread(_eof)

    async def process_wait(self, handle: ProcessHandle) -> int | None:
        """Wait for the pump to drain, then report the exec exit code.

        Returns ``None`` when the exit code is unknown (the exec is
        still reported ``Running`` after the pump drained — e.g. a
        slow ``exec_inspect`` race): callers must not mistake that for
        exit 0. Transport/inspect failures also yield ``None`` so a
        framing error never masquerades as success (see the
        ``frame_error`` metadata flag set by the pump).
        """
        pump: threading.Thread | None = handle.metadata.get("pump")

        def _join() -> None:
            if pump is not None:
                pump.join(timeout=30)

        await asyncio.to_thread(_join)
        if handle.metadata.get("frame_error"):
            return None
        exec_id = str(handle.metadata.get("exec_id", ""))

        def _inspect() -> int | None:
            try:
                inspect = self._get_client().api.exec_inspect(exec_id)
            except Exception:
                return None
            running = inspect.get("Running")
            if running:
                return None
            try:
                return int(inspect.get("ExitCode") or 0)
            except (TypeError, ValueError):
                return None

        return await asyncio.to_thread(_inspect)

    async def process_close(self, handle: ProcessHandle) -> None:
        """Graceful stdin EOF, TERM/KILL the process group, close socket.

        Ordering matters: stdin EOF first while the handle is still open
        (``handle.closed`` is only set *after* the EOF attempt — setting
        it before would make ``process_write_eof`` a no-op), then signal
        stop, kill the tree, close the socket (which releases a blocked
        pump thread), and finally join the pump.
        """
        if handle.closed:
            return
        with contextlib.suppress(Exception):
            await self.process_write_eof(handle)
        handle.closed = True
        stop: threading.Event | None = handle.metadata.get("stop")
        if stop is not None:
            stop.set()
        pidfile = str(handle.metadata.get("pidfile", ""))
        if pidfile:
            kill_argv = self._build_stream_kill_script(pidfile)

            def _kill() -> None:
                try:
                    container = self._container(handle.instance_id)
                    api = self._get_client().api
                    exec_id = api.exec_create(
                        container.id,
                        cmd=kill_argv,
                        tty=False,
                        stdin=False,
                        stdout=False,
                        stderr=False,
                    )["Id"]
                    api.exec_start(exec_id, stream=False, tty=False)
                except Exception:
                    logger.warning("stream_kill_failed")

            await asyncio.to_thread(_kill)
        pump: threading.Thread | None = handle.metadata.get("pump")

        def _close_socket() -> None:
            try:
                sock = handle.handle
                raw = getattr(sock, "_sock", sock)
                # Shutdown first so a pump blocked in recv() wakes up
                # even if close alone would not interrupt it.
                shutdown = getattr(raw, "shutdown", None)
                if shutdown is not None:
                    import socket as _socket

                    with contextlib.suppress(OSError):
                        shutdown(_socket.SHUT_RDWR)
                close = getattr(raw, "close", None)
                if close is not None:
                    close()
                elif hasattr(sock, "close"):
                    sock.close()  # type: ignore[union-attr]
            except Exception:
                pass

        await asyncio.to_thread(_close_socket)
        if pump is not None:
            await asyncio.to_thread(pump.join, 10)
        frame_error = bool(handle.metadata.get("frame_error"))
        logger.info(
            "stream_process_closed",
            exec_id=str(handle.metadata.get("exec_id", ""))[:12],
            pidfile=pidfile,
            frame_error=frame_error,
        )

    # -- PTY / interactive terminal --------------------------------------------

    async def exec_pty(
        self,
        instance_id: str,
        cols: int = 80,
        rows: int = 24,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
    ) -> PtyHandle:
        """Create a PTY-enabled exec instance and return a handle."""

        def _create() -> PtyHandle:
            api = self._get_client().api
            environment = {"TERM": "xterm-256color"}
            if env:
                environment.update(env)
            exec_id = api.exec_create(
                instance_id,
                cmd=command or ["/bin/bash", "-l"],
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
