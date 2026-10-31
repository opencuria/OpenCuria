"""Abstract base class for runtime backends.

The runtime backend is responsible for managing the lifecycle of isolated
QEMU/KVM virtual machines.  Concrete implementations
must implement every abstract method defined here.
"""

from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from datetime import datetime

from .inventory import RuntimeInventory
from .managed_process import (
    TOKEN_PROBE_CODE,
    ProcessProfile,
    managed_close_argv,
    validate_guest_token,
)


@dataclass(frozen=True)
class WorkspaceConfig:
    """Parameters for creating a new workspace environment.

    ``image`` is a QCOW2 base image path. Each VM gets its own libvirt network
    and overlay disk.
    """

    workspace_id: str
    image: str
    qemu_vcpus: int | None = None
    qemu_memory_mb: int | None = None
    qemu_disk_size_gb: int | None = None


@dataclass(frozen=True)
class RuntimeStatus:
    """Status information returned by the runtime."""

    instance_id: str
    status: str  # normalised: "running", "exited", "created", "dead"
    name: str | None = None


@dataclass(frozen=True)
class RuntimeWorkspaceInfo:
    """Workspace information discovered from the runtime.

    Used by ``list_workspaces`` to reconstruct workspace state from
    libvirt metadata and status without needing a local database.
    """

    workspace_id: str
    instance_id: str
    status: str  # normalised: "running", "exited", etc.
    name: str | None = None


@dataclass
class ProcessHandle:
    """Opaque handle for a generic non-TTY bidirectional process stream.

    Created by ``spawn_process``; drive I/O with ``process_read`` /
    ``process_write`` / ``process_close`` / ``process_wait``.

    ``instance_id`` identifies the libvirt domain. ``handle`` stores the
    transport's I/O object.
    ``metadata`` holds runtime-specific data (exec id, pidfile, …).
    """

    instance_id: str
    handle: object
    metadata: dict = field(default_factory=dict)
    closed: bool = field(default=False, init=False)


@dataclass
class PtyHandle:
    """Opaque handle for an interactive PTY session.

    Created by ``exec_pty`` and passed to ``pty_read``/``pty_write``/
    ``pty_resize``/``pty_close``.

    ``instance_id`` identifies the libvirt domain. ``handle`` stores the
    asyncssh process. ``metadata`` holds additional transport data.
    """

    instance_id: str
    handle: object
    metadata: dict = field(default_factory=dict)
    closed: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ImageArtifactInfo:
    """Metadata for a captured workspace image artifact."""

    artifact_id: str
    workspace_id: str
    name: str
    created_at: datetime
    size_bytes: int | None = None
    min_disk_size_gb: int | None = None
    virtual_size_bytes: int | None = None


class RuntimeBackend(abc.ABC):
    """Abstract interface for workspace runtime management.

    Keeps workspace operations independent of the QEMU/libvirt transport.
    """

    # --- Lifecycle ------------------------------------------------------------

    @abc.abstractmethod
    async def create_workspace(self, config: WorkspaceConfig) -> str:
        """Create and start a new workspace environment.

        Returns the libvirt domain name.
        """

    @abc.abstractmethod
    async def stop_workspace(self, instance_id: str) -> None:
        """Stop a running workspace."""

    @abc.abstractmethod
    async def start_workspace(self, instance_id: str) -> None:
        """Start a previously stopped workspace."""

    @abc.abstractmethod
    async def remove_workspace(self, instance_id: str) -> None:
        """Remove the workspace and clean up resources."""

    async def reconfigure_workspace(
        self,
        instance_id: str,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
        restart: bool,
    ) -> None:
        """Reconfigure workspace resources.

        Runtimes that do not support reconfiguration should raise
        ``NotImplementedError``.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support reconfigure_workspace"
        )

    async def inventory(self) -> RuntimeInventory:
        """Return a full physical graph or explicit incomplete inspection."""
        return RuntimeInventory(errors=["Inventory unsupported"])

    # --- Inspection -----------------------------------------------------------

    @abc.abstractmethod
    async def workspace_exists(self, instance_id: str) -> bool:
        """Check whether the workspace environment still exists."""

    @abc.abstractmethod
    async def get_workspace_status(self, instance_id: str) -> RuntimeStatus:
        """Return current status information for the workspace."""

    @abc.abstractmethod
    async def list_workspaces(self) -> list[RuntimeWorkspaceInfo]:
        """Discover all opencuria workspaces managed by this runtime.

        Returns workspace info reconstructed from metadata/labels.
        """

    # --- Execution ------------------------------------------------------------

    @abc.abstractmethod
    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Execute a command and wait for completion.

        Returns (exit_code, combined_output).
        """

    @abc.abstractmethod
    async def put_archive(
        self,
        instance_id: str,
        path: str,
        data: bytes,
    ) -> None:
        """Extract a tar archive stream into *path* inside the workspace."""

    # --- Generic non-TTY bidirectional process streams ---------------------

    # Limits shared by all runtime implementations of the generic stream
    # transport (MCP stdio / workspace-local TCP relay).  Chunk sizes are
    # bounded so one peer can never force unbounded buffering.
    STREAM_CHUNK_SIZE = 64 * 1024
    STREAM_READ_TIMEOUT = 300.0

    @property
    def supports_managed_process(self) -> bool:
        """Whether durable fenced stream launch/close is implemented."""
        return False

    async def probe_managed_token(self, instance_id: str) -> dict[str, str]:
        """Read boot + PID1 starttime before a managed launch is committed."""
        code, output = await self.exec_command_wait(
            instance_id, ["python3", "-c", TOKEN_PROBE_CODE],
        )
        if code != 0:
            raise RuntimeError("Managed guest token probe failed")
        return validate_guest_token(json.loads(output))

    async def spawn_process(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        *,
        control_path: str | None = None,
        expected_token: dict[str, str] | None = None,
        process_profile: ProcessProfile | None = None,
        isolated_home: str | None = None,
    ) -> ProcessHandle:
        """Spawn a non-TTY bidirectional process inside the workspace.

        Args:
            instance_id: Runtime-specific instance identifier.
            command: argv list (never shell-joined by the caller).
            control_path: Optional runner-selected durable guest control directory.
                Managed callers must persist this identity before spawning.
            expected_token: Immutable guest incarnation token sampled before
                the managed intent commit; never supplied by an external RPC.
            workdir: Working directory inside the workspace.
            env: Extra environment for the child only (least privilege:
                persistent workspace credentials are NOT sourced).
            process_profile: Optional isolated agent or credential-free helper
                environment, independent of the managed process owner.
            isolated_home: Runner-selected temporary home for isolated profiles.

        Returns a ``ProcessHandle`` with split stdout/stderr streams.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support spawn_process"
        )

    async def process_read(
        self,
        handle: ProcessHandle,
        stream: str = "stdout",
        size: int = 65536,
    ) -> bytes:
        """Read raw bytes from *stream* (``"stdout"``/``"stderr"``).

        Blocks until data is available.  Returns ``b""`` on EOF.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support process_read"
        )

    async def process_write(self, handle: ProcessHandle, data: bytes) -> None:
        """Write raw bytes to the process stdin."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support process_write"
        )

    async def process_write_eof(self, handle: ProcessHandle) -> None:
        """Half-close the process stdin (graceful EOF)."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support process_write_eof"
        )

    async def process_wait(self, handle: ProcessHandle) -> int | None:
        """Wait for the process tree to exit; return the exit code."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support process_wait"
        )

    async def close_managed_process(
        self, instance_id: str, control_path: str,
    ) -> bool:
        """Fence late starts and verify group termination; raise if unknown."""
        code, _ = await self.exec_command_wait(
            instance_id, managed_close_argv(control_path),
        )
        if code != 0:
            raise RuntimeError("Managed stream termination unverified")
        return True

    async def process_detach(self, handle: ProcessHandle) -> None:
        """Release local transport only after lifecycle proves guest death.

        Must never exec or signal the guest. Unsupported runtimes fail closed.
        """
        raise NotImplementedError("Local stream detach unsupported")

    async def process_close(self, handle: ProcessHandle) -> bool | None:
        """Terminate the tree and release transport resources.

        Managed handles return True only after confirmed fenced group closure,
        or raise on unknown outcome. Legacy unmanaged runtimes may return None.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support process_close"
        )

    # --- PTY / interactive terminal -----------------------------------------

    @abc.abstractmethod
    async def exec_pty(
        self,
        instance_id: str,
        cols: int = 80,
        rows: int = 24,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
    ) -> PtyHandle:
        """Create an interactive PTY shell inside the workspace.

        Returns a ``PtyHandle`` that can be used for bidirectional I/O.
        """

    @abc.abstractmethod
    async def pty_read(self, handle: PtyHandle, size: int = 4096) -> bytes:
        """Read raw bytes from the PTY. Blocks until data is available.

        Returns ``b""`` when the PTY is closed / EOF.
        """

    @abc.abstractmethod
    async def pty_write(self, handle: PtyHandle, data: bytes) -> None:
        """Write raw bytes (stdin) to the PTY."""

    @abc.abstractmethod
    async def pty_resize(
        self, handle: PtyHandle, cols: int, rows: int
    ) -> None:
        """Resize the PTY window."""

    @abc.abstractmethod
    async def pty_close(self, handle: PtyHandle) -> None:
        """Close the PTY and release resources."""

    # --- Image artifacts -----------------------------------------------------


    async def create_image_artifact(
        self, instance_id: str, artifact_name: str, *,
        artifact_id: str | None = None, operation_id: str | None = None,
        credential_clean: bool = False,
    ) -> ImageArtifactInfo:
        """Capture an image artifact for the workspace.

        Returns an artifact identifier.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support image artifacts"
        )

    async def delete_image_artifact(self, artifact_id: str) -> None:
        """Delete a previously created image artifact."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support image artifacts"
        )

    async def create_workspace_from_image_artifact(
        self, artifact_id: str, config: WorkspaceConfig
    ) -> str:
        """Create a new workspace from an image artifact.

        Returns the instance ID of the new workspace.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support image artifacts"
        )
