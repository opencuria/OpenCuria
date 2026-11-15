"""QEMU/KVM runtime backend using libvirt + asyncssh.

Manages workspace VMs with QCOW2 copy-on-write overlays for fast
cloning.  All command execution is performed over SSH.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tarfile
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import asyncssh
import libvirt
import structlog

from ..config import RunnerSettings
from .base import (
    ImageArtifactInfo,
    ProcessHandle,
    PtyHandle,
    RuntimeBackend,
    RuntimeStatus,
    RuntimeWorkspaceInfo,
    WorkspaceConfig,
)
from .diagnostics import (
    BUILD_QUEUE_TIMEOUT,
    BUILD_SCRIPT_FAILED,
    BUILD_SCRIPT_TIMEOUT,
    DISK_BELOW_IMAGE_MINIMUM,
    DISK_SHRINK_UNSUPPORTED,
    GENERALIZE_FAILED,
    IMAGE_INVALID,
    IMAGE_UNAVAILABLE,
    INVALID_RESOURCES,
    WINDOWS_HOST_UNSUPPORTED,
    WORKSPACE_EXISTS,
    OperationError,
    OperationRejectedError,
)
from .guest_os import (
    GUEST_OS_VALUES,
    WINDOWS_STATE_DIR,
    LINUX,
    METADATA_NS,
    GuestBootstrapError,
    GuestOS,
    SeedSpec,
    guest_for,
    guest_for_distro,
    validate_guest_os,
)
from .inventory import RuntimeInventory, StorageResource
from .linux_seed import (
    GUEST_SSH_MAX_SESSIONS as _GUEST_SSH_MAX_SESSIONS,
    GUEST_SSH_SESSIONS_CONF as _GUEST_SSH_SESSIONS_CONF,
    GUEST_SSH_SESSIONS_RELOADED as _GUEST_SSH_SESSIONS_RELOADED,
    GUEST_SSH_SESSIONS_SCRIPT as _GUEST_SSH_SESSIONS_SCRIPT,
)
from .recovery import RecoveryConflict
from .rfb import RFBClient
from .managed_process import (
    ProcessProfile,
    isolated_agent_env_preamble,
    managed_argv,
    managed_isolated_argv,
)
from .storage import (
    drain_cancellation,
    sha256_digest,
    storage_mutation,
    workspace_mutation,
)
from .stream_wrapper import shell_quote_argv, stream_wrapper_argv
from .windows_media import (
    BASE_DISK_GB,
    VIRTIO_WIN,
    WindowsMedia,
    guest_scripts_archive,
    windows_configure_script,
    windows_edition,
)

logger = structlog.get_logger(__name__)

# Label prefix stored in VM description (JSON metadata)
_LABEL_PREFIX = "opencuria"

_SSH_KEEPALIVE_INTERVAL = 30
_SSH_KEEPALIVE_COUNT_MAX = 5
_SSH_CLOSE_TIMEOUT = 2.0

# Guest filesystem whose usage is reported with the VM metrics.
_GUEST_DISK_PATH = "/workspace"
_GUEST_DISK_USAGE_TIMEOUT = 5.0

# Publication path sent by the control plane; distinct from cloud-image cache.
_BUILD_IMAGE_DIR = Path("/var/lib/opencuria/base-images")


def _workspace_status(state: int) -> str:
    """Normalize every libvirt power state without implying guest usability."""
    return {
        libvirt.VIR_DOMAIN_RUNNING: "running",
        libvirt.VIR_DOMAIN_BLOCKED: "running",
        libvirt.VIR_DOMAIN_PAUSED: "paused",
        libvirt.VIR_DOMAIN_SHUTDOWN: "stopping",
        libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
        libvirt.VIR_DOMAIN_CRASHED: "failed",
        libvirt.VIR_DOMAIN_PMSUSPENDED: "suspended",
    }.get(state, "unknown")


def _parse_df_usage(output: str) -> dict[str, int]:
    """Parse ``df -Pk`` output into guest disk byte counters.

    Returns an empty dict when no data line can be parsed.
    """
    for line in reversed(output.splitlines()):
        fields = line.split()
        if len(fields) < 6:
            continue
        try:
            total_kib, used_kib, free_kib = (int(value) for value in fields[1:4])
        except ValueError:
            continue
        return {
            "guest_disk_total_bytes": total_kib * 1024,
            "guest_disk_used_bytes": used_kib * 1024,
            "guest_disk_free_bytes": free_kib * 1024,
        }
    return {}


@dataclass
class _SSHState:
    """Stable per-instance ownership, retained across lifecycle invalidations."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connection: asyncssh.SSHClientConnection | None = None
    generation: int = 0
    blocked: bool = False


_GUEST_SSH_SESSIONS_TIMEOUT = 15.0
_GUEST_SSH_RELOAD_DIAL_ATTEMPTS = 5
_GUEST_SSH_RELOAD_DIAL_DELAY = 0.5


def _domain_xml(
    name: str,
    vcpus: int,
    memory_mb: int,
    disk_path: str,
    cloud_init_iso: str,
    network: str | None,
    *,
    guest: GuestOS | None = None,
    serial_log: str | None = None,
) -> str:
    """Build a libvirt domain XML definition.

    Each VM is connected to its own dedicated isolated network (created by
    ``QemuRuntime._create_workspace_network``); ``network=None`` defines a guest
    without any NIC. No MAC address is pinned: Linux guests configure their
    static IP from the seed, Windows guests lease the same address via DHCP.
    Guest-specific firmware and devices come from ``guest``; a Linux definition
    is unchanged.
    """
    xml = _base_domain_xml(name, vcpus, memory_mb, disk_path, cloud_init_iso, network)
    if guest is None or guest.name == LINUX:
        return xml
    root = ET.fromstring(xml)
    guest.customize_domain(root, vcpus=vcpus, serial_log=serial_log or "")
    owner = root.find(f"./metadata/{{{METADATA_NS}}}workspace")
    assert owner is not None
    ET.SubElement(owner, f"{{{METADATA_NS}}}guest-os").text = guest.name
    ET.register_namespace("opencuria", METADATA_NS)
    return ET.tostring(root, encoding="unicode")


def _base_domain_xml(
    name: str,
    vcpus: int,
    memory_mb: int,
    disk_path: str,
    cloud_init_iso: str,
    network: str | None,
) -> str:
    """Return the guest-independent domain definition."""
    memory_kb = memory_mb * 1024
    interface = (
        f"""    <interface type='network'>
      <source network='{network}'/>
      <model type='virtio'/>
    </interface>
"""
        if network
        else ""
    )
    return f"""\
<domain type='kvm'>
  <name>{name}</name>
  <memory unit='KiB'>{memory_kb}</memory>
  <vcpu placement='static'>{vcpus}</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough'/>
  <clock offset='utc'/>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' discard='unmap'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{cloud_init_iso}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
{interface}    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <memballoon model='virtio' freePageReporting='on'/>
  </devices>
  <metadata>
    <opencuria:workspace xmlns:opencuria='https://opencuria.dev/libvirt'>
      <opencuria:workspace-id>{name.replace("opencuria-workspace-", "")}</opencuria:workspace-id>
    </opencuria:workspace>
  </metadata>
</domain>
"""


def _enable_free_page_reporting(root: ET.Element) -> bool:
    """Enable returning free guest pages without reducing usable guest RAM.

    Keep Linux's batched reporting and large-page defaults: reclaiming only
    genuinely free pages leaves application memory and the guest cache intact.
    Return whether the persistent definition needs updating.
    """
    devices = root.find("devices")
    if devices is None:
        devices = ET.SubElement(root, "devices")
    balloon = devices.find("memballoon")
    if balloon is None:
        balloon = ET.SubElement(devices, "memballoon", model="virtio")
    elif balloon.get("freePageReporting") == "on" and balloon.get("model") in (
        "virtio", "virtio-transitional", "virtio-non-transitional"
    ):
        return False
    if balloon.get("model") in (None, "none"):
        balloon.set("model", "virtio")
    balloon.set("freePageReporting", "on")
    return True


class QemuRuntime(RuntimeBackend):
    """QEMU/KVM runtime using libvirt for VM lifecycle and asyncssh for exec."""

    def __init__(self, settings: RunnerSettings) -> None:
        import threading

        self._publication_hash_cache: dict[str, tuple[tuple, str]] = {}
        self._publication_hash_lock = threading.Lock()
        self._settings = settings
        self._conn: libvirt.virConnect | None = None
        self._ssh_states: dict[str, _SSHState] = {}
        self._disk_dir = Path(settings.qemu_disk_dir)
        self._snapshot_dir = Path(settings.qemu_snapshot_dir)
        for directory in (
            self._disk_dir,
            self._snapshot_dir,
            Path(settings.qemu_image_cache_dir),
        ):
            self._ensure_host_directory(
                directory,
                writable_hint=(
                    f"sudo install -d -o $USER -g $USER -m 755 '{directory}'"
                ),
            )

        # Load or generate SSH keypair for VM access
        self._ssh_key_path = Path(
            settings.qemu_ssh_key_path or str(self._disk_dir / "opencuria-qemu-key")
        )
        self._ssh_user = settings.qemu_ssh_user
        self._ssh_timeout = settings.qemu_ssh_timeout
        # In-memory TOFU host-key cache: instance_id -> "ip keytype keydata"
        # (known_hosts line format).  Populated on first SSH connect and
        # persisted to disk so it survives runner restarts.
        self._host_key_cache: dict[str, str] = {}
        self._ensure_ssh_keypair()

    # ── Properties ────────────────────────────────────────────────────



    # ── Internal helpers ──────────────────────────────────────────────

    def _ensure_ssh_keypair(self) -> None:
        """Generate an Ed25519 SSH keypair if it doesn't exist."""
        if not self._ssh_key_path.exists():
            import subprocess

            self._ssh_key_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ssh-keygen",
                    "-t",
                    "ed25519",
                    "-f",
                    str(self._ssh_key_path),
                    "-N",
                    "",  # no passphrase
                    "-C",
                    "opencuria-qemu-runtime",
                ],
                check=True,
                capture_output=True,
            )
            logger.info("ssh_keypair_generated", path=str(self._ssh_key_path))

    def _ensure_host_directory(
        self, path: Path, *, writable_hint: str | None = None
    ) -> None:
        """Ensure a host directory exists and is traversable by libvirt-qemu."""
        try:
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o755)
        except PermissionError as exc:
            hint = writable_hint or f"sudo install -d -m 755 '{path}'"
            raise RuntimeError(
                f"Missing permissions for QEMU host directory '{path}'. Run: {hint}"
            ) from exc

    def _get_ssh_public_key(self) -> str:
        """Read the SSH public key."""
        pub_path = self._ssh_key_path.with_suffix(self._ssh_key_path.suffix + ".pub")
        return pub_path.read_text().strip()

    def _libvirt_conn(self) -> libvirt.virConnect:
        """Get or create the libvirt connection."""
        if self._conn is None or not self._conn.isAlive():
            self._conn = libvirt.open("qemu:///system")
        return self._conn

    def _domain_name(self, instance_id: str) -> str:
        """Canonical libvirt domain name for a workspace."""
        return f"opencuria-workspace-{instance_id}"

    def _disk_path(self, instance_id: str) -> Path:
        """Path to the QCOW2 overlay disk for a workspace."""
        return self._disk_dir / f"{instance_id}.qcow2"

    def _cloud_init_iso_path(self, instance_id: str) -> Path:
        """Path to the cloud-init ISO for a workspace."""
        return self._disk_dir / f"{instance_id}-cloud-init.iso"

    @property
    def _guest_cache(self) -> dict[str, str]:
        """instance_id -> guest OS recorded in the domain metadata."""
        cache = self.__dict__.get("_guest_names")
        if cache is None:
            cache = self.__dict__["_guest_names"] = {}
        return cache

    @property
    def _windows(self) -> WindowsMedia:
        """Windows installation media of this runner."""
        media = self.__dict__.get("_windows_media")
        if media is None:
            media = self.__dict__["_windows_media"] = WindowsMedia(self._settings)
        return media

    def _serial_log_path(self, instance_id: str) -> Path:
        """Path of the guest's COM1 log (Windows bootstrap and generalize results)."""
        return self._disk_dir / f"{instance_id}-serial.log"

    def _workspace_owned_paths(self, instance_id: str) -> tuple[Path, ...]:
        """Every host file a workspace owns besides libvirt-managed state."""
        return (
            self._disk_path(instance_id),
            self._cloud_init_iso_path(instance_id),
            self._disk_dir / f"{instance_id}-cloud-init",
            self._host_key_file(instance_id),
            self._serial_log_path(instance_id),
        )

    def _read_guest_name(self, instance_id: str) -> str | None:
        """Read the guest OS from the domain metadata; ``None`` if unreadable."""
        try:
            return self._guest_name_of(self._get_domain(instance_id))
        except Exception:
            return None

    @staticmethod
    def _guest_name_of(domain: libvirt.virDomain) -> str | None:
        """Return a domain's recorded guest OS; domains without one are Linux."""
        try:
            root = ET.fromstring(domain.XMLDesc(0))
        except Exception:
            return None
        name = root.findtext(
            f"./metadata/{{{METADATA_NS}}}workspace/{{{METADATA_NS}}}guest-os"
        )
        if name is None:
            return LINUX
        return name if name in GUEST_OS_VALUES else None

    async def _resolve_guest(self, instance_id: str) -> GuestOS:
        """Return a guest's strategy without blocking the event loop.

        Only a successfully read value is cached; an unreadable domain is
        treated as Linux for this call.
        """
        cached = self._guest_cache.get(instance_id)
        if cached is None:
            try:
                name = await asyncio.to_thread(self._read_guest_name, instance_id)
            except Exception:
                name = None
            if name not in GUEST_OS_VALUES:
                return guest_for(LINUX)
            cached = self._guest_cache[instance_id] = name
        return guest_for(cached)

    def _image_guest(self, image: Path, expected: str | None = None) -> GuestOS:
        """Return the guest OS a published image records (default Linux).

        An explicit ``expected`` value from the control plane must agree.
        """
        marker = image.with_suffix(".manifest.json")
        recorded = LINUX
        if marker.exists():
            try:
                recorded = validate_guest_os(
                    json.loads(marker.read_text()).get("guest_os") or LINUX
                )
            except (ValueError, OSError, AttributeError) as exc:
                raise OperationRejectedError(
                    IMAGE_INVALID, "Image manifest records an unknown guest OS"
                ) from exc
        if expected is not None and validate_guest_os(expected) != recorded:
            raise OperationRejectedError(
                IMAGE_INVALID, "Image guest OS does not match the request"
            )
        return guest_for(recorded)

    def _get_domain(self, instance_id: str) -> libvirt.virDomain:
        """Look up a libvirt domain by instance_id."""
        conn = self._libvirt_conn()
        name = self._domain_name(instance_id)
        try:
            return conn.lookupByName(name)
        except libvirt.libvirtError as exc:
            raise RuntimeError(f"VM '{name}' not found: {exc}") from exc

    def _ensure_memory_reclaim(self, domain: libvirt.virDomain) -> None:
        """Upgrade an existing stopped VM before boot, preserving its devices."""
        root = ET.fromstring(domain.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE))
        if _enable_free_page_reporting(root):
            self._libvirt_conn().defineXML(ET.tostring(root, encoding="unicode"))
            logger.info("vm_free_page_reporting_enabled", domain=domain.name())

    async def _create_overlay_disk(
        self,
        instance_id: str,
        *,
        disk_size_gb: int,
        base_image: str | None = None,
    ) -> Path:
        """Create a QCOW2 overlay backed by the base image.

        Every refusal precedes the first write, so callers may rely on
        ``OperationRejectedError`` meaning that no disk was allocated.
        """
        disk = self._disk_path(instance_id)
        if not base_image:
            raise OperationRejectedError(
                IMAGE_UNAVAILABLE,
                "QEMU overlay creation requires an explicit base image",
            )
        if disk.exists() or disk.is_symlink():
            raise OperationRejectedError(
                WORKSPACE_EXISTS, f"Workspace disk already exists: {disk}"
            )
        backing = str(Path(base_image).absolute())
        if not Path(backing).exists():
            raise OperationRejectedError(
                IMAGE_UNAVAILABLE, f"QEMU base image not found: {backing}"
            )

        info = await self._image_info(Path(backing))
        if info.get("format") != "qcow2":
            raise OperationRejectedError(IMAGE_INVALID, "QEMU base image must be QCOW2")
        disk_size_bytes = disk_size_gb * 1024**3
        if disk_size_bytes < int(info["virtual-size"]):
            minimum = (int(info["virtual-size"]) + 1024**3 - 1) // 1024**3
            raise OperationRejectedError(
                DISK_BELOW_IMAGE_MINIMUM,
                f"Requested QEMU disk size is smaller than image minimum {minimum} GiB",
            )
        temp = disk.with_name(disk.name + ".tmp-" + uuid.uuid4().hex)
        cmd = [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            backing,
            str(temp),
            str(disk_size_bytes),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create overlay disk: {stderr.decode()}")

        def commit_overlay() -> None:
            # Exclusive publication also protects against another runner process
            # or an operator creating the final path after our initial check.
            temp.chmod(0o644)
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(temp, disk)
            temp.unlink()
            directory = os.open(disk.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        await asyncio.to_thread(commit_overlay)
        return disk

    def _resolve_build_base_image(self, base_distro: str) -> Path:
        """Resolve or download the QEMU base image for a distro string."""
        distro = (base_distro or "").strip().lower()
        ubuntu_cloud = Path("/var/lib/opencuria/images/ubuntu-base.img")
        if distro.startswith("ubuntu:"):
            version = distro.split(":", 1)[1].strip()
            if version == "22.04" and ubuntu_cloud.exists():
                return ubuntu_cloud
            return self._ensure_ubuntu_cloud_image(version)

        raise RuntimeError(
            "Unsupported QEMU base distro "
            f"'{base_distro or 'unknown'}'. Use ubuntu:<version>."
        )

    def _ensure_ubuntu_cloud_image(self, version: str) -> Path:
        """Return a cached Ubuntu cloud image, downloading it on first use."""
        normalized_version = version.strip()
        if not re.fullmatch(r"\d{2}\.\d{2}", normalized_version):
            raise RuntimeError(
                f"Unsupported Ubuntu base distro '{version}'. "
                "Use values like 'ubuntu:22.04' or 'ubuntu:24.04'."
            )

        images_dir = Path(self._settings.qemu_image_cache_dir)
        self._ensure_host_directory(
            images_dir,
            writable_hint=f"sudo install -d -o $USER -g $USER -m 755 '{images_dir}'",
        )
        target = images_dir / f"ubuntu-{normalized_version}-server-cloudimg-amd64.img"
        def validate(path: Path) -> None:
            if path.is_symlink():
                raise RuntimeError("Symlink cloud image refused")
            proc = subprocess.run(
                ["qemu-img", "info", "--output=json", str(path)],
                capture_output=True,
                check=True,
                timeout=60,
            )
            info = json.loads(proc.stdout)
            if info.get("format") != "qcow2" or info.get("backing-filename"):
                raise RuntimeError("Cached cloud image is invalid; preserve for inspection")

        if target.exists():
            validate(target)
            return target

        url = (
            "https://cloud-images.ubuntu.com/releases/server/"
            f"{normalized_version}/release/"
            f"ubuntu-{normalized_version}-server-cloudimg-amd64.img"
        )
        tmp_target = target.with_suffix(f"{target.suffix}.{uuid.uuid4().hex}.tmp")
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                with tmp_target.open("wb") as f:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            validate(tmp_target)
            tmp_target.chmod(0o644)
            with tmp_target.open("rb") as downloaded:
                os.fsync(downloaded.fileno())
            try:
                os.link(tmp_target, target)
            except FileExistsError:
                # Another downloader may have published this distro meanwhile.
                # Never replace an inode which may already back an active VM.
                validate(target)
            tmp_target.unlink()
            directory = os.open(images_dir, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception as exc:
            if tmp_target.exists():
                tmp_target.unlink()
            raise RuntimeError(
                f"Failed to download Ubuntu cloud image for {normalized_version}: {exc}"
            ) from exc

        logger.info(
            "qemu_base_image_downloaded",
            version=normalized_version,
            path=str(target),
            url=url,
        )
        return target

    async def _cleanup_build_instance(self, instance_id: str) -> None:
        """Best-effort cleanup for temporary QEMU image build VMs."""
        try:
            await self.remove_workspace(instance_id)
        except Exception:
            logger.warning("qemu_image_build_cleanup_failed", instance_id=instance_id)

    async def _stream_ssh_process(
        self,
        instance_id: str,
        command: str,
        progress_callback,
    ) -> None:
        """Run an SSH command and stream combined output line-by-line."""
        ssh = await self._get_ssh(instance_id)
        process = await ssh.create_process(
            command,
            stdin=asyncssh.DEVNULL,
            stderr=asyncssh.STDOUT,
        )
        assert process.stdout is not None
        async for line in process.stdout:
            if progress_callback is not None:
                await progress_callback(line.rstrip("\n"))
        await process.wait_closed()
        exit_code = self._require_exit_status(process)
        if exit_code != 0:
            raise OperationError(
                BUILD_SCRIPT_FAILED,
                f"QEMU image build failed with exit code {exit_code}",
            )

    async def _create_cloud_init_iso(
        self,
        instance_id: str,
        vm_ip: str | None = None,
        gateway: str | None = None,
        *,
        guest: GuestOS | None = None,
        action: str = "boot",
    ) -> Path:
        """Create the NoCloud seed ISO (volume ``cidata``) for VM bootstrap.

        The guest strategy decides the files: cloud-init ``user-data``,
        ``meta-data`` and a static ``network-config`` for Linux, ``meta-data``
        plus ``opencuria.json`` for the Windows bootstrap. ``action`` is
        ``generalize`` only for disposable copies that are prepared for capture.

        Each new seed has its own bootstrap identity, independent of the
        stable workspace ID. This invalidates cached bootstrap state in
        captured images, including same-workspace resets. Normal starts
        reuse the existing ISO and therefore keep their bootstrap identity.
        """
        guest = guest or guest_for(LINUX)
        iso_path = self._cloud_init_iso_path(instance_id)
        work_dir = self._disk_dir / f"{instance_id}-cloud-init"
        public_key = await asyncio.to_thread(self._get_ssh_public_key)
        files = guest.seed_files(
            SeedSpec(
                bootstrap_id=str(uuid.uuid4()),
                public_key=public_key,
                vm_ip=vm_ip,
                gateway=gateway,
                action="generalize" if action == "generalize" else "boot",
            )
        )

        def write_seed() -> list[str]:
            work_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for name, content in files.items():
                (work_dir / name).write_text(content)
                paths.append(str(work_dir / name))
            return paths

        files_to_include = await asyncio.to_thread(write_seed)

        # Use genisoimage or mkisofs to create the ISO
        for tool in ("genisoimage", "mkisofs"):
            if shutil.which(tool):
                cmd = [
                    tool,
                    "-output",
                    str(iso_path),
                    "-volid",
                    "cidata",
                    "-joliet",
                    "-rock",
                    *files_to_include,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    raise RuntimeError(
                        f"Failed to create cloud-init ISO: {stderr.decode()}"
                    )
                break
        else:
            raise RuntimeError(
                "Neither genisoimage nor mkisofs found — "
                "install one to create cloud-init ISOs"
            )

        # libvirt-qemu runs as a different uid — ensure it can read the ISO.
        await asyncio.to_thread(iso_path.chmod, 0o644)

        # Clean up temp dir
        await asyncio.to_thread(shutil.rmtree, work_dir, ignore_errors=True)
        return iso_path

    # ── Per-workspace network helpers ─────────────────────────────────

    def _workspace_network_name(self, instance_id: str) -> str:
        """Libvirt network name for this workspace's isolated network."""
        return f"opencuria-ws-{instance_id}"

    def _get_existing_workspace_gateways(self) -> set[str]:
        """Return gateway IPs of all active opencuria workspace networks."""
        conn = self._libvirt_conn()
        gateways: set[str] = set()
        for net in conn.listAllNetworks(0):
            if not net.name().startswith("opencuria-ws-"):
                continue
            root = ET.fromstring(net.XMLDesc(0))
            for ip_elem in root.findall("./ip"):
                addr = ip_elem.get("address")
                if addr:
                    gateways.add(addr)
        return gateways

    def _get_reserved_ipv4_networks(self) -> list[ipaddress.IPv4Network]:
        """Collect host + libvirt IPv4 networks that must not be reused.

        This prevents libvirt define/create failures when a candidate subnet
        overlaps with the host uplink (for example ``enp1s0``) or another
        existing libvirt network.
        """
        reserved: list[ipaddress.IPv4Network] = []

        # Incomplete host or libvirt observations never prove a subnet free.
        result = subprocess.run(
            ["ip", "-o", "-f", "inet", "addr", "show"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 4:
                raise RuntimeError("Invalid host IPv4 interface observation")
            reserved.append(ipaddress.IPv4Network(parts[3], strict=False))

        for net in self._libvirt_conn().listAllNetworks(0):
            root = ET.fromstring(net.XMLDesc(0))
            for ip_elem in root.findall("./ip"):
                address = ip_elem.get("address")
                if not address or ip_elem.get("family") == "ipv6":
                    continue
                parsed = ipaddress.ip_address(address)
                if parsed.version != 4:
                    continue
                prefix = ip_elem.get("prefix") or ip_elem.get("netmask")
                if not prefix:
                    raise RuntimeError("Libvirt IPv4 network prefix is unknown")
                reserved.append(ipaddress.IPv4Network(f"{address}/{prefix}", strict=False))

        return reserved

    def _pick_workspace_subnet(self) -> tuple[str, str, str]:
        """Find an unused /30 subnet in 10.100.0.0/16.

        Scans existing opencuria workspace networks and picks the first
        unused /30 block.  Within each /30::

            base      = 10.100.X.(4k)
            gateway   = 10.100.X.(4k+1)   ← host / NAT router
            vm_ip     = 10.100.X.(4k+2)   ← the guest VM
            broadcast = 10.100.X.(4k+3)

        Returns ``(gateway_ip, vm_ip, netmask)`` for the chosen block.
        """
        existing_gateways = self._get_existing_workspace_gateways()
        reserved_networks = self._get_reserved_ipv4_networks()
        for third in range(0, 256):
            for block_k in range(0, 64):  # 64 /30 blocks per /24
                gateway = f"10.100.{third}.{block_k * 4 + 1}"
                vm_ip = f"10.100.{third}.{block_k * 4 + 2}"
                candidate = ipaddress.IPv4Network(f"{gateway}/30", strict=False)
                if gateway in existing_gateways:
                    continue
                if any(candidate.overlaps(network) for network in reserved_networks):
                    continue
                return gateway, vm_ip, "255.255.255.252"
        raise RuntimeError(
            "No available /30 subnet in 10.100.0.0/16 — all 16 384 blocks in use"
        )

    async def _create_workspace_network(
        self, instance_id: str, *, dhcp: bool = False
    ) -> tuple[str, str]:
        """Create a dedicated isolated /30 NAT network for this workspace.

        Each workspace gets its own libvirt network with a unique /30 subnet
        in the ``10.100.0.0/16`` range.  The network uses NAT so the VM
        can reach the internet, but VMs on different networks cannot reach
        each other because they are on separate bridge interfaces. With
        ``dhcp`` the network leases exactly the deterministic guest address,
        so a guest that configures its NIC by DHCP gets the same IP.

        Returns ``(gateway_ip, vm_ip)``.
        """

        def _create() -> tuple[str, str]:
            conn = self._libvirt_conn()
            if any(
                domain.name() == self._domain_name(instance_id)
                for domain in conn.listAllDomains(0)
            ):
                raise FileExistsError("Workspace domain already exists")
            gateway, vm_ip, netmask = self._pick_workspace_subnet()
            net_name = self._workspace_network_name(instance_id)
            lease = (
                f"<dhcp><range start='{vm_ip}' end='{vm_ip}'/></dhcp>" if dhcp else ""
            )
            net_xml = f"""\
<network>
  <name>{net_name}</name>
  <bridge stp='on' delay='0'/>
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <dns enable='yes' forwardPlainNames='yes'/>
  <ip address='{gateway}' netmask='{netmask}'>{lease}</ip>
</network>"""
            conn = self._libvirt_conn()
            if any(net.name() == net_name for net in conn.listAllNetworks(0)):
                raise FileExistsError(f"Workspace network already exists: {net_name}")
            net = conn.networkDefineXML(net_xml)
            net.setAutostart(1)
            net.create()
            return gateway, vm_ip

        gateway, vm_ip = await asyncio.to_thread(_create)
        logger.info(
            "vm_network_created",
            instance_id=instance_id,
            network=self._workspace_network_name(instance_id),
            gateway=gateway,
            vm_ip=vm_ip,
        )
        return gateway, vm_ip

    async def _destroy_workspace_network(self, instance_id: str) -> None:
        """Stop and undefine the per-workspace libvirt network."""
        net_name = self._workspace_network_name(instance_id)

        def _destroy() -> None:
            conn = self._libvirt_conn()
            try:
                net = conn.networkLookupByName(net_name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() == libvirt.VIR_ERR_NO_NETWORK:
                    return
                raise
            if net.isActive():
                net.destroy()
            net.undefine()
            if any(net.name() == net_name for net in conn.listAllNetworks(0)):
                raise RuntimeError("Network removal could not be verified")

        await asyncio.to_thread(_destroy)
        logger.info("vm_network_destroyed", instance_id=instance_id, network=net_name)

    def _get_workspace_vm_ip(self, instance_id: str) -> str:
        """Derive the VM's IP from its dedicated libvirt network XML.

        The gateway address is stored in the network definition; the VM IP
        is always ``gateway + 1`` within the /30 block.
        """
        net_name = self._workspace_network_name(instance_id)
        conn = self._libvirt_conn()
        try:
            net = conn.networkLookupByName(net_name)
            root = ET.fromstring(net.XMLDesc(0))
            for ip_elem in root.findall("./ip"):
                gw = ip_elem.get("address", "")
                if gw:
                    parts = gw.split(".")
                    if len(parts) == 4:
                        vm_fourth = int(parts[3]) + 1
                        return f"{parts[0]}.{parts[1]}.{parts[2]}.{vm_fourth}"
        except (libvirt.libvirtError, ET.ParseError, ValueError):
            pass
        raise RuntimeError(
            f"Cannot determine VM IP for workspace {instance_id} "
            f"(network '{net_name}' not found or has no IP)"
        )

    # ── SSH TOFU host-key helpers ──────────────────────────────────────

    def _host_key_file(self, instance_id: str) -> Path:
        """Path to the persisted known-hosts entry for this workspace's VM."""
        return self._disk_dir / f"opencuria-hostkey-{instance_id}"

    def _load_tofu_key(
        self, instance_id: str, ip: str
    ) -> asyncssh.SSHKnownHosts | None:
        """Return a known-hosts validator for reconnect verification.

        Loads the stored host key (memory cache → disk file) and wraps it
        in an ``SSHKnownHosts`` object that asyncssh uses to verify the
        server's identity.  Returns ``None`` if no key has been stored yet
        (i.e. this is the very first connection — TOFU applies).
        """
        entry = self._host_key_cache.get(instance_id)
        if not entry:
            key_file = self._host_key_file(instance_id)
            if key_file.exists():
                entry = key_file.read_text().strip()
                self._host_key_cache[instance_id] = entry

        if not entry:
            return None

        # The entry is stored as "ip keytype base64key"; substitute the
        # current IP so the lookup always matches (IP is static but defensive).
        parts = entry.split(" ", 1)
        if len(parts) == 2:
            entry = f"{ip} {parts[1]}"

        try:
            return asyncssh.import_known_hosts(entry)
        except Exception:
            return None

    async def _save_tofu_key(
        self,
        instance_id: str,
        ip: str,
        conn: asyncssh.SSHClientConnection,
    ) -> None:
        """Persist the server's host key after the first successful connect.

        Stores the key both in the in-memory cache and on disk so that all
        subsequent connections can verify the VM's identity (TOFU model).
        """
        try:
            server_key = conn.get_server_host_key()
            if server_key is None:
                return
            key_bytes = server_key.export_public_key()
            key_str = key_bytes.decode().strip()
            entry = f"{ip} {key_str}"

            self._host_key_cache[instance_id] = entry
            key_file = self._host_key_file(instance_id)
            key_file.write_text(entry + "\n")
            key_file.chmod(0o600)

            logger.info(
                "vm_host_key_saved",
                instance_id=instance_id,
                ip=ip,
                key_type=key_str.split()[0] if " " in key_str else "unknown",
            )
        except Exception as exc:
            logger.warning(
                "vm_host_key_save_failed",
                instance_id=instance_id,
                error=str(exc),
            )

    def _ssh_state(self, instance_id: str) -> _SSHState:
        """Keep lock identity stable for queued acquires across stop/recreate."""
        state = self._ssh_states.get(instance_id)
        if state is None:
            state = _SSHState()
            self._ssh_states[instance_id] = state
        return state

    @staticmethod
    def _check_ssh_generation(state: _SSHState, generation: int) -> None:
        if state.blocked or state.generation != generation:
            raise RuntimeError("SSH lifecycle changed while connecting")

    @staticmethod
    async def _close_ssh_connection(conn: asyncssh.SSHClientConnection) -> None:
        """Close an owned transport without delaying lifecycle work indefinitely."""
        conn.close()
        try:
            await asyncio.wait_for(conn.wait_closed(), timeout=_SSH_CLOSE_TIMEOUT)
        except (OSError, asyncssh.Error, asyncio.TimeoutError):
            conn.abort()
        except asyncio.CancelledError:
            conn.abort()
            raise

    async def _invalidate_ssh(self, instance_id: str) -> None:
        """Fence late connects before awaiting deliberate transport shutdown."""
        state = self._ssh_state(instance_id)
        state.generation += 1
        state.blocked = True
        conn, state.connection = state.connection, None
        if conn is not None:
            await self._close_ssh_connection(conn)

    async def _connect_ssh(
        self,
        instance_id: str,
        state: _SSHState,
        generation: int,
        timeout: float,
    ) -> asyncssh.SSHClientConnection:
        """Connect under the state lock; never publish a stale candidate."""
        self._check_ssh_generation(state, generation)
        cached = state.connection
        if cached is not None and not cached.is_closed():
            return cached
        state.connection = None
        conn = await self._open_ssh_transport(instance_id, state, generation, timeout)
        state.connection = conn
        return conn

    async def _open_ssh_transport(
        self,
        instance_id: str,
        state: _SSHState,
        generation: int,
        timeout: float,
    ) -> asyncssh.SSHClientConnection:
        """Open a fenced transport that carries the guest's raised session cap.

        A connection keeps the limits sshd had when it was accepted, so after
        installing the cap on an older guest the fresh, still unshared
        transport is replaced once.
        """
        conn = await self._dial_ssh(instance_id, state, generation, timeout)
        try:
            reloaded = await self._ensure_guest_session_cap(instance_id, conn)
            self._check_ssh_generation(state, generation)
        except BaseException:
            await self._close_ssh_connection(conn)
            raise
        if not reloaded:
            return conn
        await self._close_ssh_connection(conn)
        # sshd re-executes on reload; give it a moment to listen again.
        attempts = _GUEST_SSH_RELOAD_DIAL_ATTEMPTS
        while True:
            await asyncio.sleep(_GUEST_SSH_RELOAD_DIAL_DELAY)
            self._check_ssh_generation(state, generation)
            try:
                return await self._dial_ssh(instance_id, state, generation, timeout)
            except (OSError, asyncssh.Error, asyncio.TimeoutError):
                attempts -= 1
                if attempts <= 0:
                    raise

    @staticmethod
    async def _ensure_guest_session_cap(
        instance_id: str, conn: asyncssh.SSHClientConnection
    ) -> bool:
        """Install the session cap once; return whether sshd was reloaded.

        Best effort: guests that cannot take the drop-in keep working with
        their own limits, so failures never block the connection. Windows
        images carry the cap in their sshd configuration; the script exits
        early there because the drop-in directory does not exist.
        """
        try:
            result = await asyncio.wait_for(
                conn.run(_GUEST_SSH_SESSIONS_SCRIPT, check=False),
                _GUEST_SSH_SESSIONS_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "vm_ssh_session_cap_unverified", instance_id=instance_id, exc_info=True
            )
            return False
        if result.exit_status != 0:
            logger.warning(
                "vm_ssh_session_cap_not_applied",
                instance_id=instance_id,
                exit_status=result.exit_status,
            )
            return False
        reloaded = _GUEST_SSH_SESSIONS_RELOADED in str(result.stdout or "")
        if reloaded:
            logger.info(
                "vm_ssh_session_cap_applied",
                instance_id=instance_id,
                max_sessions=_GUEST_SSH_MAX_SESSIONS,
            )
        return reloaded

    async def _dial_ssh(
        self,
        instance_id: str,
        state: _SSHState,
        generation: int,
        timeout: float,
    ) -> asyncssh.SSHClientConnection:
        """Authenticate with one host-key policy, keepalive and generation fence."""
        ip = await self._get_vm_ip(instance_id)
        self._check_ssh_generation(state, generation)
        known_hosts = await asyncio.to_thread(self._load_tofu_key, instance_id, ip)
        self._check_ssh_generation(state, generation)
        try:
            conn = await asyncssh.connect(
                ip,
                username=self._ssh_user,
                client_keys=[str(self._ssh_key_path)],
                known_hosts=known_hosts,
                connect_timeout=timeout,
                keepalive_interval=_SSH_KEEPALIVE_INTERVAL,
                keepalive_count_max=_SSH_KEEPALIVE_COUNT_MAX,
            )
        except asyncssh.HostKeyNotVerifiable as exc:
            raise RuntimeError(
                f"SSH host key mismatch for VM {instance_id} ({ip}) — "
                "possible MITM or VM was recreated. "
                f"Delete {self._host_key_file(instance_id)} to reset."
            ) from exc
        try:
            self._check_ssh_generation(state, generation)
            if not self._host_key_cache.get(instance_id):
                await self._save_tofu_key(instance_id, ip, conn)
            self._check_ssh_generation(state, generation)
            return conn
        except BaseException:
            await self._close_ssh_connection(conn)
            raise

    async def _wait_for_ssh(self, instance_id: str) -> str:
        """Enable SSH after VM startup and wait for authenticated readiness.

        Initial setup and subsequent starts share connection ownership and
        persisted host-key verification with normal reconnects. Only explicit
        startup reopens admission after a lifecycle invalidation.
        """
        state = self._ssh_state(instance_id)
        generation = state.generation
        state.blocked = False
        # Guests are recorded when this runtime defines or starts them.
        guest = guest_for(self._guest_cache.get(instance_id))
        ssh_timeout = max(self._ssh_timeout, guest.ssh_timeout_floor)
        deadline = asyncio.get_running_loop().time() + ssh_timeout
        async with state.lock:
            self._check_ssh_generation(state, generation)
            while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
                try:
                    await self._connect_ssh(
                        instance_id, state, generation, min(5, remaining)
                    )
                    vm_ip = await asyncio.to_thread(
                        self._get_workspace_vm_ip, instance_id
                    )
                    logger.info("vm_ssh_ready", instance_id=instance_id, ip=vm_ip)
                    return vm_ip
                except (OSError, asyncssh.Error, asyncio.TimeoutError):
                    self._check_ssh_generation(state, generation)
                remaining = deadline - asyncio.get_running_loop().time()
                await asyncio.sleep(min(2, max(0, remaining)))
                self._check_ssh_generation(state, generation)
        raise RuntimeError(
            f"VM {instance_id} did not become reachable via SSH within {ssh_timeout}s"
        )

    async def _wait_for_cloud_init(self, instance_id: str) -> None:
        """Wait for first-boot completion before provisioning or admitting work.

        SSH can start before the guest's first-boot configuration finishes
        (cloud-init's final modules and SSH restart on Linux, the bootstrap
        task on Windows). Only this read-only status probe may be retried after
        a bootstrap disconnect; the actual image script is issued exactly once
        afterwards.
        """
        guest = await self._resolve_guest(instance_id)
        timeout = max(
            getattr(self._settings, "qemu_cloud_init_timeout", 300),
            guest.first_boot_timeout_floor,
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                async def probe():
                    ssh = await self._get_ssh(instance_id)
                    return await ssh.run(guest.first_boot_probe(), check=False)

                result = await asyncio.wait_for(probe(), min(10, remaining))
                state = guest.first_boot_state(result.exit_status, str(result.stdout or ""))
                if state == "degraded":
                    logger.warning("vm_cloud_init_degraded", instance_id=instance_id)
                if state in ("done", "degraded"):
                    return
            except GuestBootstrapError as exc:
                raise RuntimeError(str(exc)) from exc
            except (OSError, asyncssh.Error, asyncio.TimeoutError):
                # cloud-init's final ssh restart can close the first transport.
                # The next acquisition reconnects once it is observed closed.
                pass
            except RuntimeError as exc:
                cause = exc.__cause__
                if not isinstance(cause, (OSError, asyncssh.Error, asyncio.TimeoutError)):
                    raise
                if isinstance(cause, asyncssh.HostKeyNotVerifiable):
                    raise
            except (ValueError, TypeError) as exc:
                raise RuntimeError("Invalid guest cloud-init status") from exc
            await asyncio.sleep(min(1, max(0, deadline - loop.time())))
        raise RuntimeError(f"Guest cloud-init did not complete within {timeout}s")

    async def _get_ssh(self, instance_id: str) -> asyncssh.SSHClientConnection:
        """Reuse open transports; only reconnect after an observed disconnect.

        Exec/channel failures say nothing about transport liveness. Protocol
        keepalives detect silent failures without consuming a session channel
        or tearing down streams because a guest command was slow. No issued
        command is ever replayed on the replacement transport.
        """
        state = self._ssh_state(instance_id)
        generation = state.generation
        self._check_ssh_generation(state, generation)
        async with state.lock:
            try:
                return await self._connect_ssh(instance_id, state, generation, 10)
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
                raise RuntimeError(
                    f"SSH reconnect to VM {instance_id} failed: {exc}"
                ) from exc

    async def _get_vm_ip(self, instance_id: str) -> str:
        """Get the IP address of a running VM.

        With per-workspace isolated networks the VM IP is always deterministic
        (second host in the /30 block) and can be read directly from the
        network's libvirt XML — no DHCP lease lookup required.
        """
        return await asyncio.to_thread(self._get_workspace_vm_ip, instance_id)

    def get_guest_ip(self, instance_id: str, workspace_id: str) -> str:
        """Return the VM IP used by the runner desktop proxy.

        """
        del workspace_id
        return self._get_workspace_vm_ip(instance_id)


    async def desktop_kind(self, instance_id: str) -> str:
        """Return where a guest's desktop is served (``guest`` or ``qemu``)."""
        return (await self._resolve_guest(instance_id)).desktop

    async def open_display(
        self, instance_id: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a stream to QEMU's display of a running guest.

        libvirt hands over a connected socket, so no VNC port is ever listening
        on the host and no other process can reach the display.
        """

        def open_fd() -> int:
            domain = self._get_domain(instance_id)
            return domain.openGraphicsFD(0, libvirt.VIR_DOMAIN_OPEN_GRAPHICS_SKIPAUTH)

        fd = await asyncio.to_thread(open_fd)
        sock = socket.socket(fileno=fd)
        try:
            return await asyncio.open_connection(sock=sock)
        except BaseException:
            sock.close()
            raise

    async def set_display_size(self, instance_id: str, width: int, height: int) -> bool:
        """Ask a Windows guest to switch its console to ``width`` x ``height``.

        Best effort: the change runs in the logged-on session and reports back
        through ``display.result``; unsupported modes keep the current size.
        """
        if (await self._resolve_guest(instance_id)).desktop != "qemu":
            return False
        command = (
            f"printf '{{\"width\": %d, \"height\": %d}}' {int(width)} {int(height)}"
            f" > {WINDOWS_STATE_DIR}/display.json"
            f" && rm -f {WINDOWS_STATE_DIR}/display.result"
            " && MSYS2_ARG_CONV_EXCL='*' schtasks.exe /run /tn 'OpenCuria Display' >/dev/null"
            f" && for _ in $(seq 50); do [ -s {WINDOWS_STATE_DIR}/display.result ] && break;"
            " sleep 0.2; done"
            f" && cat {WINDOWS_STATE_DIR}/display.result"
        )
        try:
            code, output = await asyncio.wait_for(
                self.exec_command_wait(instance_id, ["sh", "-c", command]), 30
            )
        except Exception:
            logger.warning("vm_display_size_failed", instance_id=instance_id, exc_info=True)
            return False
        applied = code == 0 and output.strip() == "0"
        logger.info(
            "vm_display_size", instance_id=instance_id, width=width, height=height,
            applied=applied,
        )
        return applied

    @contextlib.asynccontextmanager
    async def display_client(self, instance_id: str):
        """Yield a negotiated RFB control connection to QEMU's display."""
        reader, writer = await self.open_display(instance_id)
        client = RFBClient(reader, writer)
        try:
            await asyncio.wait_for(client.handshake(), 10)
            yield client
        finally:
            await client.close()

    # ── RuntimeBackend implementation ─────────────────────────────────

    def _assert_workspace_domain_absent(self, instance_id: str) -> None:
        """Reject fresh allocation if an exact domain already owns this identity."""
        if any(
            domain.name() == self._domain_name(instance_id)
            for domain in self._libvirt_conn().listAllDomains(0)
        ):
            raise OperationRejectedError(
                WORKSPACE_EXISTS, "Workspace domain already exists"
            )

    @staticmethod
    def _validate_workspace_resources(vcpus: int, memory_mb: int, disk_gb: int) -> None:
        """Reject malformed resource values before allocation.

        Host capacity admission belongs to the control plane; the runner only
        rejects values libvirt cannot represent.
        """
        if min(vcpus, memory_mb, disk_gb) <= 0:
            raise OperationRejectedError(
                INVALID_RESOURCES, "QEMU workspace resources must be positive"
            )

    @workspace_mutation
    async def create_workspace(self, config: WorkspaceConfig) -> str:
        """Create one guest without blocking other guests during SSH readiness."""
        instance_id = await self._create_workspace(config)
        await self._wait_for_ssh(instance_id)
        await self._wait_for_cloud_init(instance_id)
        return instance_id

    @storage_mutation
    async def _create_workspace(self, config: WorkspaceConfig) -> str:
        """Create a new QEMU/KVM VM workspace.

        Each workspace gets its own dedicated /30 NAT network so VMs are
        fully isolated from one another at the network level.
        """
        instance_id = config.workspace_id
        await asyncio.to_thread(self._assert_workspace_domain_absent, instance_id)
        qemu_vcpus = (
            self._settings.qemu_vcpus if config.qemu_vcpus is None else config.qemu_vcpus
        )
        qemu_memory_mb = (
            self._settings.qemu_memory_mb
            if config.qemu_memory_mb is None else config.qemu_memory_mb
        )
        qemu_disk_size_gb = (
            self._settings.qemu_disk_size_gb
            if config.qemu_disk_size_gb is None else config.qemu_disk_size_gb
        )
        self._validate_workspace_resources(
            qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb
        )

        disk = self._disk_path(instance_id)
        if await asyncio.to_thread(lambda: disk.exists() or disk.is_symlink()):
            raise OperationRejectedError(
                WORKSPACE_EXISTS, f"Workspace disk already exists: {disk}"
            )

        guest = await asyncio.to_thread(
            self._image_guest, Path(config.image or ""), config.guest_os
        )
        # Validate the base and publish its overlay before allocating a network.
        disk = await self._create_overlay_disk(
            instance_id,
            disk_size_gb=qemu_disk_size_gb,
            base_image=config.image,
        )
        await self._define_and_start_guest(
            instance_id,
            guest=guest,
            disk=disk,
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
        )

        logger.info(
            "vm_workspace_created",
            instance_id=instance_id,
            network=self._workspace_network_name(instance_id),
            guest_os=guest.name,
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
            disk_size_gb=qemu_disk_size_gb,
        )
        return instance_id

    async def _define_and_start_guest(
        self,
        instance_id: str,
        *,
        guest: GuestOS,
        disk: Path,
        vcpus: int,
        memory_mb: int,
        network: bool = True,
        seed_action: str = "boot",
    ) -> None:
        """Allocate network and seed for a published disk, then boot the guest.

        Shared by workspace creation, clones, image builds and capture copies;
        the guest strategy decides firmware, devices, DHCP and seed contents.
        """
        vm_ip = gateway = None
        network_name = None
        if network:
            gateway, vm_ip = await self._create_workspace_network(
                instance_id, **({"dhcp": True} if guest.dhcp else {})
            )
            network_name = self._workspace_network_name(instance_id)
        cloud_init_iso = await self._create_cloud_init_iso(
            instance_id, vm_ip=vm_ip, gateway=gateway, guest=guest, action=seed_action
        )
        domain_xml = _domain_xml(
            name=self._domain_name(instance_id),
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_path=str(disk),
            cloud_init_iso=str(cloud_init_iso),
            network=network_name,
            guest=guest,
            serial_log=(
                None if guest.name == LINUX else str(self._serial_log_path(instance_id))
            ),
        )

        def _define_and_start() -> None:
            conn = self._libvirt_conn()
            dom = conn.defineXML(domain_xml)
            # A fresh definition never inherits stale firmware variables.
            if guest.name != LINUX:
                dom.createWithFlags(libvirt.VIR_DOMAIN_START_RESET_NVRAM)
            else:
                dom.create()  # start the VM

        self._guest_cache[instance_id] = guest.name
        await asyncio.to_thread(_define_and_start)

    @workspace_mutation
    async def stop_workspace(self, instance_id: str) -> None:
        """Gracefully shut down the VM (ACPI shutdown)."""
        await self._invalidate_ssh(instance_id)

        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        if state in (
            libvirt.VIR_DOMAIN_RUNNING,
            libvirt.VIR_DOMAIN_BLOCKED,
            libvirt.VIR_DOMAIN_SHUTDOWN,
        ):
            if state != libvirt.VIR_DOMAIN_SHUTDOWN:
                await asyncio.to_thread(domain.shutdown)
            # Wait for shutdown with timeout
            guest = await self._resolve_guest(instance_id)
            for _ in range(int(guest.shutdown_timeout)):
                await asyncio.sleep(1)
                state, _ = await asyncio.to_thread(domain.state)
                if state == libvirt.VIR_DOMAIN_SHUTOFF:
                    break
            else:
                # Force destroy if graceful shutdown fails
                await asyncio.to_thread(domain.destroy)
        elif state != libvirt.VIR_DOMAIN_SHUTOFF:
            # A paused/pmsuspended guest cannot process an ACPI shutdown.
            await asyncio.to_thread(domain.destroy)
        state, _ = await asyncio.to_thread(domain.state)
        if state != libvirt.VIR_DOMAIN_SHUTOFF:
            raise RuntimeError("Guest shutdown could not be verified")

        logger.info("vm_workspace_stopped", instance_id=instance_id)

    @workspace_mutation
    async def start_workspace(self, instance_id: str) -> None:
        """Start a guest, then wait for its SSH readiness independently."""
        if await self._start_workspace(instance_id):
            await self._wait_for_ssh(instance_id)
        logger.info("vm_workspace_started", instance_id=instance_id)

    @storage_mutation
    async def _start_workspace(self, instance_id: str, *, upgrade: bool = True) -> bool:
        """Activate the domain; return whether it was not already running."""
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        if state in (libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_BLOCKED):
            return False
        if state not in (
            libvirt.VIR_DOMAIN_PAUSED,
            libvirt.VIR_DOMAIN_PMSUSPENDED,
            libvirt.VIR_DOMAIN_SHUTOFF,
        ):
            raise RuntimeError(f"Guest cannot start from libvirt state {state}")
        # An externally stopped guest may leave a silent cached transport.
        await self._invalidate_ssh(instance_id)
        if instance_id not in self._guest_cache:
            name = await asyncio.to_thread(self._guest_name_of, domain)
            if name in GUEST_OS_VALUES:
                self._guest_cache[instance_id] = name
        if state == libvirt.VIR_DOMAIN_PAUSED:
            await asyncio.to_thread(domain.resume)
        elif state == libvirt.VIR_DOMAIN_PMSUSPENDED:
            await asyncio.to_thread(domain.pMWakeup, 0)
        elif state == libvirt.VIR_DOMAIN_SHUTOFF:
            if upgrade:
                await asyncio.to_thread(self._ensure_memory_reclaim, domain)
            await asyncio.to_thread(domain.create)
        return True

    @workspace_mutation
    async def reconfigure_workspace(
        self,
        instance_id: str,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
        restart: bool,
    ) -> None:
        """Stop, update and restart one guest while other lifecycles continue."""
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        was_active = state != libvirt.VIR_DOMAIN_SHUTOFF
        await self.validate_reconfigure(
            instance_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )
        if was_active:
            await self.stop_workspace(instance_id)
        await self._reconfigure_workspace(
            instance_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
        )
        if (restart or was_active) and await self._start_workspace(
            instance_id, upgrade=False
        ):
            await self._wait_for_ssh(instance_id)

    @storage_mutation
    async def validate_reconfigure(
        self,
        instance_id: str,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> None:
        """Reject malformed values and disk shrinking before stopping the guest."""
        self._validate_workspace_resources(
            qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb
        )
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        root = ET.fromstring(await asyncio.to_thread(domain.XMLDesc, 0))
        source = root.find("./devices/disk[@device='disk']/source")
        if source is None or not source.get("file"):
            raise RuntimeError(f"Could not resolve disk path for VM {instance_id}")
        info = await self._image_info(Path(source.get("file")))
        current_size = int(info.get("virtual-size", 0))
        if current_size <= 0:
            raise RuntimeError("Could not verify existing QEMU virtual disk size")
        if qemu_disk_size_gb * 1024**3 < current_size:
            raise OperationRejectedError(
                DISK_SHRINK_UNSUPPORTED, "Reducing QEMU disk size is not supported"
            )

    @storage_mutation
    async def _reconfigure_workspace(
        self,
        instance_id: str,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
    ) -> None:
        """Apply resources only after the caller has verified guest shutdown."""
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        if state != libvirt.VIR_DOMAIN_SHUTOFF:
            raise RuntimeError("Resource changes require a shut off guest")

        # Read current disk path from domain XML.
        xml = await asyncio.to_thread(domain.XMLDesc, 0)
        root = ET.fromstring(xml)
        disk_source = root.find("./devices/disk[@device='disk']/source")
        if disk_source is None:
            raise RuntimeError(
                f"Could not locate disk source in VM XML for {instance_id}"
            )
        disk_path = disk_source.get("file")
        if not disk_path:
            raise RuntimeError(f"Could not resolve disk path for VM {instance_id}")

        # Never shrink existing virtual disks to avoid data loss.
        info_proc = await asyncio.create_subprocess_exec(
            "qemu-img",
            "info",
            "--output=json",
            disk_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        info_stdout, info_stderr = await info_proc.communicate()
        if info_proc.returncode != 0:
            raise RuntimeError(f"Failed to inspect disk image: {info_stderr.decode()}")
        import json

        disk_info = json.loads(info_stdout.decode() or "{}")
        current_virtual_size = int(disk_info.get("virtual-size", 0))
        requested_size_bytes = qemu_disk_size_gb * 1024 * 1024 * 1024
        if requested_size_bytes < current_virtual_size:
            raise RuntimeError("Reducing QEMU disk size is not supported")
        if requested_size_bytes > current_virtual_size:
            resize_proc = await asyncio.create_subprocess_exec(
                "qemu-img",
                "resize",
                disk_path,
                f"{qemu_disk_size_gb}G",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, resize_stderr = await resize_proc.communicate()
            if resize_proc.returncode != 0:
                raise RuntimeError(
                    f"Failed to resize disk image: {resize_stderr.decode()}"
                )

        memory_kib = qemu_memory_mb * 1024
        memory_node = root.find("./memory")
        if memory_node is None:
            memory_node = ET.SubElement(root, "memory")
        memory_node.set("unit", "KiB")
        memory_node.text = str(memory_kib)

        current_memory_node = root.find("./currentMemory")
        if current_memory_node is None:
            current_memory_node = ET.SubElement(root, "currentMemory")
        current_memory_node.set("unit", "KiB")
        current_memory_node.text = str(memory_kib)

        vcpu_node = root.find("./vcpu")
        if vcpu_node is None:
            vcpu_node = ET.SubElement(root, "vcpu")
        vcpu_node.set("placement", "static")
        vcpu_node.text = str(qemu_vcpus)
        # libvirt rejects a topology that no longer matches the vCPU count.
        topology = root.find("./cpu/topology")
        if topology is not None:
            topology.set("sockets", "1")
            topology.set("dies", "1")
            topology.set("threads", "1")
            topology.set("cores", str(qemu_vcpus))

        _enable_free_page_reporting(root)
        updated_xml = ET.tostring(root, encoding="unicode")
        await asyncio.to_thread(lambda: self._libvirt_conn().defineXML(updated_xml))

        logger.info(
            "vm_workspace_reconfigured",
            instance_id=instance_id,
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
            disk_size_gb=qemu_disk_size_gb,
        )

    @workspace_mutation
    @storage_mutation
    async def remove_workspace(self, instance_id: str) -> None:
        """Destroy and undefine the VM, its network, disk files and host key."""
        disk = self._disk_path(instance_id)
        await self._assert_workspace_resources_unconsumed(instance_id)

        await self._invalidate_ssh(instance_id)

        try:
            domain = await asyncio.to_thread(self._get_domain, instance_id)
        except RuntimeError:
            # Only exact absence on successful enumeration permits disk cleanup.
            domains = await asyncio.to_thread(
                lambda: self._libvirt_conn().listAllDomains(0)
            )
            if any(d.name() == self._domain_name(instance_id) for d in domains):
                raise
            domain = None
        try:
            if domain is not None:
                # Paused guests are active too. Destroying a transient domain
                # removes its definition, so only undefine persistent guests.
                persistent = await asyncio.to_thread(domain.isPersistent)
                if await asyncio.to_thread(domain.isActive):
                    await asyncio.to_thread(domain.destroy)
                if persistent:
                    await asyncio.to_thread(
                        domain.undefineFlags,
                        libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                        | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE,
                    )
            domains = await asyncio.to_thread(
                lambda: self._libvirt_conn().listAllDomains(0)
            )
            if any(d.name() == self._domain_name(instance_id) for d in domains):
                raise RuntimeError(
                    "Domain removal could not be verified; disks retained"
                )
        except libvirt.libvirtError as exc:
            raise RuntimeError("Domain removal failed; disks retained") from exc

        # ── Tear down the per-workspace network ───────────────────────
        # Must happen *after* the domain is destroyed so libvirt doesn't
        # refuse to remove the network while it still has active interfaces.
        await self._destroy_workspace_network(instance_id)

        # Clean up files only after the final dependency fence.
        await self._assert_workspace_resources_unconsumed(instance_id)

        def unlink_files() -> None:
            for path in self._workspace_owned_paths(instance_id):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            directory = os.open(self._disk_dir, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        await asyncio.to_thread(unlink_files)
        self._host_key_cache.pop(instance_id, None)
        self._guest_cache.pop(instance_id, None)

        logger.info("vm_workspace_removed", instance_id=instance_id)

    @workspace_mutation
    @storage_mutation
    async def workspace_removed(self, instance_id: str) -> bool:
        """Prove exact absence without inferring ownership of orphan resources."""
        def observe() -> bool:
            conn = self._libvirt_conn()
            domains = conn.listAllDomains(0)
            networks = conn.listAllNetworks(0)
            if any(d.name() == self._domain_name(instance_id) for d in domains):
                return False
            if any(n.name() == self._workspace_network_name(instance_id) for n in networks):
                return False
            # An unavailable storage root must not be mistaken for empty storage.
            self._disk_dir.stat()
            for path in self._workspace_owned_paths(instance_id):
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                return False
            return True

        return await asyncio.to_thread(observe)

    @workspace_mutation
    async def quiesce_interrupted_workspace(self, instance_id: str) -> str:
        """Stop an exact interrupted guest without claiming credential cleanup."""
        if str(uuid.UUID(instance_id)) != instance_id:
            raise ValueError("Recovery requires a canonical workspace UUID")

        def observe() -> tuple[tuple[str, int, int], int] | None:
            domains = [
                domain
                for domain in self._libvirt_conn().listAllDomains(0)
                if domain.name() == self._domain_name(instance_id)
            ]
            if not domains:
                return None
            if len(domains) != 1:
                raise RuntimeError("Interrupted workspace identity is ambiguous")
            domain = domains[0]
            root = ET.fromstring(domain.XMLDesc(0))
            namespace = "{https://opencuria.dev/libvirt}"
            owner = root.findtext(
                f"./metadata/{namespace}workspace/{namespace}workspace-id"
            )
            source = root.find("./devices/disk[@device='disk']/source")
            disk = self._disk_path(instance_id).absolute()
            if (
                owner != instance_id
                or source is None
                or source.get("file") != str(disk)
                or any(path.is_symlink() for path in (disk, *disk.parents))
            ):
                raise RuntimeError("Interrupted workspace ownership is unverified")
            stat = disk.stat()
            return (
                (domain.UUIDString(), stat.st_dev, stat.st_ino),
                domain.state()[0],
            )

        before = await asyncio.to_thread(observe)
        if before is None:
            if await self.workspace_removed(instance_id):
                return "absent"
            # Allocation can fail after publishing a disk/seed/network but before
            # defining a guest. Complete dependency observation proves that no
            # guest or backing chain can still mutate these preserved resources.
            # This is quiescence, never absence or credential-clean evidence.
            await self._assert_workspace_resources_unconsumed(instance_id)
            if await asyncio.to_thread(observe) is not None:
                raise RuntimeError("Interrupted workspace appeared during inspection")
            return "partial"
        await self.stop_workspace(instance_id)
        after = await asyncio.to_thread(observe)
        if (
            after is None
            or after[0] != before[0]
            or after[1] != libvirt.VIR_DOMAIN_SHUTOFF
        ):
            raise RuntimeError("Interrupted workspace shutdown could not be verified")
        return "stopped"

    async def workspace_exists(self, instance_id: str) -> bool:
        """Check if a VM domain exists."""
        try:
            await asyncio.to_thread(self._get_domain, instance_id)
            return True
        except RuntimeError as exc:
            cause = exc.__cause__
            if (isinstance(cause, libvirt.libvirtError)
                    and cause.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN):
                return False
            raise

    async def get_workspace_status(self, instance_id: str) -> RuntimeStatus:
        """Map libvirt domain state to a RuntimeStatus."""
        domain_name = self._domain_name(instance_id)
        try:
            domain = await asyncio.to_thread(self._get_domain, instance_id)
            state, _ = await asyncio.to_thread(domain.state)
        except RuntimeError as exc:
            cause = exc.__cause__
            if (not isinstance(cause, libvirt.libvirtError)
                    or cause.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN):
                raise
            return RuntimeStatus(
                instance_id=instance_id, status="removed", name=domain_name
            )

        return RuntimeStatus(
            instance_id=instance_id,
            status=_workspace_status(state),
            name=domain_name,
        )

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Execute a command and wait for completion.

        Cancellation of the awaiting caller does not kill the remote SSH
        command — ``ssh.run`` exposes no process handle, so the command runs
        to completion in the VM. Cancelling only stops waiting for the result.
        """
        ssh = await self._get_ssh(instance_id)
        cmd_str = self._build_shell_command(command, workdir, env)

        result = await ssh.run(cmd_str, check=False)
        exit_code = self._require_exit_status(result)
        output = (result.stdout or "") + (result.stderr or "")
        return exit_code, output

    async def exec_with_input(
        self,
        instance_id: str,
        command: list[str],
        data: bytes,
        workdir: str | None = None,
    ) -> tuple[int, str, str]:
        """Run a command with ``data`` on stdin; return exit code, stdout, stderr.

        For secrets that must not appear in a command line or a guest file.
        """
        ssh = await self._get_ssh(instance_id)
        process = await ssh.create_process(
            self._build_shell_command(command, workdir), encoding=None
        )
        try:
            process.stdin.write(data)
            process.stdin.write_eof()
            stdout, stderr = await asyncio.gather(
                process.stdout.read(), process.stderr.read()
            )
            await process.wait_closed()
        finally:
            process.close()
        code = self._require_exit_status(process)
        return (
            code,
            bytes(stdout).decode("utf-8", "replace"),
            bytes(stderr).decode("utf-8", "replace"),
        )

    @staticmethod
    def _require_exit_status(
        process: asyncssh.SSHClientProcess | asyncssh.SSHCompletedProcess,
    ) -> int:
        """Require exit evidence where the execution contract promises an int."""
        try:
            code = process.exit_status
        except (OSError, asyncssh.Error):
            code = None
        if code is None:
            raise RuntimeError("QEMU command exit status is unknown")
        return int(code)

    def _build_shell_command(
        self,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Build a shell command string with optional workdir and env."""
        parts: list[str] = ['export PATH="$HOME/.local/bin:$PATH"']
        if env:
            for k, v in env.items():
                # Escape single quotes in value
                escaped_v = str(v).replace("'", "'\\''")
                parts.append(f"export {k}='{escaped_v}'")
        if workdir:
            parts.append(f"cd '{workdir}'")

        # Join the actual command — each arg is single-quoted with internal
        # single quotes escaped via the '"'"' technique so that arbitrary
        # prompt text (including newlines and quotes) is passed verbatim.
        def _sq(s: str) -> str:
            return "'" + s.replace("'", "'\\''") + "'"

        cmd_str = " ".join(_sq(c) for c in command)
        parts.append(cmd_str)
        return " && ".join(parts)

    async def exec_pty(
        self,
        instance_id: str,
        cols: int = 80,
        rows: int = 24,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
    ) -> PtyHandle:
        """Open an interactive PTY session via SSH."""
        ssh = await self._get_ssh(instance_id)
        cmd_str = self._build_shell_command(
            command or ["/bin/bash", "-l"],
            workdir=workdir or "/workspace",
            env=env,
        )
        process = await ssh.create_process(
            cmd_str,
            term_type="xterm-256color",
            term_size=(cols, rows),
            encoding=None,  # binary mode
        )

        return PtyHandle(
            instance_id=instance_id,
            handle=process,
            metadata={"cols": cols, "rows": rows},
        )

    def is_transport_failure(self, error: BaseException) -> bool:
        """Refused SSH channels and lost connections are transport failures."""
        seen: set[int] = set()
        current: BaseException | None = error
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(
                current,
                (asyncssh.ChannelOpenError, asyncssh.DisconnectError, ConnectionError),
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    async def pty_read(self, handle: PtyHandle, size: int = 4096) -> bytes:
        """Read raw bytes from the PTY. Returns b'' on EOF."""
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        try:
            if process.stdout.at_eof():
                return b""
            chunk = await process.stdout.read(size)
            return chunk if chunk else b""
        except (asyncssh.Error, OSError):
            return b""

    async def pty_write(self, handle: PtyHandle, data: bytes) -> None:
        """Send data to the PTY's stdin."""
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        process.stdin.write(data)

    async def pty_resize(self, handle: PtyHandle, cols: int, rows: int) -> None:
        """Resize the PTY terminal."""
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        process.change_terminal_size(cols, rows)
        handle.metadata["cols"] = cols
        handle.metadata["rows"] = rows

    async def pty_close(self, handle: PtyHandle) -> None:
        """Close the PTY SSH session and release its guest session slot."""
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        handle.closed = True
        # A half-closed channel rejects EOF; it must still be closed.
        with contextlib.suppress(Exception):
            process.stdin.write_eof()
        try:
            process.close()
            await process.wait_closed()
        except Exception:
            pass

    # -- Generic non-TTY bidirectional process streams ---------------------

    def _stream_pidfile(self) -> str:
        """Return a fresh pidfile path for one stream process."""
        return f"/tmp/opencuria-stream-{uuid.uuid4().hex}.pid"

    def _build_stream_kill_command(self, pidfile: str) -> str:
        """Build a static kill command for one stream pidfile (no interpolation).

        Reads the session pid from the runner-generated *pidfile* and
        delivers TERM to the process group, waits briefly, then KILLs
        the group.  The only variable part is the pidfile path, safely
        single-quoted.
        """
        quoted = "'" + pidfile.replace("'", "'\\''") + "'"
        return (
            f"pidfile={quoted}; "
            'pid=""; '
            'if [ -f "$pidfile" ]; then '
            'pid=$(cat "$pidfile" 2>/dev/null); fi; '
            "case \"$pid\" in ''|*[!0-9]*) exit 0;; esac; "
            'kill -TERM -"$pid" 2>/dev/null || '
            'kill -TERM "$pid" 2>/dev/null || true; '
            "for _ in 1 2 3 4 5 6 7 8 9 10; do "
            'kill -0 "$pid" 2>/dev/null || break; '
            "sleep 0.2; "
            "done; "
            'kill -KILL -"$pid" 2>/dev/null || '
            'kill -KILL "$pid" 2>/dev/null || true; '
            'rm -f "$pidfile"'
        )

    async def _kill_stream_tree(self, instance_id: str, pidfile: str) -> None:
        """Best-effort TERM/KILL of one stream process tree (separate SSH call)."""
        try:
            ssh = await self._get_ssh(instance_id)
            kill_cmd = self._build_stream_kill_command(pidfile)
            proc = await ssh.create_process(kill_cmd)
            await proc.wait_closed()
        except Exception:
            logger.warning("qemu_stream_kill_failed")

    @property
    def supports_managed_process(self) -> bool:
        """Support durable guest stream supervision and fenced recovery."""
        return True

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
        """Spawn a non-TTY bidirectional process (stdin/stdout/stderr split)."""
        if not command or not all(isinstance(part, str) for part in command):
            raise ValueError("command must be a non-empty argv list of str")
        if any("\x00" in part for part in command):
            raise ValueError("command must not contain NUL bytes")
        ssh = await self._get_ssh(instance_id)
        if control_path and expected_token is None:
            expected_token = await self.probe_managed_token(instance_id)
        pidfile = self._stream_pidfile()
        if process_profile is not None:
            if not control_path or expected_token is None or isolated_home is None:
                raise ValueError("Isolated agent streams require managed identity")
            argv = managed_isolated_argv(
                control_path,
                workdir,
                list(command),
                expected_token=expected_token,
                isolated_home=isolated_home,
                env=env,
                process_profile=process_profile,
            )
        elif control_path:
            argv = managed_argv(
                control_path,
                workdir,
                env,
                list(command),
                expected_token=expected_token,
            )
        else:
            argv = stream_wrapper_argv(pidfile, workdir, env, list(command))
        # Same quoting discipline as _build_shell_command: every argv
        # word is single-quoted so user values stay opaque to the shell.
        cmd_str = shell_quote_argv(argv)
        process = await ssh.create_process(
            cmd_str,
            stdin=asyncssh.PIPE,
            stdout=asyncssh.PIPE,
            stderr=asyncssh.PIPE,
            encoding=None,  # binary mode: bytes on all three streams
        )
        if process_profile is not None:
            try:
                process.stdin.write(
                    isolated_agent_env_preamble(env, process_profile=process_profile)
                )
                await process.stdin.drain()
            except Exception:
                process.close()
                raise
        handle = ProcessHandle(instance_id=instance_id, handle=process)
        handle.metadata.update(
            {
                "pidfile": pidfile,
                "control_path": control_path,
                "expected_token": expected_token,
                "pending_stdout": b"",
                "pending_stderr": b"",
            }
        )
        logger.info(
            "qemu_stream_spawned",
            instance_id=instance_id,
            pidfile=pidfile,
            # argv[0] only: full args may embed flags/paths that echo
            # secret-adjacent material; the wrapper pidfile correlates
            # with _kill_stream_tree diagnostics on failure.
            command=next(iter(command), ""),
            workdir=workdir or "",
        )
        return handle

    async def _stream_read_split(
        self, handle: ProcessHandle, stream: str, size: int
    ) -> bytes:
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"unknown stream: {stream!r}")
        size = max(1, min(int(size), self.STREAM_CHUNK_SIZE))
        pending_key = f"pending_{stream}"
        pending: bytes = handle.metadata.get(pending_key, b"")
        if pending:
            out = pending[:size]
            handle.metadata[pending_key] = pending[size:]
            return out
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        reader = process.stdout if stream == "stdout" else process.stderr
        try:
            if reader.at_eof():
                return b""
            chunk = await reader.read(size)
        except (asyncssh.Error, OSError):
            return b""
        if not chunk:
            return b""
        data = bytes(chunk)
        handle.metadata[pending_key] = data[size:]
        return data[:size]

    async def process_read(
        self,
        handle: ProcessHandle,
        stream: str = "stdout",
        size: int = 65536,
    ) -> bytes:
        """Read raw bytes from one split stream (``b""`` on EOF)."""
        if handle.closed:
            return b""
        return await self._stream_read_split(handle, stream, size)

    async def process_write(self, handle: ProcessHandle, data: bytes) -> None:
        """Write raw bytes to the process stdin."""
        if handle.closed or not data:
            return
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        try:
            process.stdin.write(bytes(data))
        except (asyncssh.Error, OSError, ValueError) as exc:
            raise RuntimeError(f"stream write failed: {exc}") from exc

    async def process_write_eof(self, handle: ProcessHandle) -> None:
        """Half-close the process stdin (graceful EOF)."""
        if handle.closed:
            return
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        try:
            process.stdin.write_eof()
        except (asyncssh.Error, OSError):
            pass

    async def process_wait(self, handle: ProcessHandle) -> int | None:
        """Wait for the remote process tree to exit; return exit code.

        Returns ``None`` when the handle was already closed or the exit
        status is unknown — never a bogus exit 0 for a closed handle.
        """
        if handle.closed:
            return None
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        try:
            await process.wait_closed()
        except (asyncssh.Error, OSError):
            pass
        try:
            code = process.exit_status
        except (asyncssh.Error, OSError):
            code = None
        return int(code) if code is not None else None

    async def process_detach(self, handle: ProcessHandle) -> None:
        """Close only the existing SSH channel, never exec a replacement guest."""
        handle.handle.close()
        with contextlib.suppress(asyncio.TimeoutError, asyncssh.Error, OSError):
            await asyncio.wait_for(handle.handle.wait_closed(), 5)
        handle.closed = True

    async def process_close(self, handle: ProcessHandle) -> bool | None:
        """Graceful stdin EOF, TERM/KILL the tree, close handles.

        Robust close order for the single-SSH-channel transport: the
        process-group kill runs on a *separate* SSH channel
        (``_kill_stream_tree`` uses the shared transport) so it never deadlocks
        against the closing session channel.  ``wait_closed``
        is only awaited once *before* the kill with a short timeout and
        never again after ``close()`` (which already implies closure) —
        the old double-wait could hang when the channel was already
        torn down by the kill.
        """
        control_path = handle.metadata.get("control_path")
        if control_path:
            await self.close_managed_process(handle.instance_id, control_path)
        if handle.closed:
            return True if control_path else None
        handle.closed = True
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        pidfile = str(handle.metadata.get("pidfile", ""))
        exit_status: int | None = None
        try:
            exit_status = process.exit_status
        except (asyncssh.Error, OSError):
            exit_status = None
        # stdin EOF first while the handle is still usable.
        with contextlib.suppress(Exception):
            await self.process_write_eof(handle)
        # NOTE: process_write_eof early-returns on handle.closed, so the
        # raw write_eof is attempted directly here as well.
        try:
            process.stdin.write_eof()
        except (asyncssh.Error, OSError):
            pass
        try:
            await asyncio.wait_for(process.wait_closed(), timeout=5)
            exited_early = True
        except (asyncio.TimeoutError, asyncssh.Error, OSError):
            exited_early = False
        if pidfile and not control_path:
            await self._kill_stream_tree(handle.instance_id, pidfile)
        try:
            process.close()
        except (asyncssh.Error, OSError):
            pass
        if not exited_early:
            # Single bounded grace wait after close(); never a second
            # unbounded wait — close() already tore the channel down.
            try:
                await asyncio.wait_for(process.wait_closed(), timeout=5)
            except (asyncio.TimeoutError, asyncssh.Error, OSError):
                pass
        try:
            final_status = process.exit_status
        except (asyncssh.Error, OSError):
            final_status = None
        logger.info(
            "qemu_stream_closed",
            pidfile=pidfile,
            exited_early=exited_early,
            exit_status_at_close=exit_status,
            exit_status_final=final_status,
        )
        return True if control_path else None

    async def list_workspaces(self) -> list[RuntimeWorkspaceInfo]:
        """Discover all opencuria VM workspaces."""
        result: list[RuntimeWorkspaceInfo] = []

        def _list() -> list[tuple[str, int, str]]:
            # Never publish a partial list as authoritative absence. A domain
            # disappearing during inspection is retried by the next full scan.
            listed = []
            for domain in self._libvirt_conn().listAllDomains(0):
                name = domain.name()
                if not name.startswith("opencuria-workspace-"):
                    continue
                instance_id = name.replace("opencuria-workspace-", "")
                guest_os = (
                    self._guest_cache.get(instance_id)
                    or self._read_guest_name(instance_id)
                    or LINUX
                )
                listed.append((name, domain.state()[0], guest_os))
            return listed

        domains = await asyncio.to_thread(_list)

        for name, state, guest_os in domains:
            instance_id = name.replace("opencuria-workspace-", "")
            result.append(
                RuntimeWorkspaceInfo(
                    workspace_id=instance_id,
                    instance_id=instance_id,
                    status=_workspace_status(state),
                    name=name,
                    guest_os=guest_os,
                )
            )

        return result

    async def get_workspace_usage(self, instance_id: str) -> dict[str, int] | None:
        """Return VM usage stats for a workspace.

        Values are returned in bytes and nanoseconds so callers can derive
        percentages over time. ``disk_*`` is the allocated host space used by
        the QCOW2 overlay. Running VMs additionally report the guest
        filesystem usage of ``/workspace`` as ``guest_disk_*`` when the guest
        answers in time.
        """

        def _collect() -> tuple[dict[str, int], bool]:
            disk_stat = self._disk_path(instance_id).stat()
            disk_used_bytes = disk_stat.st_blocks * 512

            domain = self._get_domain(instance_id)
            state, _ = domain.state()
            if state != libvirt.VIR_DOMAIN_RUNNING:
                return {
                    "cpu_time_ns": 0,
                    "vcpu_count": 0,
                    "ram_used_bytes": 0,
                    "ram_total_bytes": 0,
                    "disk_used_bytes": disk_used_bytes,
                    "disk_total_bytes": 0,
                }, False

            # Domain info: (state, maxMemKiB, memoryKiB, nrVirtCpu, cpuTimeNs)
            info = domain.info()
            vcpu_count = max(int(info[3]), 1)
            cpu_time_ns = int(info[4])

            memory_stats = domain.memoryStats() or {}
            total_memory_kib = int(memory_stats.get("actual", info[1] or 0))
            rss_memory_kib = int(memory_stats.get("rss", 0))
            unused_memory_kib = int(memory_stats.get("unused", 0))
            if total_memory_kib > 0 and unused_memory_kib > 0:
                used_memory_kib = max(total_memory_kib - unused_memory_kib, 0)
            else:
                used_memory_kib = rss_memory_kib

            disk_capacity_bytes = 0
            try:
                disk_capacity_bytes, _, _ = domain.blockInfo("vda", 0)
            except libvirt.libvirtError:
                # Fallback for unusual bus names: read the first VM disk target.
                xml = domain.XMLDesc(0)
                root = ET.fromstring(xml)
                disk_target = root.find("./devices/disk[@device='disk']/target")
                target_dev = disk_target.get("dev") if disk_target is not None else None
                if target_dev:
                    disk_capacity_bytes, _, _ = domain.blockInfo(target_dev, 0)

            return {
                "cpu_time_ns": cpu_time_ns,
                "vcpu_count": vcpu_count,
                "ram_used_bytes": max(used_memory_kib, 0) * 1024,
                "ram_total_bytes": max(total_memory_kib, 0) * 1024,
                "disk_used_bytes": disk_used_bytes,
                "disk_total_bytes": max(int(disk_capacity_bytes), 0),
            }, True

        try:
            usage, running = await asyncio.to_thread(_collect)
        except (libvirt.libvirtError, OSError):
            return None
        if running:
            usage.update(await self._guest_disk_usage(instance_id))
        return usage

    async def _guest_disk_usage(self, instance_id: str) -> dict[str, int]:
        """Read the guest ``/workspace`` filesystem usage via ``df``.

        Best effort: a booting, unreachable or slow guest yields no values
        instead of delaying the metrics report.
        """
        try:
            exit_code, output = await asyncio.wait_for(
                self.exec_command_wait(instance_id, ["df", "-Pk", _GUEST_DISK_PATH]),
                timeout=_GUEST_DISK_USAGE_TIMEOUT,
            )
        except (RuntimeError, OSError, asyncssh.Error, asyncio.TimeoutError):
            return {}
        if exit_code != 0:
            return {}
        return _parse_df_usage(output)

    async def put_archive(self, instance_id: str, path: str, data: bytes) -> None:
        """Upload a tar archive to the VM and extract it at the given path."""
        ssh = await self._get_ssh(instance_id)
        quoted_path = shlex.quote(path)
        # Ensure target directory exists
        await ssh.run(f"mkdir -p {quoted_path}", check=True)
        # Upload and extract via stdin pipe
        # encoding=None is required to write raw bytes (binary mode)
        process = await ssh.create_process(
            f"tar xf - -C {quoted_path}",
            stdin=asyncssh.PIPE,
            encoding=None,
        )
        process.stdin.write(data)
        process.stdin.write_eof()
        await process.wait()

    def _require_windows_host(self) -> None:
        """Refuse Windows guests on hosts without UEFI Secure Boot or a TPM."""
        caps = ET.fromstring(
            self._libvirt_conn().getDomainCapabilities(None, "x86_64", "q35", "kvm", 0)
        )
        def values(path: str) -> set[str]:
            return {node.text or "" for node in caps.findall(path)}

        if not (
            "efi" in values("./os/enum[@name='firmware']/value")
            and "yes" in values("./os/loader/enum[@name='secure']/value")
            and "emulator" in values("./devices/tpm/enum[@name='backendModel']/value")
        ):
            raise OperationRejectedError(
                WINDOWS_HOST_UNSUPPORTED,
                "Windows guests need OVMF Secure Boot firmware and swtpm on the host",
            )

    async def _ensure_windows_base(
        self, base_distro: str, *, operation_id: str, progress_callback=None
    ) -> Path:
        """Return the cached installed base of an edition, installing it once."""
        media = self._windows
        edition = windows_edition(base_distro)
        await asyncio.to_thread(self._require_windows_host)
        iso, iso_sha256 = await asyncio.to_thread(media.resolve_iso)
        base = media.base_path(edition, iso_sha256)
        if await asyncio.to_thread(media.base_ready, base):
            return base
        if progress_callback is not None:
            await progress_callback(
                f"Installing the Windows 11 {edition} base system; this takes a while"
            )
        virtio = await asyncio.to_thread(media.ensure_pinned, VIRTIO_WIN)
        instance_id = f"{self._build_instance_id(operation_id)}-base"
        temp, setup_iso = self._windows_install_files(operation_id, base)
        try:
            await asyncio.to_thread(media.build_setup_iso, edition, setup_iso)
            await self._allocate_windows_install(instance_id, temp, iso, virtio, setup_iso)
            await self._await_windows_install(instance_id, progress_callback)
            await self._cleanup_build_instance(instance_id)
            await asyncio.to_thread(os.replace, temp, base)
            await asyncio.to_thread(
                media.record_base, base, {"edition": edition, "iso_sha256": iso_sha256}
            )
            logger.info("windows_base_installed", edition=edition, path=str(base))
            return base
        finally:
            await self._cleanup_build_instance(instance_id)
            await asyncio.to_thread(temp.unlink, missing_ok=True)
            await asyncio.to_thread(setup_iso.unlink, missing_ok=True)

    async def _configure_windows_build(self, instance_id: str, progress_callback) -> None:
        """Apply the runner's current guest configuration and scripts to a build.

        Published images therefore carry this runner's bootstrap, generalize
        and shell settings, whatever base installation they derive from.
        """
        code, stdout, stderr = await self.exec_with_input(
            instance_id, ["bash", "-s"], windows_configure_script()
        )
        if progress_callback is not None:
            for line in (stdout + stderr).splitlines():
                if line.strip():
                    await progress_callback(line.rstrip())
        if code != 0:
            raise OperationError(
                BUILD_SCRIPT_FAILED, f"Windows guest configuration failed ({code})"
            )
        await self.put_archive(instance_id, WINDOWS_STATE_DIR, guest_scripts_archive())
        # New SSH sessions pick up the changed machine environment (BASH_ENV).
        await self._invalidate_ssh(instance_id)
        await self._wait_for_ssh(instance_id)

    def _windows_install_files(self, operation_id: str, base: Path) -> tuple[Path, Path]:
        """Return the operation-owned temporary system disk and setup media."""
        identity = uuid.UUID(operation_id).hex
        instance_id = f"{self._build_instance_id(operation_id)}-base"
        return (
            base.with_name(base.name + f".tmp-{identity}"),
            self._disk_dir / f"{instance_id}-setup.iso",
        )

    @storage_mutation
    async def _allocate_windows_install(
        self, instance_id: str, disk: Path, iso: Path, virtio: Path, setup_iso: Path
    ) -> None:
        """Define a guest that boots the installer onto an empty system disk."""
        guest = guest_for("windows")
        proc = await asyncio.create_subprocess_exec(
            "qemu-img", "create", "-q", "-f", "qcow2", str(disk), f"{BASE_DISK_GB}G",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create the Windows system disk: {stderr.decode()}")
        await asyncio.to_thread(disk.chmod, 0o644)
        gateway, vm_ip = await self._create_workspace_network(instance_id, dhcp=True)
        seed = await self._create_cloud_init_iso(
            instance_id, vm_ip=vm_ip, gateway=gateway, guest=guest
        )
        root = ET.fromstring(
            _domain_xml(
                name=self._domain_name(instance_id),
                vcpus=max(self._settings.qemu_build_vcpus, guest.min_vcpus),
                memory_mb=max(self._settings.qemu_build_memory_mb, guest.min_memory_mb),
                disk_path=str(disk),
                cloud_init_iso=str(seed),
                network=self._workspace_network_name(instance_id),
                guest=guest,
                serial_log=str(self._serial_log_path(instance_id)),
            )
        )
        # Boot the installer once; afterwards the installed disk boots first.
        os_node = root.find("./os")
        for boot in os_node.findall("./boot"):
            os_node.remove(boot)
        devices = root.find("./devices")
        system = devices.find("./disk[@device='disk']")
        ET.SubElement(system, "boot", order="1")
        for target, source, order in (
            ("sdb", iso, "2"), ("sdc", virtio, None), ("sdd", setup_iso, None)
        ):
            cdrom = ET.SubElement(devices, "disk", type="file", device="cdrom")
            ET.SubElement(cdrom, "driver", name="qemu", type="raw")
            ET.SubElement(cdrom, "source", file=str(source))
            ET.SubElement(cdrom, "target", dev=target, bus="sata")
            ET.SubElement(cdrom, "readonly")
            if order:
                ET.SubElement(cdrom, "boot", order=order)
        xml = ET.tostring(root, encoding="unicode")

        def define_and_start() -> None:
            domain = self._libvirt_conn().defineXML(xml)
            domain.createWithFlags(libvirt.VIR_DOMAIN_START_RESET_NVRAM)

        self._guest_cache[instance_id] = guest.name
        await asyncio.to_thread(define_and_start)

    async def _await_windows_install(self, instance_id: str, progress_callback) -> None:
        """Pass the installer's boot prompt, then await the unattended poweroff."""
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        loop = asyncio.get_running_loop()
        # UEFI asks to press a key before booting from the installation DVD.
        prompt_deadline = loop.time() + 45
        while loop.time() < prompt_deadline:
            with contextlib.suppress(libvirt.libvirtError):
                await asyncio.to_thread(
                    domain.sendKey, libvirt.VIR_KEYCODE_SET_LINUX, 0, [57], 1, 0
                )
            await asyncio.sleep(1)
        timeout = getattr(self._settings, "qemu_windows_install_timeout", 14400)
        deadline = loop.time() + timeout
        serial = self._serial_log_path(instance_id)
        reported = 0
        started = loop.time()
        next_heartbeat = started + 300
        while (await asyncio.to_thread(domain.state))[0] != libvirt.VIR_DOMAIN_SHUTOFF:
            if loop.time() > deadline:
                with contextlib.suppress(libvirt.libvirtError):
                    await asyncio.to_thread(domain.destroy)
                raise OperationError(
                    BUILD_SCRIPT_TIMEOUT,
                    f"Windows installation exceeded its {timeout:g}s limit",
                )
            lines = await asyncio.to_thread(self._serial_lines, serial, "opencuria-setup: ")
            if progress_callback is not None:
                for line in lines[reported:]:
                    await progress_callback(line)
                if loop.time() >= next_heartbeat:
                    minutes = int((loop.time() - started) // 60)
                    await progress_callback(f"Windows installation running ({minutes} min)")
                    next_heartbeat += 300
            reported = len(lines)
            await asyncio.sleep(10)
        log = await asyncio.to_thread(self._serial_lines, serial, "")
        if not any("OPENCURIA_SETUP_OK" in line for line in log):
            failure = next(
                (line for line in log if "OPENCURIA_SETUP_FAILED" in line),
                "no result reported",
            )
            raise OperationError(
                BUILD_SCRIPT_FAILED, f"Windows base installation failed: {failure[-300:]}"
            )

    @staticmethod
    def _serial_lines(path: Path, prefix: str) -> list[str]:
        """Return the printable COM1 lines that start with ``prefix``."""
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except FileNotFoundError:
            return []
        lines = []
        for raw in text.replace("\r", "\n").split("\n"):
            line = "".join(ch for ch in raw if ch.isprintable()).strip()
            if prefix in line:
                lines.append(line[line.index(prefix) + len(prefix):] if prefix else line)
        return lines

    @staticmethod
    def _build_instance_id(operation_id: str) -> str:
        """Map one durable operation to exactly one disposable build guest."""
        try:
            identity = uuid.UUID(operation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Build requires a valid operation UUID") from exc
        return f"image-build-{identity}"

    async def recover_interrupted_build(self, operation_id: str) -> None:
        """Remove only the interrupted operation's exact temporary guest."""
        instance_id = self._build_instance_id(operation_id)
        for guest_id in (instance_id, f"{instance_id}-base"):
            await self.remove_workspace(guest_id)
            if not await self.workspace_removed(guest_id):
                raise RuntimeError("Interrupted image build resources remain present")
        identity = uuid.UUID(operation_id).hex

        def remove_install_files() -> None:
            (self._disk_dir / f"{instance_id}-base-setup.iso").unlink(missing_ok=True)
            cache = self._windows.cache_dir
            if cache.is_dir():
                for path in cache.glob(f"windows-11-*.qcow2.tmp-{identity}"):
                    path.unlink(missing_ok=True)

        await asyncio.to_thread(remove_install_files)

    @storage_mutation
    async def _prepare_build_instance(
        self, instance_id: str, base_image: Path, guest: GuestOS | None = None
    ) -> Path:
        """Allocate subnet, disk and domain atomically against storage scans."""
        guest = guest or guest_for(LINUX)
        disk_size_gb = self._settings.qemu_disk_size_gb
        vcpus = getattr(self._settings, "qemu_build_vcpus", self._settings.qemu_vcpus)
        memory_mb = getattr(
            self._settings, "qemu_build_memory_mb", self._settings.qemu_memory_mb
        )
        if guest.name != LINUX:
            info = await self._image_info(base_image)
            base_gib = (int(info["virtual-size"]) + 1024**3 - 1) // 1024**3
            disk_size_gb = max(disk_size_gb, base_gib, guest.min_disk_gb)
            vcpus = max(vcpus, guest.min_vcpus)
            memory_mb = max(memory_mb, guest.min_memory_mb)
        disk = await self._create_overlay_disk(
            instance_id,
            disk_size_gb=disk_size_gb,
            base_image=str(base_image),
        )
        await self._define_and_start_guest(
            instance_id, guest=guest, disk=disk, vcpus=vcpus, memory_mb=memory_mb
        )
        return disk

    @workspace_mutation
    async def _finalize_build_image(
        self,
        instance_id: str,
        disk: Path,
        target: Path,
        manifest: dict,
        guest: GuestOS | None = None,
    ) -> dict:
        """Stop (and generalize) before taking the graph lock for publication."""
        guest = guest or guest_for(LINUX)
        await self.stop_workspace(instance_id)
        if guest.generalize_on_publish:
            await self._generalize_stopped_guest(instance_id)
        if guest.name != LINUX:
            manifest = {**manifest, "guest_os": guest.name}
        return await self._publish_image(disk, target, manifest)

    async def _generalize_stopped_guest(self, instance_id: str) -> None:
        """Boot a stopped guest once with a ``generalize`` seed and await poweroff.

        The guest removes its per-instance state, generalizes and powers off;
        its COM1 log carries the verdict. Only disposable guests are
        generalized: build guests in place, capture copies on their overlay.
        """
        guest = await self._resolve_guest(instance_id)
        await self._create_cloud_init_iso(instance_id, guest=guest, action="generalize")
        await asyncio.to_thread(self._serial_log_path(instance_id).unlink, missing_ok=True)
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        await asyncio.to_thread(domain.create)
        await self._await_generalized(instance_id)

    async def _await_generalized(self, instance_id: str) -> None:
        """Wait for a generalizing guest to power off and verify its result."""
        timeout = getattr(self._settings, "qemu_generalize_timeout", 3600)
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (await asyncio.to_thread(domain.state))[0] != libvirt.VIR_DOMAIN_SHUTOFF:
            if loop.time() > deadline:
                with contextlib.suppress(libvirt.libvirtError):
                    await asyncio.to_thread(domain.destroy)
                raise OperationError(
                    GENERALIZE_FAILED,
                    f"Guest did not finish generalizing within {timeout:g}s",
                )
            await asyncio.sleep(5)

        def verdict() -> str:
            try:
                text = self._serial_log_path(instance_id).read_text(errors="replace")
            except FileNotFoundError:
                return ""
            return text

        log = await asyncio.to_thread(verdict)
        if "OPENCURIA_GENERALIZE_OK" in log:
            logger.info("vm_guest_generalized", instance_id=instance_id)
            return
        reason = "no result reported"
        for line in log.splitlines():
            if "OPENCURIA_GENERALIZE_FAILED" in line:
                reason = line.split("OPENCURIA_GENERALIZE_FAILED", 1)[1].strip()[:300]
        logger.warning("vm_guest_generalize_failed", instance_id=instance_id, reason=reason)
        raise OperationError(GENERALIZE_FAILED, f"Guest generalization failed: {reason}")

    async def build_image(
        self,
        *,
        base_distro: str,
        init_script: str,
        image_path: str,
        operation_id: str | None = None,
        image_instance_id: str | None = None,
        progress_callback=None,
    ) -> dict[str, str | int]:
        """Queue builds for a configured build slot before issuing physical effects."""
        self._build_instance_id(operation_id)
        if not image_instance_id:
            raise RuntimeError("Build requires operation and image identity")
        self._managed_image_path(image_path)
        if not hasattr(self, "_build_semaphore"):
            self._build_semaphore = asyncio.Semaphore(
                getattr(self._settings, "qemu_build_concurrency", 2)
            )
        timeout = getattr(self._settings, "qemu_build_queue_timeout", 1800)
        if self._build_semaphore.locked() and progress_callback is not None:
            await progress_callback("Waiting for an image build slot")
        try:
            await asyncio.wait_for(self._build_semaphore.acquire(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise OperationRejectedError(
                BUILD_QUEUE_TIMEOUT, "Image build queue timed out waiting for a slot"
            ) from exc
        try:
            return await self._build_image(
                base_distro=base_distro,
                init_script=init_script,
                image_path=image_path,
                operation_id=operation_id,
                image_instance_id=image_instance_id,
                progress_callback=progress_callback,
            )
        finally:
            self._build_semaphore.release()

    @drain_cancellation
    async def _build_image(
        self,
        *,
        base_distro: str,
        init_script: str,
        image_path: str,
        operation_id: str | None,
        image_instance_id: str | None,
        progress_callback,
    ) -> dict[str, str | int]:
        """Provision and publish a temporary guest in an acquired build slot."""
        target_path = self._managed_image_path(image_path)
        if not operation_id or not image_instance_id:
            raise RuntimeError("Build requires operation and image identity")
        # Downloads and qemu-img validation must not block heartbeat delivery.
        # Serialize cache publication per distro, not the guest provisioning.
        if not hasattr(self, "_base_image_locks"):
            self._base_image_locks: dict[str, asyncio.Lock] = {}
        distro, separator, version = base_distro.strip().lower().partition(":")
        cache_key = f"{distro.strip()}:{version.strip()}" if separator else distro
        cache_lock = self._base_image_locks.setdefault(cache_key, asyncio.Lock())
        guest = guest_for_distro(base_distro)
        if guest.name != LINUX:
            # One installation at a time: it is long and shares the ISO.
            cache_lock = self._base_image_locks.setdefault("windows", asyncio.Lock())
        async with cache_lock:
            if guest.name == LINUX:
                base_image = await asyncio.to_thread(
                    self._resolve_build_base_image, base_distro
                )
            else:
                base_image = await self._ensure_windows_base(
                    base_distro,
                    operation_id=operation_id,
                    progress_callback=progress_callback,
                )
        instance_id = self._build_instance_id(operation_id)
        await asyncio.to_thread(
            self._ensure_host_directory,
            target_path.parent,
            writable_hint=(
                f"sudo install -d -o $USER -g $USER -m 755 '{target_path.parent}'"
            ),
        )

        if progress_callback is not None:
            await progress_callback(f"Using base image {base_image.name}")

        try:
            if guest.name == LINUX:
                disk = await self._prepare_build_instance(instance_id, base_image)
            else:
                disk = await self._prepare_build_instance(instance_id, base_image, guest)
            await self._wait_for_ssh(instance_id)
            await self._wait_for_cloud_init(instance_id)

            if progress_callback is not None:
                await progress_callback("Temporary build VM is ready")
            if guest.name != LINUX:
                await self._configure_windows_build(instance_id, progress_callback)

            build = guest.build_script()
            script_body = init_script.strip() or (
                "#!/bin/bash\nset -euo pipefail\n"
                if guest.name == LINUX
                else "$ErrorActionPreference = 'Stop'\n"
            )
            archive = io.BytesIO()
            script_bytes = script_body.encode("utf-8")
            with tarfile.open(fileobj=archive, mode="w") as tar:
                info = tarfile.TarInfo(name=build.filename)
                info.mode = 0o700
                info.size = len(script_bytes)
                tar.addfile(info, io.BytesIO(script_bytes))
            archive.seek(0)
            async def provision() -> None:
                await self.put_archive(instance_id, "/tmp", archive.getvalue())
                await self._stream_ssh_process(
                    instance_id,
                    build.command,
                    progress_callback,
                )

            timeout = getattr(self._settings, "qemu_build_script_timeout", 7200)
            try:
                await asyncio.wait_for(provision(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise OperationError(
                    BUILD_SCRIPT_TIMEOUT,
                    f"Image build script exceeded its {timeout:g}s execution limit",
                ) from exc

            manifest = await self._finalize_build_image(
                instance_id,
                disk,
                target_path,
                {
                    "kind": "build",
                    "operation_id": operation_id,
                    "artifact_id": image_instance_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
                **({} if guest.name == LINUX else {"guest": guest}),
            )

            if progress_callback is not None:
                await progress_callback(f"QEMU image finalized at {target_path}")

            return {
                "image_path": str(target_path),
                "size_bytes": str(manifest["size_bytes"]),
                "min_disk_size_gb": manifest["min_disk_size_gb"],
                "virtual_size_bytes": manifest["virtual_size_bytes"],
            }
        finally:
            await self._cleanup_build_instance(instance_id)

    # ── Image artifact operations ─────────────────────────────────────

    def _managed_image_roots(self) -> list[Path]:
        """Return publication and cache roots shared by validation and inventory."""
        cache_dir = getattr(
            getattr(self, "_settings", None),
            "qemu_image_cache_dir",
            self._snapshot_dir.parent / "images",
        )
        return [
            self._snapshot_dir,
            Path(cache_dir),
            getattr(self, "_build_image_dir", _BUILD_IMAGE_DIR),
        ]

    def _managed_image_path(self, reference: str) -> Path:
        """Reject traversal and symlink components, including nonexistent targets."""
        roots = self._managed_image_roots()
        path = Path(reference)
        if not path.is_absolute():
            import re

            if not re.fullmatch(r"[A-Za-z0-9_-]+", reference):
                raise ValueError("Invalid artifact ID")
            path = self._snapshot_dir / f"{reference}.qcow2"
        if path.suffix != ".qcow2" or ".." in path.parts:
            raise ValueError("Invalid image path")
        if not any(path.is_relative_to(root.absolute()) for root in roots):
            raise ValueError("Image outside managed storage")
        if any(parent.is_symlink() for parent in [path, *path.parents]):
            raise ValueError("Symlink image path refused")
        return path.absolute()

    def _publication_digest(self, path: Path) -> str:
        """Cache content verification only for unchanged regular publications.

        Open without following symlinks and compare descriptor/path stat before
        and after reading. ctime prevents same-size/mtime replacement reuse.
        No cache is used for active workspace disks or mutable image info.
        """
        import os
        import stat
        import threading

        # Initialized lazily for runtimes constructed without __init__ in tests.
        if not hasattr(self, "_publication_hash_lock"):
            self._publication_hash_cache = {}
            self._publication_hash_lock = threading.Lock()

        def identity(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
            )

        with self._publication_hash_lock:
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Publication is not a regular file")
            key = str(path.absolute())
            signature = identity(before)
            cached = self._publication_hash_cache.get(key)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or identity(opened) != signature:
                    raise ValueError("Publication changed during inspection")
                digest = (
                    cached[1]
                    if cached and cached[0] == signature
                    else sha256_digest(stream)
                )
                if (
                    identity(os.fstat(stream.fileno())) != signature
                    or identity(path.stat(follow_symlinks=False)) != signature
                ):
                    self._publication_hash_cache.pop(key, None)
                    raise ValueError("Publication changed during verification")
            # Bound memory even if an operator creates many immutable images.
            if len(self._publication_hash_cache) >= 1024:
                self._publication_hash_cache.clear()
            self._publication_hash_cache[key] = (signature, digest)
            return digest

    async def _image_info(self, path: Path) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "qemu-img",
            "info",
            "--force-share",
            "--output=json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, _ = await proc.communicate()
        if proc.returncode:
            raise RuntimeError("Image inspection failed")
        return json.loads(output)

    async def _assert_workspace_resources_unconsumed(self, instance_id: str) -> None:
        """Fence every file we unlink, before destroy and again before cleanup."""
        scan = await self.inventory()
        if not scan.complete:
            raise RuntimeError("Incomplete inventory blocks workspace deletion")
        owned_paths = {
            str(self._disk_path(instance_id).absolute()),
            str(self._cloud_init_iso_path(instance_id).absolute()),
        }
        own_domain = "domain:" + self._domain_name(instance_id)
        if any(
            owned_paths.intersection(resource.dependencies)
            and resource.resource_id != own_domain
            for resource in scan.resources
        ):
            raise RuntimeError("Workspace storage has physical dependents; retained")

    async def workspace_incarnation(self, workspace_id: str) -> tuple[dict, str] | None:
        """Inspect the source independently of unrelated failed capture outputs."""

        def observe():
            matches = [
                d
                for d in self._libvirt_conn().listAllDomains(0)
                if d.name() == self._domain_name(workspace_id)
            ]
            if len(matches) != 1:
                return None
            domain = matches[0]
            root = ET.fromstring(domain.XMLDesc(0))
            paths = [
                Path(n.attrib["file"]).absolute()
                for n in root.findall("./devices/disk/source")
                if "file" in n.attrib
            ]
            if not paths or self._disk_path(workspace_id).absolute() not in paths:
                return None
            disks = []
            for path in paths:
                if any(p.is_symlink() for p in [path, *path.parents]):
                    return None
                stat = path.stat()
                with path.open("rb") as stream:
                    stream.read(1)
                disks.append(
                    {
                        "path": str(path),
                        "device": stat.st_dev,
                        "inode": stat.st_ino,
                        "size": stat.st_size,
                        "mtime_ns": stat.st_mtime_ns,
                        "ctime_ns": stat.st_ctime_ns,
                    }
                )
            state, _ = domain.state()
            if state not in {libvirt.VIR_DOMAIN_RUNNING, libvirt.VIR_DOMAIN_SHUTOFF}:
                return None
            return (
                {
                    "runtime": "qemu",
                    "resource_id": "domain:" + domain.name(),
                    "dependencies": sorted(str(p) for p in paths),
                    "metadata": {
                        "workspace_id": workspace_id,
                        "domain_uuid": domain.UUIDString(),
                        "disks": disks,
                    },
                },
                "running" if state == libvirt.VIR_DOMAIN_RUNNING else "exited",
            )

        try:
            result = await asyncio.to_thread(observe)
            if result:
                for path in result[0]["dependencies"]:
                    await self._image_info(Path(path))
                if await asyncio.to_thread(observe) != result:
                    return None
            return result
        except Exception:
            return None

    @storage_mutation
    async def inventory(self) -> RuntimeInventory:
        """Inspect all owned directories and all libvirt disk references recursively."""
        import os
        import xml.etree.ElementTree as ET

        scan = RuntimeInventory()
        paths: set[Path] = set()
        workspace_disks: dict[str, set[str]] = {}
        roots = [self._disk_dir, *self._managed_image_roots()]
        filesystem_ids: set[str] = set()

        def inspect_filesystem(path: Path, filesystem_id: str) -> None:
            if filesystem_id in filesystem_ids:
                return
            filesystem_ids.add(filesystem_id)
            try:
                stat = os.statvfs(path)
                scan.filesystems.append(
                    {
                        "filesystem_id": filesystem_id,
                        "path": str(path),
                        "capacity_bytes": stat.f_blocks * stat.f_frsize,
                        "available_bytes": stat.f_bavail * stat.f_frsize,
                        "used_bytes": (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                    }
                )
            except OSError:
                scan.errors.append("Filesystem inspection failed: " + str(path))

        def enumerate_resources() -> None:
            for root in roots:
                # Build storage is created on first publication, not at runner boot.
                if root == getattr(self, "_build_image_dir", _BUILD_IMAGE_DIR):
                    if not root.exists():
                        continue
                inspect_filesystem(root, str(root.stat().st_dev))
                for directory, _, files in os.walk(
                    root, onerror=lambda e: scan.errors.append("Directory unreadable")
                ):
                    for name in files:
                        path = Path(directory) / name
                        if (
                            name.endswith((".qcow2", ".img", ".iso", ".tmp"))
                            or ".tmp-" in name
                        ):
                            paths.add(path.absolute())

            def domains():
                return [
                    (d.name(), d.XMLDesc(0), d.UUIDString(), d.state()[0])
                    for d in self._libvirt_conn().listAllDomains(0)
                ]

            for name, xml, domain_uuid, state in domains():
                refs = [
                    Path(n.attrib["file"]).absolute()
                    for n in ET.fromstring(xml).findall("./devices/disk/source")
                    if "file" in n.attrib
                ]
                managed = name.startswith("opencuria-workspace-")
                if managed:
                    for disk in refs:
                        workspace_disks.setdefault(str(disk), set()).add(
                            name.removeprefix("opencuria-workspace-")
                        )
                    scan.resources.append(
                        StorageResource(
                            "domain:" + name,
                            "workspace",
                            True,
                            state=(
                                "exited" if _workspace_status(state) == "stopped"
                                else _workspace_status(state)
                            ),
                            dependencies=[str(p) for p in refs],
                            metadata={
                                "workspace_id": name.removeprefix(
                                    "opencuria-workspace-"
                                ),
                                "domain_uuid": domain_uuid,
                                "disks": [
                                    {
                                        "path": str(p),
                                        "device": p.stat().st_dev,
                                        "inode": p.stat().st_ino,
                                    }
                                    for p in refs
                                ],
                            },
                        )
                    )
                else:
                    scan.foreign_resource_count += 1
                    scan.resources.append(
                        StorageResource(
                            "foreign-domain:" + name,
                            "foreign_reference",
                            False,
                            dependencies=[str(p) for p in refs],
                        )
                    )
                paths.update(refs)

        try:
            await asyncio.to_thread(enumerate_resources)
        except Exception:
            scan.errors.append("Runtime/directory enumeration failed")
        visited: dict[str, StorageResource] = {}

        async def inspect(path: Path, chain: set[str]):
            key = str(path.absolute())
            if key in chain:
                scan.errors.append("Backing cycle: " + key)
                return
            if key in visited:
                return
            managed = any(path.is_relative_to(root.absolute()) for root in roots)
            resource = StorageResource(key, "disk", managed, aliases=[key])
            visited[key] = resource
            scan.resources.append(resource)
            try:
                def inspect_file():
                    if any(p.is_symlink() for p in [path, *path.parents]):
                        raise ValueError("Symlink")
                    stat = path.stat()
                    inspect_filesystem(path, str(stat.st_dev))
                    marker = path.with_suffix(".manifest.json")
                    has_marker = marker.exists() or marker.is_symlink()
                    try:
                        manifest = (
                            json.loads(marker.read_text())
                            if has_marker and not marker.is_symlink() else None
                        )
                    except (OSError, ValueError, UnicodeError):
                        manifest = None
                    return stat, manifest, has_marker

                stat, manifest, has_marker = await asyncio.to_thread(inspect_file)
                resource.allocated_bytes = stat.st_blocks * 512
                resource.logical_bytes = stat.st_size
                resource.metadata = {
                    "filesystem_id": str(stat.st_dev),
                    "file_identity": f"{stat.st_dev}:{stat.st_ino}",
                }
                info = await self._image_info(path)
                resource.virtual_bytes = info.get("virtual-size")
                if has_marker:
                    try:
                        if (
                            not isinstance(manifest, dict)
                            or manifest.get("image_path") != key
                            or manifest.get("size_bytes") != stat.st_size
                            or manifest.get("sha256")
                            != await asyncio.to_thread(self._publication_digest, path)
                            or info.get("backing-filename")
                            or info.get("full-backing-filename")
                        ):
                            raise ValueError("Invalid publication marker")
                    except (OSError, ValueError, TypeError):
                        # Readiness is not graph completeness. A damaged marker
                        # cannot hide a readable image's physical dependents or
                        # block cleanup of every unrelated workspace on the host.
                        resource.metadata["diagnostic_code"] = "invalid_publication"
                    else:
                        resource.metadata = {
                            **manifest,
                            **resource.metadata,
                            "virtual_size_bytes": int(info["virtual-size"]),
                            "min_disk_size_gb": (
                                int(info["virtual-size"]) + 1024**3 - 1
                            ) // 1024**3,
                        }
                        resource.state = "ready"
                        resource.kind = "image"
                elif path.suffix == ".iso":
                    # Directory placement or an arbitrary managed-domain reference
                    # is not ownership. Only the exact workspace's seed path is.
                    owners = workspace_disks.get(key, set())
                    if len(owners) == 1:
                        owner = next(iter(owners))
                        if path == self._cloud_init_iso_path(owner).absolute():
                            resource.state = "observed"
                            resource.metadata.update(
                                {
                                    "workspace_id": owner,
                                    "role": "cloud_init",
                                }
                            )
                elif path.parent == self._disk_dir:
                    resource.state = "observed"
                    owners = workspace_disks.get(key, set())
                    if len(owners) == 1:
                        resource.metadata.update({"workspace_id": next(iter(owners))})
                    elif not owners:
                        try:
                            resource.metadata.update(
                                {
                                    "workspace_id": str(uuid.UUID(path.stem)),
                                    "partial_create": True,
                                }
                            )
                        except ValueError:
                            pass
                else:
                    resource.state = "unknown"
                backing = info.get("full-backing-filename") or info.get(
                    "backing-filename"
                )
                if backing:
                    base = Path(backing)
                    if not base.is_absolute():
                        base = path.parent / base
                    import os

                    base = Path(os.path.abspath(base))
                    resource.dependencies.append(str(base))
                    await inspect(base, chain | {key})
            except Exception:
                scan.errors.append("Image inspection failed: " + key)

        for path in sorted(paths):
            await inspect(path, set())
        scan.complete = not scan.errors
        from datetime import datetime, timezone

        scan.collected_at = datetime.now(timezone.utc).isoformat()
        return scan

    def _publication_stage(self, target: Path, operation_id: str) -> Path:
        """Locate one operation's private staging area without searching storage."""
        key = uuid.uuid5(uuid.NAMESPACE_URL, operation_id + ":" + str(target))
        return target.parent / f".publication-{key}"

    def _reserve_image_publication(
        self, target: Path, manifest: dict, ownership: dict | None = None
    ) -> dict:
        """Persist directory ownership before creating any conversion output."""
        stage = self._publication_stage(target, str(manifest["operation_id"]))
        stage.mkdir(mode=0o700)
        stat = stage.stat()
        if ownership is not None:
            ownership["directory_identity"] = [stat.st_dev, stat.st_ino]
        intent = {
            "version": 1,
            "operation_id": manifest["operation_id"],
            "artifact_id": manifest.get("artifact_id", ""),
            "target": str(target),
            "directory_identity": [stat.st_dev, stat.st_ino],
            "disk_identity": None,
            "marker_identity": None,
        }

        def save(first: bool = False) -> None:
            path = stage / ("intent.json" if first else "intent.next")
            with path.open("x") as stream:
                json.dump(intent, stream)
                stream.flush()
                os.fsync(stream.fileno())
            if not first:
                os.replace(path, stage / "intent.json")
            for directory in (stage, target.parent):
                fd = os.open(directory, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)

        save(first=True)
        for name, identity_field in (
            ("disk.part", "disk_identity"),
            ("manifest.part", "marker_identity"),
        ):
            with (stage / name).open("xb") as stream:
                stat = os.fstat(stream.fileno())
                intent[identity_field] = [stat.st_dev, stat.st_ino]
                if ownership is not None:
                    ownership.setdefault("files", {})[str(stage / name)] = (
                        stat.st_dev,
                        stat.st_ino,
                    )
            anchor = stage / (name + ".owner")
            os.link(stage / name, anchor)
            if ownership is not None:
                ownership["files"][str(anchor)] = (stat.st_dev, stat.st_ino)
        save()
        return intent

    @storage_mutation
    async def recover_image_publication(self, identity: dict) -> None:
        """Clean only a quiescent journal attempt's attested staging resources.

        The caller joins disk helpers before invoking recovery. Staged outputs
        never serve as backing images, but explicit external references still
        appear in the complete inventory and prevent their removal.
        """
        operation_id = identity.get("operation_id")
        artifact_id = identity.get("image_instance_id")
        if not operation_id or not artifact_id:
            return
        reference = identity.get("image_path")
        if not reference:
            reference = (
                str(
                    getattr(self, "_build_image_dir", _BUILD_IMAGE_DIR)
                    / f"{artifact_id}.qcow2"
                )
                if identity.get("failure_event") == "image:build_failed"
                else artifact_id
            )
        target = self._managed_image_path(reference)
        stage = self._publication_stage(target, operation_id)
        if not stage.exists():
            return

        def metadata_only() -> bool:
            import stat as stat_module

            if stage.is_symlink():
                raise RecoveryConflict("Publication staging ownership is unverified")
            entries = list(stage.iterdir())
            if not entries:
                # rmdir is atomic and cannot remove newly appearing contents.
                stage.rmdir()
                fd = os.open(target.parent, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return True
            # Death before the initial intent commit cannot have started a
            # converter. Keep unverified metadata, but close its execution;
            # it must not turn an interrupted reservation into a live fence.
            return all(
                path.name in {"intent.json", "intent.next"}
                and stat_module.S_ISREG(path.lstat().st_mode)
                for path in entries
            )

        if await asyncio.to_thread(metadata_only):
            return

        def load() -> dict:
            import stat as stat_module

            if stage.is_symlink() or (stage / "intent.json").is_symlink():
                raise RecoveryConflict("Publication staging ownership is unverified")
            try:
                intent = json.loads((stage / "intent.json").read_text())
            except (FileNotFoundError, ValueError, UnicodeError) as error:
                raise RecoveryConflict(
                    "Publication staging intent cannot be verified"
                ) from error
            if not isinstance(intent, dict):
                raise RecoveryConflict("Publication staging intent cannot be verified")
            stat = stage.stat()
            if (
                intent.get("version") != 1
                or intent.get("operation_id") != operation_id
                or intent.get("artifact_id") != artifact_id
                or intent.get("target") != str(target)
                or intent.get("directory_identity") != [stat.st_dev, stat.st_ino]
            ):
                raise RecoveryConflict("Publication staging identity mismatch")
            for name, identity_field in (
                ("disk.part", "disk_identity"),
                ("manifest.part", "marker_identity"),
            ):
                path = stage / name
                anchor = stage / (name + ".owner")
                expected = intent.get(identity_field)
                existing = {}
                for child in (path, anchor):
                    try:
                        stat = child.lstat()
                    except FileNotFoundError:
                        continue
                    existing[child] = stat
                    if (
                        not stat_module.S_ISREG(stat.st_mode)
                        or (expected is None and stat.st_size != 0)
                        or (
                            expected is not None
                            and expected != [stat.st_dev, stat.st_ino]
                        )
                    ):
                        raise RecoveryConflict(
                            "Publication staging file ownership changed"
                        )
                if path in existing and expected is not None and anchor not in existing:
                    raise RecoveryConflict(
                        "Publication staging anchor ownership changed"
                    )
            if {path.name for path in stage.iterdir()} - {
                "intent.json",
                "intent.next",
                "disk.part",
                "manifest.part",
                "disk.part.owner",
                "manifest.part.owner",
            }:
                raise RecoveryConflict("Unexpected publication staging resources")
            return intent

        intent = await asyncio.to_thread(load)
        scan = await self.inventory()
        if not scan.complete:
            raise RuntimeError("Incomplete inventory blocks publication cleanup")
        staged_paths = {
            str(stage / name)
            for name in (
                "disk.part",
                "manifest.part",
                "disk.part.owner",
                "manifest.part.owner",
                "intent.json",
                "intent.next",
            )
        }
        if any(
            staged_paths.intersection(resource.dependencies)
            for resource in scan.resources
        ):
            raise RuntimeError("Publication staging has physical dependents")
        marker = target.with_suffix(".manifest.json")
        published = next(
            (
                resource
                for resource in scan.resources
                if resource.resource_id == str(target)
            ),
            None,
        )
        committed = (
            published is not None
            and published.state == "ready"
            and published.metadata.get("operation_id") == operation_id
            and published.metadata.get("artifact_id") == artifact_id
        )
        if marker.exists() and not committed:
            raise RecoveryConflict("Publication commit cannot be verified")
        if target.exists() and not committed:
            stat = await asyncio.to_thread(target.lstat)
            if intent.get("disk_identity") != [stat.st_dev, stat.st_ino]:
                raise RecoveryConflict("Unpublished target ownership changed")
            if any(str(target) in resource.dependencies for resource in scan.resources):
                raise RuntimeError("Unpublished target has physical dependents")

        def cleanup() -> None:
            # Revalidate after inspection and before removing the exact inodes.
            load()
            if target.exists() and not committed:
                stat = target.lstat()
                if intent.get("disk_identity") != [stat.st_dev, stat.st_ino]:
                    raise RecoveryConflict("Unpublished target ownership changed")
                target.unlink()
            for name in (
                "disk.part",
                "manifest.part",
                "disk.part.owner",
                "manifest.part.owner",
            ):
                (stage / name).unlink(missing_ok=True)
            # Ownership metadata must outlive durable removal of every output.
            # A crash cannot resurrect a disk after its intent has disappeared.
            for directory in (stage, target.parent):
                fd = os.open(directory, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            (stage / "intent.next").unlink(missing_ok=True)
            (stage / "intent.json").unlink()
            stage.rmdir()
            fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

        await asyncio.to_thread(cleanup)

    @storage_mutation
    async def _publish_image(
        self, disk: Path, target: Path, manifest: dict, *, compress: bool | None = None
    ) -> dict:
        """Publish an immutable image or roll back this attempt's owned files.

        A committed marker keeps its disk even if the final directory fsync
        fails: journal recovery can validate and recover that publication.
        Ordinary conversion/commit failures must not leave truncated files that
        make every subsequent inventory and unrelated deletion fail.
        """
        target = self._managed_image_path(str(target))
        marker = target.with_suffix(".manifest.json")
        if target.exists() or marker.exists():
            raise FileExistsError("Publication target already exists")
        stage = self._publication_stage(target, str(manifest["operation_id"]))
        temp = stage / "disk.part"
        tmp_marker = stage / "manifest.part"
        stage_identity = None
        owned: dict[Path, tuple[int, int]] = {}
        marker_published = False

        def reserve() -> None:
            nonlocal stage_identity
            # The exclusive directory prevents cleanup from claiming a preexisting
            # attempt. No disk output exists until its initial intent is durable.
            ownership = {}
            try:
                self._reserve_image_publication(target, manifest, ownership)
            finally:
                stage_identity = ownership.get("directory_identity")
                owned.update(
                    {
                        Path(path): identity
                        for path, identity in ownership.get("files", {}).items()
                    }
                )

        def cleanup() -> None:
            for path, identity in owned.items():
                if marker_published and path == target:
                    continue
                try:
                    stat = path.lstat()
                except FileNotFoundError:
                    continue
                if (stat.st_dev, stat.st_ino) != identity:
                    raise RuntimeError("Publication cleanup ownership changed")
                path.unlink()
            if stage_identity is not None:
                stat = stage.lstat()
                if stage_identity != [stat.st_dev, stat.st_ino]:
                    raise RuntimeError("Publication staging ownership changed")
                for path in (stage, target.parent):
                    fd = os.open(path, os.O_DIRECTORY)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                (stage / "intent.next").unlink(missing_ok=True)
                (stage / "intent.json").unlink(missing_ok=True)
                stage.rmdir()
            directory = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        def commit(info: dict) -> dict:
            nonlocal marker_published
            # qemu-img writes as the runner, while libvirt may use another UID.
            temp.chmod(0o644)
            with temp.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(temp, target)  # exclusive, never replaces a published inode
            owned[target] = owned[temp]
            published = {
                **manifest,
                "sha256": self._publication_digest(target),
                "version": 1,
                "image_path": str(target),
                "size_bytes": target.stat().st_size,
                "self_contained": True,
                "virtual_size_bytes": int(info["virtual-size"]),
                "min_disk_size_gb": (int(info["virtual-size"]) + 1024**3 - 1)
                // 1024**3,
            }
            directory = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
                with tmp_marker.open("w") as stream:
                    json.dump(published, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(tmp_marker, marker)
                marker_published = True
                os.fsync(directory)
            finally:
                os.close(directory)
            return published

        try:
            await asyncio.to_thread(reserve)
            if compress is None:
                compress = guest_for(manifest.get("guest_os")).compress_publication
            # Large guests (Windows) are stored compressed; overlays stay raw.
            compression = ["-c", "-o", "compression_type=zstd"] if compress else []
            proc = await asyncio.create_subprocess_exec(
                "qemu-img",
                "convert",
                "-f",
                "qcow2",
                "-O",
                "qcow2",
                *compression,
                str(disk),
                str(temp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode:
                raise RuntimeError("Image conversion failed")
            info = await self._image_info(temp)
            if info.get("backing-filename") or info.get("full-backing-filename"):
                raise RuntimeError("Published image must be self-contained")
            proc = await asyncio.create_subprocess_exec(
                "qemu-img",
                "check",
                str(temp),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            if proc.returncode:
                raise RuntimeError("Image integrity check failed")
            return await asyncio.to_thread(commit, info)
        finally:
            await asyncio.to_thread(cleanup)

    @workspace_mutation
    async def create_image_artifact(
        self,
        instance_id: str,
        name: str,
        *,
        artifact_id: str | None = None,
        operation_id: str | None = None,
        credential_clean: bool = False,
    ) -> ImageArtifactInfo:
        """Capture only a proven scrubbed, fully shut off guest; no live freeze.

        Guests that must be generalized (Windows) are never modified: a
        disposable copy on an overlay of the stopped disk is generalized and
        published instead, so the source keeps its identity and license.
        """
        if not credential_clean or not artifact_id or not operation_id:
            raise RuntimeError(
                "Capture requires deterministic identity and scrub proof"
            )
        artifact_id = str(uuid.UUID(artifact_id))
        target = self._managed_image_path(artifact_id)
        guest = await self._resolve_guest(instance_id)
        manifest_fields = {
            "operation_id": operation_id,
            "artifact_id": artifact_id,
            "instance_id": instance_id,
            "name": name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "credential_clean": True,
            "kind": "capture",
        }
        if guest.name != LINUX:
            manifest_fields["guest_os"] = guest.name
        if not guest.generalize_on_publish:
            manifest = await self._publish_capture(
                instance_id, self._disk_path(instance_id), target, manifest_fields
            )
        else:
            copy_id = self._capture_copy_id(operation_id)
            try:
                copy_disk = await self._prepare_capture_copy(instance_id, copy_id, guest)
                await self._await_generalized(copy_id)
                manifest = await self._publish_capture(
                    instance_id, copy_disk, target, manifest_fields
                )
            finally:
                await self._cleanup_build_instance(copy_id)
        return ImageArtifactInfo(
            artifact_id,
            instance_id,
            name,
            datetime.fromisoformat(manifest["created_at"]),
            manifest["size_bytes"],
            min_disk_size_gb=manifest["min_disk_size_gb"],
            virtual_size_bytes=manifest["virtual_size_bytes"],
        )

    @staticmethod
    def _capture_copy_id(operation_id: str) -> str:
        """Map one capture operation to exactly one disposable generalize guest."""
        try:
            identity = uuid.UUID(operation_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Capture requires a valid operation UUID") from exc
        return f"image-capture-{identity}"

    async def _require_shut_off(self, instance_id: str) -> None:
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        if state != libvirt.VIR_DOMAIN_SHUTOFF:
            raise RuntimeError("Capture requires shut off guest")

    @storage_mutation
    async def _publish_capture(
        self, instance_id: str, disk: Path, target: Path, manifest: dict
    ) -> dict:
        """Publish a capture while the source guest is proven shut off."""
        await self._require_shut_off(instance_id)
        return await self._publish_image(disk, target, manifest)

    @storage_mutation
    async def _prepare_capture_copy(
        self, instance_id: str, copy_id: str, guest: GuestOS
    ) -> Path:
        """Boot a network-less copy of the stopped source that generalizes itself.

        The overlay only reads the source disk; the inventory fence keeps the
        source from being removed while the copy exists.
        """
        await self._require_shut_off(instance_id)
        source = self._disk_path(instance_id)
        info = await self._image_info(source)
        size_gb = (int(info["virtual-size"]) + 1024**3 - 1) // 1024**3
        copy_disk = await self._create_overlay_disk(
            copy_id, disk_size_gb=size_gb, base_image=str(source)
        )
        await self._define_and_start_guest(
            copy_id,
            guest=guest,
            disk=copy_disk,
            vcpus=max(self._settings.qemu_build_vcpus, guest.min_vcpus),
            memory_mb=max(self._settings.qemu_build_memory_mb, guest.min_memory_mb),
            network=False,
            seed_action="generalize",
        )
        return copy_disk

    async def recover_interrupted_capture(self, operation_id: str) -> None:
        """Remove only the interrupted capture's exact generalize copy."""
        copy_id = self._capture_copy_id(operation_id)
        await self.remove_workspace(copy_id)
        if not await self.workspace_removed(copy_id):
            raise RuntimeError("Interrupted capture resources remain present")

    @storage_mutation
    async def delete_image_artifact(self, artifact_id: str) -> None:
        """Full graph rescan; unknown inspection never proves zero dependencies."""
        target = await asyncio.to_thread(self._managed_image_path, artifact_id)
        scan = await self.inventory()
        if not scan.complete:
            raise RuntimeError("Incomplete inventory blocks deletion")
        key = str(target)
        if any(key in item.dependencies for item in scan.resources):
            raise RuntimeError(
                "Image has physical dependents: "
                + ", ".join(
                    r.resource_id for r in scan.resources if key in r.dependencies
                )
            )
        resource = next((r for r in scan.resources if r.resource_id == key), None)
        if resource and resource.state != "ready":
            raise RuntimeError("Unknown image cannot be deleted")
        def unlink_publication() -> None:
            marker = target.with_suffix(".manifest.json")
            changed = False
            for path in (target, marker):
                try:
                    path.unlink()
                    changed = True
                except FileNotFoundError:
                    pass
            for path in (target, marker):
                try:
                    path.lstat()
                except FileNotFoundError:
                    continue
                raise RuntimeError("Image deletion incomplete")
            try:
                directory = os.open(target.parent, os.O_DIRECTORY)
            except FileNotFoundError:
                if changed:
                    raise
                return
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

        await asyncio.to_thread(unlink_publication)

    @workspace_mutation
    async def create_workspace_from_image_artifact(
        self,
        artifact_id: str,
        new_instance_id: str,
        *,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        guest_os: str | None = None,
    ) -> str:
        """Clone one guest, keeping readiness outside the storage graph lock."""
        instance_id = await self._create_workspace_from_image_artifact(
            artifact_id,
            new_instance_id,
            qemu_vcpus=qemu_vcpus,
            qemu_memory_mb=qemu_memory_mb,
            qemu_disk_size_gb=qemu_disk_size_gb,
            guest_os=guest_os,
        )
        await self._wait_for_ssh(instance_id)
        await self._wait_for_cloud_init(instance_id)
        return instance_id

    @storage_mutation
    async def _create_workspace_from_image_artifact(
        self,
        artifact_id: str,
        new_instance_id: str,
        *,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
        guest_os: str | None = None,
    ) -> str:
        """Create a workspace from an image artifact.

        Creates a new QCOW2 overlay backed by the artifact image
        and starts a new VM.
        """
        await asyncio.to_thread(self._assert_workspace_domain_absent, new_instance_id)
        qemu_vcpus = (
            self._settings.qemu_vcpus if qemu_vcpus is None else qemu_vcpus
        )
        qemu_memory_mb = (
            self._settings.qemu_memory_mb if qemu_memory_mb is None else qemu_memory_mb
        )
        qemu_disk_size_gb = (
            self._settings.qemu_disk_size_gb
            if qemu_disk_size_gb is None else qemu_disk_size_gb
        )
        self._validate_workspace_resources(
            qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb
        )
        snapshot_path = self._managed_image_path(artifact_id)
        if not snapshot_path.exists():
            raise OperationRejectedError(
                IMAGE_UNAVAILABLE, f"Image artifact {artifact_id} not found"
            )

        if not snapshot_path.with_suffix(".manifest.json").exists():
            raise OperationRejectedError(
                IMAGE_INVALID, "Clone requires a published image manifest"
            )

        def validate_publication() -> None:
            manifest = json.loads(snapshot_path.with_suffix(".manifest.json").read_text())
            if (
                manifest.get("image_path") != str(snapshot_path)
                or manifest.get("size_bytes") != snapshot_path.stat().st_size
                or manifest.get("sha256") != self._publication_digest(snapshot_path)
            ):
                raise OperationRejectedError(
                    IMAGE_INVALID, "Invalid published image manifest"
                )

        await asyncio.to_thread(validate_publication)
        info = await self._image_info(snapshot_path)
        if info.get("backing-filename") or info.get("full-backing-filename"):
            raise OperationRejectedError(
                IMAGE_INVALID, "Published image must be self-contained"
            )
        guest = await asyncio.to_thread(self._image_guest, snapshot_path, guest_os)
        # Use the same exclusive sparse overlay publication as fresh guests.
        # Too-small requests fail before allocating a disk or network.
        new_disk = await self._create_overlay_disk(
            new_instance_id,
            disk_size_gb=qemu_disk_size_gb,
            base_image=str(snapshot_path),
        )
        # Dedicated isolated network, fresh seed identity, guest-specific domain.
        await self._define_and_start_guest(
            new_instance_id,
            guest=guest,
            disk=new_disk,
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
        )

        logger.info(
            "vm_workspace_cloned",
            snapshot_id=artifact_id,
            new_instance_id=new_instance_id,
        )
        return new_instance_id
