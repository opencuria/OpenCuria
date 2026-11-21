"""Guest operating system strategies for QEMU workspaces.

``QemuRuntime`` owns every lifecycle step once. The few places where a Linux
and a Windows guest differ (firmware and devices, first-boot seed, readiness
probe, timeouts, build script invocation, generalization) delegate to one
:class:`GuestOS` instance chosen from the image manifest and recorded in the
libvirt domain metadata. Linux values reproduce the historical behaviour
exactly; a missing record always means Linux.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from .linux_seed import cloud_init_files

LINUX = "linux"
WINDOWS = "windows"
GUEST_OS_VALUES = frozenset({LINUX, WINDOWS})

#: XML namespace of the runner's libvirt domain metadata.
METADATA_NS = "https://opencuria.dev/libvirt"

FirstBootState = Literal["done", "degraded", "pending"]
#: Where the desktop is served: an in-guest KasmVNC server or QEMU's display.
DesktopKind = Literal["guest", "qemu"]


class GuestBootstrapError(RuntimeError):
    """The guest reported that its first-boot configuration failed."""


@dataclass(frozen=True)
class SeedSpec:
    """Inputs of one NoCloud seed volume."""

    bootstrap_id: str
    public_key: str
    vm_ip: str | None = None
    gateway: str | None = None
    #: ``boot`` configures the guest; ``generalize`` prepares a copy for capture.
    action: Literal["boot", "generalize"] = "boot"


@dataclass(frozen=True)
class BuildScript:
    """Where the image build script is uploaded and how it is run."""

    filename: str
    command: str


class GuestOS(ABC):
    """Differences between guest operating systems, nothing else."""

    name: str
    #: Minimum seconds a booting guest may take until SSH authenticates.
    ssh_timeout_floor: float
    #: Minimum seconds the first boot may take until the guest is configured.
    first_boot_timeout_floor: float
    #: Seconds a graceful shutdown may take before the guest is destroyed.
    shutdown_timeout: float
    #: Whether the workspace network leases the deterministic guest address.
    dhcp: bool
    #: Whether published images are stored compressed.
    compress_publication: bool
    #: Whether captures boot a disposable copy to generalize it first.
    generalize_on_publish: bool
    #: Where the workspace desktop is served from.
    desktop: DesktopKind
    #: Smallest resources a guest of this OS runs with (image builds).
    min_vcpus: int
    min_memory_mb: int
    min_disk_gb: int

    @abstractmethod
    def customize_domain(
        self, root: ET.Element, *, vcpus: int, serial_log: str, vdagent: bool = True
    ) -> None:
        """Add guest-specific firmware and devices to a new domain definition.

        ``vdagent`` is false on hosts whose libvirt lacks the qemu-vdagent channel.
        """

    @abstractmethod
    def seed_files(self, spec: SeedSpec) -> dict[str, str]:
        """Return the files of the guest's NoCloud seed volume."""

    @abstractmethod
    def first_boot_probe(self) -> str:
        """Return the read-only guest command that reports first-boot state."""

    @abstractmethod
    def first_boot_state(self, exit_status: int | None, stdout: str) -> FirstBootState:
        """Interpret the probe; raise :class:`GuestBootstrapError` on failure."""

    @abstractmethod
    def build_script(self) -> BuildScript:
        """Return how the rendered image build script runs in the guest."""


class LinuxGuest(GuestOS):
    """Ubuntu cloud-image guests configured by cloud-init."""

    name = LINUX
    ssh_timeout_floor = 0
    first_boot_timeout_floor = 0
    shutdown_timeout = 30
    dhcp = False
    compress_publication = False
    generalize_on_publish = False
    desktop: DesktopKind = "guest"
    min_vcpus = 1
    min_memory_mb = 1
    min_disk_gb = 1

    def customize_domain(
        self, root: ET.Element, *, vcpus: int, serial_log: str, vdagent: bool = True
    ) -> None:
        del root, vcpus, serial_log, vdagent

    def seed_files(self, spec: SeedSpec) -> dict[str, str]:
        return cloud_init_files(
            bootstrap_id=spec.bootstrap_id,
            public_key=spec.public_key,
            vm_ip=spec.vm_ip,
            gateway=spec.gateway,
        )

    def first_boot_probe(self) -> str:
        return "cloud-init status --format=json"

    def first_boot_state(self, exit_status: int | None, stdout: str) -> FirstBootState:
        status = json.loads(stdout)
        if not isinstance(status, dict):
            raise ValueError("Expected cloud-init status object")
        state = status.get("status")
        if exit_status in (0, 2) and state == "done":
            if status.get("errors"):
                raise GuestBootstrapError("Guest cloud-init completed with errors")
            return "degraded" if exit_status == 2 else "done"
        if exit_status not in (0, 2) or state not in ("running", "not started"):
            raise GuestBootstrapError("Guest cloud-init did not complete successfully")
        return "pending"

    def build_script(self) -> BuildScript:
        return BuildScript(
            filename="opencuria-image-build.sh",
            command="sudo -E bash /tmp/opencuria-image-build.sh",
        )


#: Paths of the Windows guest contract (see ``runtime/windows/``).
WINDOWS_STATE_DIR = "/c/ProgramData/opencuria"
WINDOWS_BOOTSTRAP_STATUS = f"{WINDOWS_STATE_DIR}/bootstrap.json"
#: Seconds between guest memory reports of the Windows balloon service.
BALLOON_STATS_PERIOD = 10


class WindowsGuest(GuestOS):
    """Windows 11 guests configured by the OpenCuria bootstrap task.

    UEFI with Secure Boot and a TPM 2.0 match Windows 11's requirements; the
    Hyper-V enlightenments keep it fast under KVM. The display is QEMU's own VNC
    server, reached through libvirt without any TCP listener.
    """

    name = WINDOWS
    # A generalized image runs specialize and OOBE on its first boot.
    ssh_timeout_floor = 1800
    first_boot_timeout_floor = 600
    shutdown_timeout = 180
    dhcp = True
    compress_publication = True
    generalize_on_publish = True
    desktop: DesktopKind = "qemu"
    min_vcpus = 2
    min_memory_mb = 4096
    min_disk_gb = 64

    def customize_domain(
        self, root: ET.Element, *, vcpus: int, serial_log: str, vdagent: bool = True
    ) -> None:
        os_node = root.find("./os")
        assert os_node is not None
        os_node.set("firmware", "efi")
        for boot in os_node.findall("./boot"):
            os_node.remove(boot)
        firmware = ET.SubElement(os_node, "firmware")
        ET.SubElement(firmware, "feature", enabled="yes", name="secure-boot")
        ET.SubElement(firmware, "feature", enabled="yes", name="enrolled-keys")
        ET.SubElement(os_node, "boot", dev="hd")

        features = root.find("./features")
        assert features is not None
        ET.SubElement(features, "smm", state="on")
        hyperv = ET.SubElement(features, "hyperv", mode="custom")
        for name in (
            "relaxed", "vapic", "vpindex", "runtime", "synic", "stimer",
            "frequencies", "tlbflush", "ipi",
        ):
            ET.SubElement(hyperv, name, state="on")
        ET.SubElement(hyperv, "spinlocks", state="on", retries="8191")

        # Windows 11 Pro uses at most two sockets: expose cores, not sockets.
        cpu = root.find("./cpu")
        assert cpu is not None
        ET.SubElement(
            cpu, "topology", sockets="1", dies="1", cores=str(vcpus), threads="1"
        )

        clock = root.find("./clock")
        assert clock is not None
        ET.SubElement(clock, "timer", name="rtc", tickpolicy="catchup")
        ET.SubElement(clock, "timer", name="pit", tickpolicy="delay")
        ET.SubElement(clock, "timer", name="hpet", present="no")
        ET.SubElement(clock, "timer", name="hypervclock", present="yes")

        devices = root.find("./devices")
        assert devices is not None
        serial = devices.find("./serial")
        if serial is not None:
            # The guest reports bootstrap and generalize results on COM1.
            ET.SubElement(serial, "log", file=serial_log, append="off")
        ET.SubElement(devices, "controller", type="usb", model="qemu-xhci")
        ET.SubElement(devices, "input", type="tablet", bus="usb")
        if vdagent:
            # Optional: the guest clipboard runs in the console session, and the
            # USB tablet already reports absolute pointer positions.
            channel = ET.SubElement(devices, "channel", type="qemu-vdagent")
            source = ET.SubElement(channel, "source")
            ET.SubElement(source, "clipboard", copypaste="yes")
            ET.SubElement(source, "mouse", mode="client")
            ET.SubElement(channel, "target", type="virtio", name="com.redhat.spice.0")
        graphics = ET.SubElement(devices, "graphics", type="vnc", sharePolicy="force-shared")
        ET.SubElement(graphics, "listen", type="none")
        video = ET.SubElement(devices, "video")
        ET.SubElement(video, "model", type="virtio", heads="1")
        tpm = ET.SubElement(devices, "tpm", model="tpm-crb")
        ET.SubElement(tpm, "backend", type="emulator", version="2.0")
        balloon = devices.find("./memballoon")
        if balloon is not None:
            # Windows touches all of its memory at boot, so the QEMU process
            # size says nothing; the balloon service reports free memory.
            ET.SubElement(balloon, "stats", period=str(BALLOON_STATS_PERIOD))

    def seed_files(self, spec: SeedSpec) -> dict[str, str]:
        return {
            "meta-data": (
                f"instance-id: {spec.bootstrap_id}\n"
                "local-hostname: opencuria-workspace\n"
            ),
            "opencuria.json": json.dumps(
                {"ssh_authorized_keys": [spec.public_key], "action": spec.action}
            ),
        }

    def first_boot_probe(self) -> str:
        return f"cat {WINDOWS_BOOTSTRAP_STATUS} 2>/dev/null || true"

    def first_boot_state(self, exit_status: int | None, stdout: str) -> FirstBootState:
        text = stdout.strip()
        if exit_status != 0 or not text:
            return "pending"
        status = json.loads(text)
        if not isinstance(status, dict):
            raise ValueError("Expected bootstrap status object")
        if status.get("status") == "ready":
            return "done"
        if status.get("status") == "failed":
            raise GuestBootstrapError("Guest bootstrap failed")
        return "pending"

    def build_script(self) -> BuildScript:
        return BuildScript(
            filename="opencuria-image-build.ps1",
            command=(
                "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
                '-File "$(cygpath -w /tmp/opencuria-image-build.ps1)"'
            ),
        )


_GUESTS: dict[str, GuestOS] = {LINUX: LinuxGuest(), WINDOWS: WindowsGuest()}


def guest_for(name: str | None) -> GuestOS:
    """Return the strategy for a recorded guest OS (default Linux)."""
    return _GUESTS.get(name or LINUX) or _GUESTS[LINUX]


def guest_for_distro(base_distro: str | None) -> GuestOS:
    """Return the strategy for an image recipe's base distro."""
    distro = (base_distro or "").strip().lower()
    return _GUESTS[WINDOWS] if distro.startswith("windows:") else _GUESTS[LINUX]


def validate_guest_os(value: object) -> str:
    """Return a known guest OS name or raise ``ValueError``."""
    if value is None:
        return LINUX
    if value not in GUEST_OS_VALUES:
        raise ValueError(f"Unknown guest OS: {value!r}")
    return str(value)
