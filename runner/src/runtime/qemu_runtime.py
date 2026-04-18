"""QEMU/KVM runtime backend using libvirt + asyncssh.

Manages workspace VMs with QCOW2 copy-on-write overlays for fast
cloning.  All command execution is performed over SSH.
"""

from __future__ import annotations

import abc
import asyncio
import io
import ipaddress
import json
import re
import shutil
import subprocess
import tarfile
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncssh
import libvirt
import structlog

from ..config import RunnerSettings
from .base import (
    CommandExecutionError,
    PtyHandle,
    RuntimeBackend,
    RuntimeStatus,
    RuntimeWorkspaceInfo,
    ImageArtifactInfo,
    WorkspaceConfig,
)

logger = structlog.get_logger(__name__)

# Label prefix stored in VM description (JSON metadata)
_LABEL_PREFIX = "opencuria"

# Cloud-init template for initial VM network config
_CLOUD_INIT_META = """\
instance-id: {instance_id}
local-hostname: opencuria-workspace
"""

_CLOUD_INIT_USER = """\
#cloud-config
users:
  - name: ubuntu
    groups: [sudo]
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: false
    shell: /bin/bash
    ssh_authorized_keys:
      - {ssh_public_key}
write_files:
  - path: /root/.ssh/authorized_keys
    permissions: '0600'
    content: |
      {ssh_public_key}
runcmd:
  - mkdir -p /workspace
  - mkdir -p /root/.ssh
  - chmod 700 /root/.ssh
  - chmod 600 /root/.ssh/authorized_keys
  - echo 'PermitRootLogin prohibit-password' > /etc/ssh/sshd_config.d/99-opencuria-root.conf
  - systemctl restart ssh
  - tune2fs -e continue /dev/vda1
"""

# Cloud-init v2 (netplan) network config for static IP assignment inside the VM.
# Applied at first boot so the VM always comes up with a predictable
# address, independent of any DHCP server.
# ``match: name: "en*"`` covers both enp1s0 (q35/PCI) and ens3 (i440fx/ISA).
_CLOUD_INIT_NETWORK = """\
version: 2
ethernets:
  id0:
    match:
      name: "en*"
    addresses:
      - {vm_ip}/30
    routes:
      - to: default
        via: {gateway}
    nameservers:
      addresses:
        - 8.8.8.8
        - 1.1.1.1
"""


def _domain_xml(
    name: str,
    vcpus: int,
    memory_mb: int,
    disk_path: str,
    cloud_init_iso: str,
    network: str,
) -> str:
    """Build a libvirt domain XML definition.

    Each VM is connected to its own dedicated isolated network (created by
    ``QemuRuntime._create_workspace_network``).  No MAC address is pinned
    here because DHCP is not used — the static IP is configured inside the
    guest via cloud-init's ``network-config`` file.
    """
    memory_kb = memory_mb * 1024
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
    <interface type='network'>
      <source network='{network}'/>
      <model type='virtio'/>
    </interface>
    <serial type='pty'>
      <target port='0'/>
    </serial>
    <console type='pty'>
      <target type='serial' port='0'/>
    </console>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
  </devices>
  <metadata>
    <opencuria:workspace xmlns:opencuria='https://opencuria.dev/libvirt'>
      <opencuria:workspace-id>{name.replace("opencuria-workspace-", "")}</opencuria:workspace-id>
    </opencuria:workspace>
  </metadata>
</domain>
"""


class QemuRuntime(RuntimeBackend):
    """QEMU/KVM runtime using libvirt for VM lifecycle and asyncssh for exec."""

    def __init__(self, settings: RunnerSettings) -> None:
        self._settings = settings
        self._conn: libvirt.virConnect | None = None
        self._ssh_connections: dict[str, asyncssh.SSHClientConnection] = {}
        # Per-instance lock: ensures only one coroutine at a time does the
        # SSH liveness check or reconnect, avoiding a burst of N concurrent
        # `echo ok` channels when many commands arrive simultaneously.
        self._ssh_locks: dict[str, asyncio.Lock] = {}
        self._disk_dir = Path(settings.qemu_disk_dir)
        self._snapshot_dir = Path(settings.qemu_snapshot_dir)
        self._ensure_host_directory(
            self._disk_dir,
            writable_hint=(
                f"sudo install -d -o $USER -g $USER -m 755 '{self._disk_dir}'"
            ),
        )
        self._ensure_host_directory(
            self._snapshot_dir,
            writable_hint=(
                "sudo install -d -o $USER -g $USER -m 755 "
                f"'{self._snapshot_dir}'"
            ),
        )

        # Load or generate SSH keypair for VM access
        self._ssh_key_path = Path(
            settings.qemu_ssh_key_path
            or str(self._disk_dir / "opencuria-qemu-key")
        )
        self._ssh_user = settings.qemu_ssh_user
        self._ssh_timeout = settings.qemu_ssh_timeout
        # In-memory TOFU host-key cache: instance_id -> "ip keytype keydata"
        # (known_hosts line format).  Populated on first SSH connect and
        # persisted to disk so it survives runner restarts.
        self._host_key_cache: dict[str, str] = {}
        self._ensure_ssh_keypair()

    # ── Properties ────────────────────────────────────────────────────

    @property
    def runtime_type(self) -> str:
        return "qemu"

    @property
    def supports_image_artifacts(self) -> bool:
        return True

    # ── Internal helpers ──────────────────────────────────────────────

    def _ensure_ssh_keypair(self) -> None:
        """Generate an Ed25519 SSH keypair if it doesn't exist."""
        if not self._ssh_key_path.exists():
            import subprocess

            self._ssh_key_path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ssh-keygen", "-t", "ed25519",
                    "-f", str(self._ssh_key_path),
                    "-N", "",  # no passphrase
                    "-C", "opencuria-qemu-runtime",
                ],
                check=True,
                capture_output=True,
            )
            logger.info(
                "ssh_keypair_generated", path=str(self._ssh_key_path)
            )

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
                "Missing permissions for QEMU host directory "
                f"'{path}'. Run: {hint}"
            ) from exc

    def _get_ssh_public_key(self) -> str:
        """Read the SSH public key."""
        pub_path = self._ssh_key_path.with_suffix(
            self._ssh_key_path.suffix + ".pub"
        )
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

    def _get_domain(self, instance_id: str) -> libvirt.virDomain:
        """Look up a libvirt domain by instance_id."""
        conn = self._libvirt_conn()
        name = self._domain_name(instance_id)
        try:
            return conn.lookupByName(name)
        except libvirt.libvirtError as exc:
            raise RuntimeError(
                f"VM '{name}' not found: {exc}"
            ) from exc

    async def _create_overlay_disk(
        self,
        instance_id: str,
        *,
        disk_size_gb: int,
        base_image: str | None = None,
    ) -> Path:
        """Create a QCOW2 overlay backed by the base image."""
        disk = self._disk_path(instance_id)
        if not base_image:
            raise RuntimeError("QEMU overlay creation requires an explicit base image")
        backing = base_image
        if not Path(backing).exists():
            raise RuntimeError(
                f"QEMU base image not found: {backing}"
            )

        cmd = [
            "qemu-img", "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", backing,
            str(disk),
            f"{disk_size_gb}G",
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create overlay disk: {stderr.decode()}"
            )
        # libvirt-qemu runs as a different uid — ensure it can read the file.
        disk.chmod(0o644)
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
        if target.exists():
            return target

        url = (
            "https://cloud-images.ubuntu.com/releases/server/"
            f"{normalized_version}/release/"
            f"ubuntu-{normalized_version}-server-cloudimg-amd64.img"
        )
        tmp_target = target.with_suffix(f"{target.suffix}.tmp")
        try:
            with urllib.request.urlopen(url, timeout=300) as response:
                with tmp_target.open("wb") as f:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)
            tmp_target.chmod(0o644)
            tmp_target.replace(target)
            target.chmod(0o644)
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
        exit_code = int(process.exit_status or 0)
        if exit_code != 0:
            raise RuntimeError(f"QEMU image build failed with exit code {exit_code}")

    async def _create_cloud_init_iso(
        self,
        instance_id: str,
        vm_ip: str | None = None,
        gateway: str | None = None,
    ) -> Path:
        """Create a cloud-init ISO for VM bootstrap.

        When ``vm_ip`` and ``gateway`` are provided a ``network-config``
        (cloud-init v2 format) is embedded in the ISO so the guest
        configures a static IP at first boot — no DHCP server required.
        """
        iso_path = self._cloud_init_iso_path(instance_id)
        work_dir = self._disk_dir / f"{instance_id}-cloud-init"
        work_dir.mkdir(parents=True, exist_ok=True)

        meta = _CLOUD_INIT_META.format(instance_id=instance_id)
        user = _CLOUD_INIT_USER.format(
            ssh_public_key=self._get_ssh_public_key()
        )

        (work_dir / "meta-data").write_text(meta)
        (work_dir / "user-data").write_text(user)

        # Static network config (cloud-init v2) — gives the VM a
        # predictable IP without a DHCP server on the isolated network.
        files_to_include = [
            str(work_dir / "user-data"),
            str(work_dir / "meta-data"),
        ]
        if vm_ip and gateway:
            network_cfg = _CLOUD_INIT_NETWORK.format(
                vm_ip=vm_ip, gateway=gateway
            )
            (work_dir / "network-config").write_text(network_cfg)
            files_to_include.append(str(work_dir / "network-config"))

        # Use genisoimage or mkisofs to create the ISO
        for tool in ("genisoimage", "mkisofs"):
            if shutil.which(tool):
                cmd = [
                    tool,
                    "-output", str(iso_path),
                    "-volid", "cidata",
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
        iso_path.chmod(0o644)

        # Clean up temp dir
        shutil.rmtree(work_dir, ignore_errors=True)
        return iso_path

    # ── Per-workspace network helpers ─────────────────────────────────

    def _workspace_network_name(self, instance_id: str) -> str:
        """Libvirt network name for this workspace's isolated network."""
        return f"opencuria-ws-{instance_id}"

    def _workspace_bridge_name(self, instance_id: str) -> str:
        """Linux bridge interface name (max 15 chars) for the workspace network.

        Format: ``kai`` + first 9 hex chars of the instance UUID (dashes removed).
        Total length: 3 + 9 = 12 characters — safely under the 15-char limit.
        """
        hex_id = instance_id.replace("-", "")
        return f"kai{hex_id[:9]}"

    def _get_existing_workspace_gateways(self) -> set[str]:
        """Return gateway IPs of all active opencuria workspace networks."""
        conn = self._libvirt_conn()
        gateways: set[str] = set()
        try:
            networks = conn.listAllNetworks(0)
        except libvirt.libvirtError:
            return gateways
        for net in networks:
            try:
                if not net.name().startswith("opencuria-ws-"):
                    continue
                root = ET.fromstring(net.XMLDesc(0))
                for ip_elem in root.findall("./ip"):
                    addr = ip_elem.get("address")
                    if addr:
                        gateways.add(addr)
            except (libvirt.libvirtError, ET.ParseError):
                pass
        return gateways

    def _get_reserved_ipv4_networks(self) -> list[ipaddress.IPv4Network]:
        """Collect host + libvirt IPv4 networks that must not be reused.

        This prevents libvirt define/create failures when a candidate subnet
        overlaps with the host uplink (for example ``enp1s0``) or another
        existing libvirt network.
        """
        reserved: list[ipaddress.IPv4Network] = []

        # Host interface networks (e.g. from enp1s0).
        try:
            result = subprocess.run(
                ["ip", "-o", "-f", "inet", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    cidr = parts[3]
                    try:
                        reserved.append(
                            ipaddress.IPv4Network(cidr, strict=False)
                        )
                    except ValueError:
                        continue
        except OSError:
            pass

        # Existing libvirt network ranges.
        conn = self._libvirt_conn()
        try:
            networks = conn.listAllNetworks(0)
        except libvirt.libvirtError:
            networks = []
        for net in networks:
            try:
                root = ET.fromstring(net.XMLDesc(0))
            except (libvirt.libvirtError, ET.ParseError):
                continue
            for ip_elem in root.findall("./ip"):
                address = ip_elem.get("address")
                prefix = ip_elem.get("prefix")
                netmask = ip_elem.get("netmask")
                if not address:
                    continue
                cidr: str | None = None
                if prefix:
                    cidr = f"{address}/{prefix}"
                elif netmask:
                    cidr = f"{address}/{netmask}"
                if not cidr:
                    continue
                try:
                    reserved.append(ipaddress.IPv4Network(cidr, strict=False))
                except ValueError:
                    continue

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
        self, instance_id: str
    ) -> tuple[str, str]:
        """Create a dedicated isolated /30 NAT network for this workspace.

        Each workspace gets its own libvirt network with a unique /30 subnet
        in the ``10.100.0.0/16`` range.  The network uses NAT so the VM
        can reach the internet, but VMs on different networks cannot reach
        each other because they are on separate bridge interfaces.

        Returns ``(gateway_ip, vm_ip)``.
        """
        def _create() -> tuple[str, str]:
            gateway, vm_ip, netmask = self._pick_workspace_subnet()
            net_name = self._workspace_network_name(instance_id)
            bridge_name = self._workspace_bridge_name(instance_id)
            net_xml = f"""\
<network>
  <name>{net_name}</name>
  <bridge name='{bridge_name}' stp='on' delay='0'/>
  <forward mode='nat'>
    <nat>
      <port start='1024' end='65535'/>
    </nat>
  </forward>
  <ip address='{gateway}' netmask='{netmask}'/>
</network>"""
            conn = self._libvirt_conn()
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
            except libvirt.libvirtError:
                return  # already gone
            try:
                if net.isActive():
                    net.destroy()
            except libvirt.libvirtError:
                pass
            try:
                net.undefine()
            except libvirt.libvirtError:
                pass

        await asyncio.to_thread(_destroy)
        logger.info(
            "vm_network_destroyed", instance_id=instance_id, network=net_name
        )

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

    async def _wait_for_ssh(self, instance_id: str) -> str:
        """Wait for the VM to become reachable via SSH, return its IP.

        The VM's IP is always the second host address in its dedicated /30
        network (gateway + 1), so there is no DHCP discovery needed.

        On the first successful connection the server's host key is saved
        (TOFU).  All subsequent calls to ``_get_ssh`` will verify against
        the stored key.
        """
        vm_ip = self._get_workspace_vm_ip(instance_id)
        deadline = asyncio.get_event_loop().time() + self._ssh_timeout

        # Errors that warrant a retry rather than an immediate failure.
        _SSH_RETRY_ERRORS = (OSError, asyncssh.Error, asyncio.TimeoutError)

        while asyncio.get_event_loop().time() < deadline:
            try:
                conn = await asyncssh.connect(
                    vm_ip,
                    username=self._ssh_user,
                    client_keys=[str(self._ssh_key_path)],
                    known_hosts=None,  # first connect: TOFU — key saved below
                    connect_timeout=5,
                )
                self._ssh_connections[instance_id] = conn
                # Persist the host key so all future reconnects can verify it.
                await self._save_tofu_key(instance_id, vm_ip, conn)
                logger.info(
                    "vm_ssh_ready",
                    instance_id=instance_id,
                    ip=vm_ip,
                )
                return vm_ip
            except _SSH_RETRY_ERRORS:
                pass

            await asyncio.sleep(2)

        raise RuntimeError(
            f"VM {instance_id} did not become reachable via SSH "
            f"within {self._ssh_timeout}s"
        )

    async def _get_ssh(
        self, instance_id: str
    ) -> asyncssh.SSHClientConnection:
        """Get an existing SSH connection or reconnect.

        A per-instance lock ensures that only one coroutine at a time
        performs the liveness check or reconnection, preventing a burst of
        concurrent ``echo ok`` SSH channels when many commands arrive at once.
        """
        lock = self._ssh_locks.get(instance_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ssh_locks[instance_id] = lock

        async with lock:
            conn = self._ssh_connections.get(instance_id)
            if conn is not None:
                # Quick liveness check
                try:
                    result = await conn.run("echo ok", check=True, timeout=5)
                    if result.stdout.strip() == "ok":
                        return conn
                except Exception:
                    pass
                # Connection stale — remove and reconnect
                try:
                    conn.close()
                except Exception:
                    pass
                del self._ssh_connections[instance_id]

            # Reconnect — also catches asyncio.TimeoutError (not an OSError in Python ≤3.10)
            ip = await self._get_vm_ip(instance_id)
            # Use the stored TOFU host key for all reconnections so that a
            # rogue process impersonating the VM's IP is rejected.
            known_hosts = self._load_tofu_key(instance_id, ip)
            try:
                conn = await asyncssh.connect(
                    ip,
                    username=self._ssh_user,
                    client_keys=[str(self._ssh_key_path)],
                    known_hosts=known_hosts,
                    connect_timeout=10,
                )
            except asyncssh.HostKeyNotVerifiable as exc:
                raise RuntimeError(
                    f"SSH host key mismatch for VM {instance_id} ({ip}) — "
                    "possible MITM or VM was recreated. "
                    f"Delete {self._host_key_file(instance_id)} to reset."
                ) from exc
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
                raise RuntimeError(
                    f"SSH reconnect to VM {instance_id} ({ip}) failed: {exc}"
                ) from exc
            # If no key was stored yet (e.g. runner restarted before save),
            # save it now — still within the isolated network so TOFU is safe.
            if not self._host_key_cache.get(instance_id):
                await self._save_tofu_key(instance_id, ip, conn)
            self._ssh_connections[instance_id] = conn
            return conn

    async def _get_vm_ip(self, instance_id: str) -> str:
        """Get the IP address of a running VM.

        With per-workspace isolated networks the VM IP is always deterministic
        (second host in the /30 block) and can be read directly from the
        network's libvirt XML — no DHCP lease lookup required.
        """
        return self._get_workspace_vm_ip(instance_id)

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        """Return the VM IP used by the runner desktop proxy.

        The desktop session payload stores the upstream address under the historical
        ``container_ip`` key for both Docker containers and QEMU VMs.
        """
        del workspace_id
        return self._get_workspace_vm_ip(instance_id)

    def get_workspace_network_name(self, workspace_id: str) -> str:
        """Return the workspace network marker used in desktop session payloads.

        Runner-proxied QEMU desktops do not require an attachable Docker network.
        """
        del workspace_id
        return ""

    # ── RuntimeBackend implementation ─────────────────────────────────

    async def create_workspace(self, config: WorkspaceConfig) -> str:
        """Create a new QEMU/KVM VM workspace.

        Each workspace gets its own dedicated /30 NAT network so VMs are
        fully isolated from one another at the network level.
        """
        instance_id = config.workspace_id
        qemu_vcpus = config.qemu_vcpus or self._settings.qemu_vcpus
        qemu_memory_mb = config.qemu_memory_mb or self._settings.qemu_memory_mb
        qemu_disk_size_gb = config.qemu_disk_size_gb or self._settings.qemu_disk_size_gb

        # ── Create dedicated isolated network ─────────────────────────
        gateway, vm_ip = await self._create_workspace_network(instance_id)

        # Create QCOW2 overlay disk
        disk = await self._create_overlay_disk(
            instance_id,
            disk_size_gb=qemu_disk_size_gb,
            base_image=config.image,
        )

        # Create cloud-init ISO (includes static network config for the VM)
        cloud_init_iso = await self._create_cloud_init_iso(
            instance_id,
            vm_ip=vm_ip,
            gateway=gateway,
        )

        # Define and start the VM via libvirt
        domain_xml = _domain_xml(
            name=self._domain_name(instance_id),
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
            disk_path=str(disk),
            cloud_init_iso=str(cloud_init_iso),
            network=self._workspace_network_name(instance_id),
        )

        def _define_and_start() -> None:
            conn = self._libvirt_conn()
            dom = conn.defineXML(domain_xml)
            dom.create()  # start the VM

        await asyncio.to_thread(_define_and_start)

        # Wait for SSH to become available
        await self._wait_for_ssh(instance_id)

        logger.info(
            "vm_workspace_created",
            instance_id=instance_id,
            network=self._workspace_network_name(instance_id),
            gateway=gateway,
            vm_ip=vm_ip,
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
            disk_size_gb=qemu_disk_size_gb,
        )
        return instance_id

    async def stop_workspace(self, instance_id: str) -> None:
        """Gracefully shut down the VM (ACPI shutdown)."""
        # Close SSH connection and remove associated lock
        conn = self._ssh_connections.pop(instance_id, None)
        if conn:
            conn.close()
        self._ssh_locks.pop(instance_id, None)

        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        if state == libvirt.VIR_DOMAIN_RUNNING:
            await asyncio.to_thread(domain.shutdown)
            # Wait for shutdown with timeout
            for _ in range(30):
                await asyncio.sleep(1)
                state, _ = await asyncio.to_thread(domain.state)
                if state == libvirt.VIR_DOMAIN_SHUTOFF:
                    break
            else:
                # Force destroy if graceful shutdown fails
                await asyncio.to_thread(domain.destroy)

        logger.info("vm_workspace_stopped", instance_id=instance_id)

    async def start_workspace(self, instance_id: str) -> None:
        """Start a previously stopped VM."""
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        if state != libvirt.VIR_DOMAIN_RUNNING:
            await asyncio.to_thread(domain.create)
            await self._wait_for_ssh(instance_id)
        logger.info("vm_workspace_started", instance_id=instance_id)

    async def restart_workspace(self, instance_id: str) -> None:
        """Force-restart a VM for self-healing.

        Unlike ``stop_workspace`` (which waits up to 30 s for graceful ACPI
        shutdown), this method immediately destroys the domain and starts it
        again.  It is intentionally fast because it is called only when the
        VM is already unresponsive.
        """
        # Drop the cached SSH connection — it is no longer usable.
        conn = self._ssh_connections.pop(instance_id, None)
        if conn:
            conn.close()
        self._ssh_locks.pop(instance_id, None)

        def _force_restart() -> None:
            conn_lv = self._libvirt_conn()
            try:
                domain = conn_lv.lookupByName(instance_id)
            except libvirt.libvirtError:
                return  # domain gone — nothing to restart
            state, _ = domain.state()
            if state == libvirt.VIR_DOMAIN_RUNNING:
                try:
                    domain.destroy()
                except libvirt.libvirtError:
                    pass  # already stopped by the time we get here
            domain.create()

        await asyncio.to_thread(_force_restart)
        await self._wait_for_ssh(instance_id)
        logger.info("vm_workspace_force_restarted", instance_id=instance_id)

    async def reconfigure_workspace(
        self,
        instance_id: str,
        *,
        qemu_vcpus: int,
        qemu_memory_mb: int,
        qemu_disk_size_gb: int,
        restart: bool,
    ) -> None:
        """Apply CPU/RAM/disk changes to a VM definition and optionally restart."""
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)
        was_running = state == libvirt.VIR_DOMAIN_RUNNING

        # Stop the VM before touching the backing image or definition.
        # qemu-img may fail to acquire a write lock while the domain is running.
        if was_running:
            await self.stop_workspace(instance_id)
            domain = await asyncio.to_thread(self._get_domain, instance_id)

        # Read current disk path from domain XML.
        xml = await asyncio.to_thread(domain.XMLDesc, 0)
        root = ET.fromstring(xml)
        disk_source = root.find("./devices/disk[@device='disk']/source")
        if disk_source is None:
            raise RuntimeError(f"Could not locate disk source in VM XML for {instance_id}")
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
                raise RuntimeError(f"Failed to resize disk image: {resize_stderr.decode()}")

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

        updated_xml = ET.tostring(root, encoding="unicode")
        conn = self._libvirt_conn()
        await asyncio.to_thread(conn.defineXML, updated_xml)

        should_start = restart or was_running
        if should_start:
            updated_domain = await asyncio.to_thread(self._get_domain, instance_id)
            await asyncio.to_thread(updated_domain.create)
            await self._wait_for_ssh(instance_id)

        logger.info(
            "vm_workspace_reconfigured",
            instance_id=instance_id,
            vcpus=qemu_vcpus,
            memory_mb=qemu_memory_mb,
            disk_size_gb=qemu_disk_size_gb,
            restarted=should_start,
        )

    async def remove_workspace(self, instance_id: str) -> None:
        """Destroy and undefine the VM, its network, disk files and host key."""
        # Close SSH connection and remove associated lock
        conn = self._ssh_connections.pop(instance_id, None)
        if conn:
            conn.close()
        self._ssh_locks.pop(instance_id, None)

        try:
            domain = await asyncio.to_thread(self._get_domain, instance_id)
            state, _ = await asyncio.to_thread(domain.state)
            if state == libvirt.VIR_DOMAIN_RUNNING:
                await asyncio.to_thread(domain.destroy)
            await asyncio.to_thread(
                domain.undefineFlags,
                libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                | libvirt.VIR_DOMAIN_UNDEFINE_MANAGED_SAVE,
            )
        except libvirt.libvirtError:
            logger.warning(
                "vm_undefine_failed", instance_id=instance_id
            )

        # ── Tear down the per-workspace network ───────────────────────
        # Must happen *after* the domain is destroyed so libvirt doesn't
        # refuse to remove the network while it still has active interfaces.
        await self._destroy_workspace_network(instance_id)

        # Clean up disk files
        disk = self._disk_path(instance_id)
        if disk.exists():
            disk.unlink()
        iso = self._cloud_init_iso_path(instance_id)
        if iso.exists():
            iso.unlink()

        # Remove persisted host key and in-memory cache entry
        key_file = self._host_key_file(instance_id)
        if key_file.exists():
            key_file.unlink()
        self._host_key_cache.pop(instance_id, None)

        logger.info("vm_workspace_removed", instance_id=instance_id)

    async def workspace_exists(self, instance_id: str) -> bool:
        """Check if a VM domain exists."""
        try:
            await asyncio.to_thread(self._get_domain, instance_id)
            return True
        except RuntimeError:
            return False

    async def get_workspace_status(self, instance_id: str) -> RuntimeStatus:
        """Map libvirt domain state to a RuntimeStatus."""
        domain_name = self._domain_name(instance_id)
        try:
            domain = await asyncio.to_thread(self._get_domain, instance_id)
            state, _ = await asyncio.to_thread(domain.state)
        except RuntimeError:
            return RuntimeStatus(instance_id=instance_id, status="removed", name=domain_name)

        state_map = {
            libvirt.VIR_DOMAIN_RUNNING: "running",
            libvirt.VIR_DOMAIN_PAUSED: "stopped",
            libvirt.VIR_DOMAIN_SHUTDOWN: "stopped",
            libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
            libvirt.VIR_DOMAIN_CRASHED: "failed",
            libvirt.VIR_DOMAIN_PMSUSPENDED: "stopped",
        }
        return RuntimeStatus(
            instance_id=instance_id,
            status=state_map.get(state, "unknown"),
            name=domain_name,
        )

    async def exec_command(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Execute a command in the VM via SSH and stream output."""
        ssh = await self._get_ssh(instance_id)
        cmd_str = self._build_shell_command(command, workdir, env)

        process = await ssh.create_process(
            cmd_str,
            stdin=asyncssh.DEVNULL,
            stderr=asyncssh.STDOUT,
        )

        assert process.stdout is not None
        async for line in process.stdout:
            yield line
        await process.wait_closed()
        exit_code = int(process.exit_status or 0)
        if exit_code != 0:
            raise CommandExecutionError(exit_code)

    async def exec_command_wait(
        self,
        instance_id: str,
        command: list[str],
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str]:
        """Execute a command and wait for completion."""
        ssh = await self._get_ssh(instance_id)
        cmd_str = self._build_shell_command(command, workdir, env)

        result = await ssh.run(cmd_str, check=False)
        exit_code = result.exit_status or 0
        output = (result.stdout or "") + (result.stderr or "")
        return exit_code, output

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

    async def pty_resize(
        self, handle: PtyHandle, cols: int, rows: int
    ) -> None:
        """Resize the PTY terminal."""
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        process.change_terminal_size(cols, rows)
        handle.metadata["cols"] = cols
        handle.metadata["rows"] = rows

    async def pty_close(self, handle: PtyHandle) -> None:
        """Close the PTY SSH session."""
        process: asyncssh.SSHClientProcess = handle.handle  # type: ignore[assignment]
        try:
            process.stdin.write_eof()
            process.close()
            await process.wait_closed()
        except Exception:
            pass

    async def list_workspaces(self) -> list[RuntimeWorkspaceInfo]:
        """Discover all opencuria VM workspaces."""
        conn = self._libvirt_conn()
        result: list[RuntimeWorkspaceInfo] = []

        def _list() -> list[tuple[str, int]]:
            domains: list[tuple[str, int]] = []
            # Running domains
            for dom_id in conn.listDomainsID() or []:
                try:
                    dom = conn.lookupByID(dom_id)
                    domains.append((dom.name(), libvirt.VIR_DOMAIN_RUNNING))
                except libvirt.libvirtError:
                    pass
            # Defined but not running
            for name in conn.listDefinedDomains() or []:
                domains.append((name, libvirt.VIR_DOMAIN_SHUTOFF))
            return domains

        domains = await asyncio.to_thread(_list)

        for name, state in domains:
            if not name.startswith("opencuria-workspace-"):
                continue
            instance_id = name.replace("opencuria-workspace-", "")
            status_map = {
                libvirt.VIR_DOMAIN_RUNNING: "running",
                libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
            }
            result.append(
                RuntimeWorkspaceInfo(
                    workspace_id=instance_id,
                    instance_id=instance_id,
                    status=status_map.get(state, "unknown"),
                    name=name,
                )
            )

        return result

    async def get_workspace_usage(self, instance_id: str) -> dict[str, int] | None:
        """Return host-observed VM usage stats for a running workspace.

        Values are returned in bytes and nanoseconds so callers can derive
        percentages over time without executing commands inside the VM.
        """

        def _collect() -> dict[str, int] | None:
            domain = self._get_domain(instance_id)
            state, _ = domain.state()
            if state != libvirt.VIR_DOMAIN_RUNNING:
                return None

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
            disk_used_bytes = 0
            try:
                disk_capacity_bytes, disk_used_bytes, _ = domain.blockInfo("vda", 0)
            except libvirt.libvirtError:
                # Fallback for unusual bus names: read the first VM disk target.
                xml = domain.XMLDesc(0)
                root = ET.fromstring(xml)
                disk_target = root.find("./devices/disk[@device='disk']/target")
                target_dev = disk_target.get("dev") if disk_target is not None else None
                if target_dev:
                    disk_capacity_bytes, disk_used_bytes, _ = domain.blockInfo(target_dev, 0)

            return {
                "cpu_time_ns": cpu_time_ns,
                "vcpu_count": vcpu_count,
                "ram_used_bytes": max(used_memory_kib, 0) * 1024,
                "ram_total_bytes": max(total_memory_kib, 0) * 1024,
                "disk_used_bytes": max(int(disk_used_bytes), 0),
                "disk_total_bytes": max(int(disk_capacity_bytes), 0),
            }

        try:
            return await asyncio.to_thread(_collect)
        except libvirt.libvirtError:
            return None

    async def put_archive(
        self, instance_id: str, path: str, data: bytes
    ) -> None:
        """Upload a tar archive to the VM and extract it at the given path."""
        ssh = await self._get_ssh(instance_id)
        # Ensure target directory exists
        await ssh.run(f"mkdir -p '{path}'", check=True)
        # Upload and extract via stdin pipe
        # encoding=None is required to write raw bytes (binary mode)
        process = await ssh.create_process(
            f"tar xf - -C '{path}'",
            stdin=asyncssh.PIPE,
            encoding=None,
        )
        process.stdin.write(data)
        process.stdin.write_eof()
        await process.wait()

    async def build_image(
        self,
        *,
        base_distro: str,
        init_script: str,
        image_path: str,
        progress_callback=None,
    ) -> dict[str, str]:
        """Build a reusable QCOW2 base image by provisioning a temporary VM."""
        base_image = self._resolve_build_base_image(base_distro)
        instance_id = f"image-build-{uuid.uuid4()}"
        target_path = Path(image_path)
        self._ensure_host_directory(
            target_path.parent,
            writable_hint=(
                "sudo install -d -o $USER -g $USER -m 755 "
                f"'{target_path.parent}'"
            ),
        )

        if progress_callback is not None:
            await progress_callback(f"Using base image {base_image.name}")

        try:
            gateway, vm_ip = await self._create_workspace_network(instance_id)
            disk = await self._create_overlay_disk(
                instance_id,
                disk_size_gb=self._settings.qemu_disk_size_gb,
                base_image=str(base_image),
            )
            cloud_init_iso = await self._create_cloud_init_iso(
                instance_id,
                vm_ip=vm_ip,
                gateway=gateway,
            )
            domain_xml = _domain_xml(
                name=self._domain_name(instance_id),
                vcpus=self._settings.qemu_vcpus,
                memory_mb=self._settings.qemu_memory_mb,
                disk_path=str(disk),
                cloud_init_iso=str(cloud_init_iso),
                network=self._workspace_network_name(instance_id),
            )

            def _define_and_start() -> None:
                conn = self._libvirt_conn()
                dom = conn.defineXML(domain_xml)
                dom.create()

            await asyncio.to_thread(_define_and_start)
            await self._wait_for_ssh(instance_id)

            if progress_callback is not None:
                await progress_callback("Temporary build VM is ready")

            script_body = init_script.strip() or "#!/bin/bash\nset -euo pipefail\n"
            remote_script = "/tmp/opencuria-image-build.sh"
            archive = io.BytesIO()
            script_bytes = script_body.encode("utf-8")
            with tarfile.open(fileobj=archive, mode="w") as tar:
                info = tarfile.TarInfo(name="opencuria-image-build.sh")
                info.mode = 0o700
                info.size = len(script_bytes)
                tar.addfile(info, io.BytesIO(script_bytes))
            archive.seek(0)
            await self.put_archive(instance_id, "/tmp", archive.getvalue())
            await self._stream_ssh_process(
                instance_id,
                f"sudo -E bash {remote_script}",
                progress_callback,
            )

            await self.stop_workspace(instance_id)

            tmp_target = target_path.with_suffix(f"{target_path.suffix}.tmp")
            if tmp_target.exists():
                tmp_target.unlink()
            proc = await asyncio.create_subprocess_exec(
                "qemu-img",
                "convert",
                "-f",
                "qcow2",
                "-O",
                "qcow2",
                str(disk),
                str(tmp_target),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"Failed to finalize QEMU image: {stderr.decode()}")
            tmp_target.chmod(0o644)
            tmp_target.replace(target_path)

            if progress_callback is not None:
                await progress_callback(f"QEMU image finalized at {target_path}")

            return {
                "image_path": str(target_path),
                "size_bytes": str(target_path.stat().st_size),
            }
        finally:
            await self._cleanup_build_instance(instance_id)

    # ── Image artifact operations ─────────────────────────────────────

    async def create_image_artifact(
        self, instance_id: str, name: str
    ) -> ImageArtifactInfo:
        """Create an image artifact by copying the QCOW2 overlay."""
        disk = self._disk_path(instance_id)
        if not disk.exists():
            raise RuntimeError(
                f"Disk not found for instance {instance_id}"
            )

        snapshot_id = str(uuid.uuid4())
        snapshot_path = self._snapshot_dir / f"{snapshot_id}.qcow2"

        # For a running VM, we need to do a live snapshot
        domain = await asyncio.to_thread(self._get_domain, instance_id)
        state, _ = await asyncio.to_thread(domain.state)

        if state == libvirt.VIR_DOMAIN_RUNNING:
            # Create an external snapshot using qemu-img
            # First, flush disk buffers via guest agent if available
            try:
                await asyncio.to_thread(
                    domain.fsFreeze,
                )
            except libvirt.libvirtError:
                pass  # Guest agent may not be available

            # Copy the disk — use -U (force share) to read the image
            # while QEMU holds a write lock.  fsFreeze above ensures
            # filesystem consistency.
            proc = await asyncio.create_subprocess_exec(
                "qemu-img", "convert",
                "-U",
                "-f", "qcow2", "-O", "qcow2",
                str(disk), str(snapshot_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            # Thaw filesystem
            try:
                await asyncio.to_thread(domain.fsThaw)
            except libvirt.libvirtError:
                pass

            if proc.returncode != 0:
                raise RuntimeError(
                    f"Failed to create snapshot: {stderr.decode()}"
                )
        else:
            # VM is stopped — simple copy
            await asyncio.to_thread(shutil.copy2, str(disk), str(snapshot_path))

        # Write snapshot metadata
        meta_path = snapshot_path.with_suffix(".meta")
        size_bytes = snapshot_path.stat().st_size
        created_at = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(
            f"snapshot_id={snapshot_id}\n"
            f"name={name}\n"
            f"instance_id={instance_id}\n"
            f"created_at={created_at}\n"
            f"size_bytes={size_bytes}\n"
        )

        logger.info(
            "vm_snapshot_created",
            instance_id=instance_id,
            snapshot_id=snapshot_id,
            name=name,
            size_bytes=size_bytes,
        )

        return ImageArtifactInfo(
            artifact_id=snapshot_id,
            workspace_id=instance_id,
            name=name,
            created_at=datetime.fromisoformat(created_at),
            size_bytes=size_bytes,
        )

    async def delete_image_artifact(self, artifact_id: str) -> None:
        """Delete an image artifact and its metadata."""
        snapshot_path = self._snapshot_dir / f"{artifact_id}.qcow2"
        meta_path = snapshot_path.with_suffix(".meta")
        dependent_workspaces = await self._find_snapshot_dependents(snapshot_path)
        if dependent_workspaces:
            workspace_list = ", ".join(sorted(dependent_workspaces))
            raise RuntimeError(
                "Cannot delete image artifact because it is still used by "
                f"workspace disk(s): {workspace_list}"
            )
        if snapshot_path.exists():
            snapshot_path.unlink()
        if meta_path.exists():
            meta_path.unlink()
        logger.info("vm_snapshot_deleted", snapshot_id=artifact_id)

    async def list_image_artifacts(
        self, instance_id: str
    ) -> list[ImageArtifactInfo]:
        """List all image artifacts for a given workspace instance."""
        snapshots: list[ImageArtifactInfo] = []
        for meta_path in self._snapshot_dir.glob("*.meta"):
            meta = {}
            for line in meta_path.read_text().strip().splitlines():
                k, _, v = line.partition("=")
                meta[k.strip()] = v.strip()
            if meta.get("instance_id") == instance_id:
                snapshots.append(
                    ImageArtifactInfo(
                        artifact_id=meta["snapshot_id"],
                        workspace_id=instance_id,
                        name=meta.get("name", ""),
                        created_at=meta.get("created_at", ""),
                        size_bytes=int(meta.get("size_bytes", 0)),
                    )
                )
        return snapshots

    async def create_workspace_from_image_artifact(
        self,
        artifact_id: str,
        new_instance_id: str,
        *,
        qemu_vcpus: int | None = None,
        qemu_memory_mb: int | None = None,
        qemu_disk_size_gb: int | None = None,
    ) -> str:
        """Create a workspace from an image artifact.

        Creates a new QCOW2 overlay backed by the artifact image
        and starts a new VM.
        """
        snapshot_path = self._resolve_image_artifact_path(artifact_id)
        if not snapshot_path.exists():
            raise RuntimeError(
                f"Image artifact {artifact_id} not found"
            )

        # Create overlay backed by the snapshot
        new_disk = self._disk_path(new_instance_id)
        cmd = [
            "qemu-img", "create",
            "-f", "qcow2",
            "-F", "qcow2",
            "-b", str(snapshot_path),
            str(new_disk),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to create clone overlay: {stderr.decode()}"
            )

        # ── Dedicated isolated network for the clone ─────────────────
        gateway, vm_ip = await self._create_workspace_network(new_instance_id)

        # Create cloud-init ISO for the clone (with static network config)
        cloud_init_iso = await self._create_cloud_init_iso(
            new_instance_id, vm_ip=vm_ip, gateway=gateway
        )

        # Resize overlay if a larger target disk size is requested.
        target_disk_size_gb = qemu_disk_size_gb or self._settings.qemu_disk_size_gb
        resize_proc = await asyncio.create_subprocess_exec(
            "qemu-img",
            "resize",
            str(new_disk),
            f"{target_disk_size_gb}G",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, resize_stderr = await resize_proc.communicate()
        if resize_proc.returncode != 0:
            raise RuntimeError(f"Failed to resize clone overlay: {resize_stderr.decode()}")

        # Define and start the VM
        domain_xml = _domain_xml(
            name=self._domain_name(new_instance_id),
            vcpus=qemu_vcpus or self._settings.qemu_vcpus,
            memory_mb=qemu_memory_mb or self._settings.qemu_memory_mb,
            disk_path=str(new_disk),
            cloud_init_iso=str(cloud_init_iso),
            network=self._workspace_network_name(new_instance_id),
        )

        def _define_and_start() -> None:
            conn = self._libvirt_conn()
            dom = conn.defineXML(domain_xml)
            dom.create()

        await asyncio.to_thread(_define_and_start)
        await self._wait_for_ssh(new_instance_id)

        logger.info(
            "vm_workspace_cloned",
            snapshot_id=artifact_id,
            new_instance_id=new_instance_id,
        )
        return new_instance_id

    def _resolve_image_artifact_path(self, artifact_id: str) -> Path:
        """Resolve a clone source from either a snapshot id or a built image path."""
        artifact_path = Path(artifact_id)
        if artifact_path.is_absolute():
            return artifact_path
        return self._snapshot_dir / f"{artifact_id}.qcow2"

    async def _find_snapshot_dependents(self, snapshot_path: Path) -> list[str]:
        """Return workspace IDs whose QCOW2 overlays directly depend on a snapshot."""
        if not snapshot_path.exists():
            return []

        target = snapshot_path.resolve()
        dependents: list[str] = []
        for disk_path in self._disk_dir.glob("*.qcow2"):
            try:
                backing_path = await self._get_qcow2_backing_path(disk_path)
            except RuntimeError as exc:
                logger.warning(
                    "qcow2_backing_inspect_failed",
                    disk_path=str(disk_path),
                    error=str(exc),
                )
                continue
            if backing_path is None:
                continue
            if backing_path == target:
                dependents.append(disk_path.stem)
        return dependents

    async def _get_qcow2_backing_path(self, disk_path: Path) -> Path | None:
        """Read the fully resolved backing file path from a QCOW2 disk."""
        proc = await asyncio.create_subprocess_exec(
            "qemu-img",
            "info",
            "--output=json",
            str(disk_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"Failed to inspect QCOW2 backing file for {disk_path}: {stderr.decode()}"
            )

        info = json.loads(stdout.decode() or "{}")
        backing = info.get("full-backing-filename") or info.get("backing-filename")
        if not backing:
            return None
        return Path(backing).resolve()
