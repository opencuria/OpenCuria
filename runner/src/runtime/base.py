"""Abstract base class for runtime backends.

The runtime backend is responsible for managing the lifecycle of isolated
workspace environments (containers, VMs, etc.).  Concrete implementations
must implement every abstract method defined here.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class WorkspaceConfig:
    """Parameters for creating a new workspace environment.

    Fields are generic — each runtime interprets them appropriately:
    - Docker: ``image`` is a Docker image tag, ``volumes`` maps volume
      names to mount specs, ``network`` is a Docker network name.
    - QEMU: ``image`` is a QCOW2 base image path, ``volumes`` is unused
      (storage is managed via disk images), ``network`` is a libvirt
      network name.
    """

    workspace_id: str
    image: str
    env_vars: dict[str, str]
    volumes: dict[str, dict[str, str]] = field(default_factory=dict)
    network: str | None = None
    labels: dict[str, str] | None = None
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
    container/VM metadata and status without needing a local database.
    """

    workspace_id: str
    instance_id: str
    status: str  # normalised: "running", "exited", etc.
    name: str | None = None


@dataclass
class PtyHandle:
    """Opaque handle for an interactive PTY session.

    Created by ``exec_pty`` and passed to ``pty_read``/``pty_write``/
    ``pty_resize``/``pty_close``.

    ``instance_id`` identifies the workspace instance (container ID,
    domain name, …).  ``handle`` stores the runtime-specific I/O object
    (Docker exec socket, asyncssh process, …).  ``metadata`` holds any
    additional runtime-specific data.
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


class CommandExecutionError(RuntimeError):
    """Raised when a streamed workspace command exits unsuccessfully."""

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__(f"Command failed with exit code {exit_code}")


class RuntimeBackend(abc.ABC):
    """Abstract interface for workspace runtime management.

    Implement this interface to swap Docker for another virtualisation
    technology (QEMU/KVM, Firecracker, Kata Containers, …).
    """

    # --- Runtime identity ----------------------------------------------------

    @property
    @abc.abstractmethod
    def runtime_type(self) -> str:
        """Return a short identifier for this runtime (e.g. ``"docker"``)."""

    # --- Lifecycle ------------------------------------------------------------

    @abc.abstractmethod
    async def create_workspace(self, config: WorkspaceConfig) -> str:
        """Create and start a new workspace environment.

        Returns the runtime-specific identifier (e.g. Docker container ID,
        libvirt domain name).
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

    async def restart_workspace(self, instance_id: str) -> None:
        """Force-restart a workspace (hard reset, then start).

        Used by the self-healing health-check loop when a workspace becomes
        unreachable.  The default implementation performs a stop followed by
        a start.  Runtimes that support a faster hard-reset (e.g. QEMU
        virDomainDestroy + virDomainCreate) should override this method.
        """
        await self.stop_workspace(instance_id)
        await self.start_workspace(instance_id)

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
    async def exec_command(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Execute a command inside the workspace and stream stdout/stderr.

        Yields output line-by-line as an async iterator.
        """

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

    # --- Image artifacts (optional) ------------------------------------------

    @property
    def supports_image_artifacts(self) -> bool:
        """Whether this runtime supports image artifact capture/clone operations."""
        return False

    async def create_image_artifact(
        self, instance_id: str, artifact_name: str
    ) -> str:
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

    async def list_image_artifacts(
        self, instance_id: str | None = None
    ) -> list[ImageArtifactInfo]:
        """List image artifacts, optionally filtered by workspace instance."""
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
