"""Free page reporting keeps guest RAM available without retaining host pages."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

libvirt = pytest.importorskip("libvirt")

from src.runtime import qemu_runtime
from src.runtime.qemu_runtime import (
    QemuRuntime,
    _domain_xml,
    _enable_free_page_reporting,
)


_WORKSPACE_ID = "memory-reclaim-test"
_DOMAIN_XML = """\
<domain type='kvm'>
  <name>opencuria-workspace-memory-reclaim-test</name>
  <uuid>00000000-0000-4000-8000-000000000001</uuid>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>4194304</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <metadata>
    <workspace xmlns='https://opencuria.dev/libvirt'>preserve-me</workspace>
  </metadata>
  <devices>
    <disk type='file' device='disk'>
      <source file='/tmp/memory-reclaim-test.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <memballoon model='virtio' autodeflate='on'>
      <stats period='10'/>
      <address type='pci' domain='0x0000' bus='0x07' slot='0x00' function='0x0'/>
    </memballoon>
  </devices>
</domain>
"""


@pytest.fixture
def runtime() -> tuple[QemuRuntime, MagicMock]:
    """Use real lifecycle methods without a running hypervisor or SSH server."""
    runtime = object.__new__(QemuRuntime)
    runtime._ssh_states = {}
    runtime._invalidate_ssh = AsyncMock()
    runtime._wait_for_ssh = AsyncMock()
    runtime._libvirt_conn = MagicMock()
    domain = runtime._libvirt_conn.return_value.lookupByName.return_value
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    domain.XMLDesc.return_value = _DOMAIN_XML
    return runtime, domain


def test_new_domains_report_free_pages_without_reducing_guest_memory() -> None:
    root = ET.fromstring(
        _domain_xml(
            name="opencuria-workspace-new",
            vcpus=4,
            memory_mb=8192,
            disk_path="/tmp/new.qcow2",
            cloud_init_iso="/tmp/new.iso",
            network="workspace-new",
        )
    )

    assert root.find("./devices/memballoon").attrib == {
        "model": "virtio",
        "freePageReporting": "on",
    }
    assert root.find("./memory").text == str(8192 * 1024)
    current = root.find("./currentMemory")
    assert current is None or current.text == str(8192 * 1024)


def test_upgrade_adds_a_missing_balloon_device() -> None:
    root = ET.fromstring("<domain><devices><console type='pty'/></devices></domain>")

    assert _enable_free_page_reporting(root) is True

    balloon = root.find("./devices/memballoon")
    assert balloon.attrib == {"model": "virtio", "freePageReporting": "on"}
    assert root.find("./devices/console").attrib == {"type": "pty"}


def test_upgrade_preserves_memory_cpu_metadata_and_balloon_address() -> None:
    root = ET.fromstring(_DOMAIN_XML)
    before = ET.fromstring(_DOMAIN_XML)

    assert _enable_free_page_reporting(root) is True

    balloon = root.find("./devices/memballoon")
    assert balloon.get("model") == "virtio"
    assert balloon.get("freePageReporting") == "on"
    assert balloon.get("autodeflate") == "on"
    for path in (
        "./name",
        "./uuid",
        "./memory",
        "./currentMemory",
        "./vcpu",
        "./metadata",
        "./devices/disk",
        "./devices/memballoon/stats",
        "./devices/memballoon/address",
    ):
        assert ET.tostring(root.find(path)) == ET.tostring(before.find(path))

    upgraded_xml = ET.tostring(root)
    assert _enable_free_page_reporting(root) is False
    assert ET.tostring(root) == upgraded_xml
    assert len(root.findall("./devices/memballoon")) == 1


def test_upgrade_enables_explicitly_disabled_reporting() -> None:
    root = ET.fromstring(_DOMAIN_XML)
    root.find("./devices/memballoon").set("freePageReporting", "off")

    assert _enable_free_page_reporting(root) is True
    assert root.find("./devices/memballoon").get("freePageReporting") == "on"


def test_existing_domain_upgrade_reads_and_updates_the_persistent_definition(
    runtime,
) -> None:
    instance, domain = runtime

    instance._ensure_memory_reclaim(domain)

    domain.XMLDesc.assert_called_once_with(libvirt.VIR_DOMAIN_XML_INACTIVE)
    definition = instance._libvirt_conn.return_value.defineXML.call_args.args[0]
    balloon = ET.fromstring(definition).find("./devices/memballoon")
    assert balloon.get("freePageReporting") == "on"
    domain.setMemory.assert_not_called()
    domain.setMemoryFlags.assert_not_called()
    domain.attachDeviceFlags.assert_not_called()


def test_existing_domain_upgrade_does_not_redefine_an_enabled_device(runtime) -> None:
    instance, domain = runtime
    root = ET.fromstring(_DOMAIN_XML)
    root.find("./devices/memballoon").set("freePageReporting", "on")
    domain.XMLDesc.return_value = ET.tostring(root, encoding="unicode")

    instance._ensure_memory_reclaim(domain)

    instance._libvirt_conn.return_value.defineXML.assert_not_called()


async def test_start_upgrades_existing_domain_before_boot(runtime) -> None:
    instance, domain = runtime
    events: list[str] = []
    instance._libvirt_conn.return_value.defineXML.side_effect = (
        lambda _xml: events.append("define")
    )
    domain.create.side_effect = lambda: events.append("create")

    await instance.start_workspace(_WORKSPACE_ID)

    assert events == ["define", "create"]
    instance._libvirt_conn.return_value.lookupByName.assert_called_once_with(
        f"opencuria-workspace-{_WORKSPACE_ID}"
    )
    instance._wait_for_ssh.assert_awaited_once_with(_WORKSPACE_ID)


async def test_start_of_running_domain_leaves_the_guest_undisturbed(runtime) -> None:
    instance, domain = runtime
    domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)

    await instance.start_workspace(_WORKSPACE_ID)

    domain.XMLDesc.assert_not_called()
    instance._libvirt_conn.return_value.defineXML.assert_not_called()
    domain.create.assert_not_called()
    domain.destroy.assert_not_called()
    instance._invalidate_ssh.assert_not_awaited()


async def test_failed_persistent_upgrade_prevents_boot(runtime) -> None:
    instance, domain = runtime
    instance._libvirt_conn.return_value.defineXML.side_effect = libvirt.libvirtError(
        "free page reporting unavailable"
    )

    with pytest.raises(libvirt.libvirtError, match="free page reporting unavailable"):
        await instance.start_workspace(_WORKSPACE_ID)

    domain.create.assert_not_called()
    instance._wait_for_ssh.assert_not_awaited()


@pytest.mark.parametrize("restart", [False, True])
async def test_reconfigure_preserves_reporting_while_applying_resources(
    runtime, monkeypatch: pytest.MonkeyPatch, restart: bool
) -> None:
    instance, domain = runtime
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b'{"virtual-size": 10737418240}', b"")),
    )
    monkeypatch.setattr(
        qemu_runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )

    await instance.reconfigure_workspace(
        _WORKSPACE_ID,
        qemu_vcpus=4,
        qemu_memory_mb=8192,
        qemu_disk_size_gb=10,
        restart=restart,
    )

    definition = instance._libvirt_conn.return_value.defineXML.call_args.args[0]
    root = ET.fromstring(definition)
    assert root.find("./vcpu").text == "4"
    assert root.find("./memory").text == str(8192 * 1024)
    assert root.find("./currentMemory").text == str(8192 * 1024)
    balloon = root.find("./devices/memballoon")
    assert balloon.get("freePageReporting") == "on"
    assert balloon.get("autodeflate") == "on"
    assert balloon.find("./address").get("bus") == "0x07"
    assert domain.create.call_count == int(restart)
    domain.setMemory.assert_not_called()
    domain.setMemoryFlags.assert_not_called()
