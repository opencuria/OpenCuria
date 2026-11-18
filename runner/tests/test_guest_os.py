"""Guest OS strategies: Linux keeps its definitions, Windows adds firmware and devices."""

from __future__ import annotations

import builtins
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

libvirt = pytest.importorskip("libvirt")

from src.runtime.diagnostics import (
    GENERALIZE_FAILED,
    IMAGE_INVALID,
    OperationError,
    OperationRejectedError,
)
from src.runtime.guest_os import (
    LINUX,
    METADATA_NS,
    WINDOWS,
    GuestBootstrapError,
    SeedSpec,
    guest_for,
    guest_for_distro,
    validate_guest_os,
)
from src.runtime.managed_process import GUEST_TOKEN_CODE
from src.runtime.qemu_runtime import QemuRuntime, _domain_xml, _start_with_fresh_nvram

_NS = f"{{{METADATA_NS}}}"


def _xml(
    guest=None, network: str | None = "net0", vcpus: int = 4, vdagent: bool = True
) -> ET.Element:
    return ET.fromstring(
        _domain_xml(
            name="opencuria-workspace-ws",
            vcpus=vcpus,
            memory_mb=4096,
            disk_path="/disks/ws.qcow2",
            cloud_init_iso="/disks/ws-cloud-init.iso",
            network=network,
            guest=guest,
            serial_log="/disks/ws-serial.log",
            vdagent=vdagent,
        )
    )


def test_linux_definition_is_unchanged_by_the_strategy() -> None:
    plain = _domain_xml(
        name="opencuria-workspace-ws",
        vcpus=2,
        memory_mb=2048,
        disk_path="/d.qcow2",
        cloud_init_iso="/s.iso",
        network="net0",
    )
    assert plain == _domain_xml(
        name="opencuria-workspace-ws",
        vcpus=2,
        memory_mb=2048,
        disk_path="/d.qcow2",
        cloud_init_iso="/s.iso",
        network="net0",
        guest=guest_for(LINUX),
        serial_log="/ignored.log",
    )
    root = ET.fromstring(plain)
    assert root.find("./os/firmware") is None
    assert root.find(f"./metadata/{_NS}workspace/{_NS}guest-os") is None
    assert root.find("./devices/graphics") is None


def test_windows_definition_meets_windows_11_requirements() -> None:
    root = _xml(guest_for(WINDOWS), vcpus=6)

    os_node = root.find("./os")
    assert os_node.get("firmware") == "efi"
    features = {f.get("name"): f.get("enabled") for f in os_node.findall("./firmware/feature")}
    assert features == {"secure-boot": "yes", "enrolled-keys": "yes"}
    assert root.find("./features/smm").get("state") == "on"
    assert root.find("./features/hyperv/synic").get("state") == "on"
    topology = root.find("./cpu/topology")
    assert topology.attrib == {"sockets": "1", "dies": "1", "cores": "6", "threads": "1"}
    assert root.find("./devices/tpm/backend").attrib == {
        "type": "emulator",
        "version": "2.0",
    }
    graphics = root.find("./devices/graphics")
    assert graphics.get("type") == "vnc"
    # No TCP listener: the runner reaches the display through libvirt only.
    assert graphics.find("./listen").get("type") == "none"
    assert root.find("./devices/input").attrib == {"type": "tablet", "bus": "usb"}
    assert (
        root.find("./devices/channel[@type='qemu-vdagent']/target").get("name")
        == "com.redhat.spice.0"
    )
    assert root.find("./devices/serial/log").get("file") == "/disks/ws-serial.log"
    # Memory usage comes from the balloon service, not the QEMU process size.
    assert root.find("./devices/memballoon/stats").get("period") == "10"
    assert root.find("./devices/memballoon").get("freePageReporting") == "on"
    assert root.findtext(f"./metadata/{_NS}workspace/{_NS}guest-os") == WINDOWS
    assert root.findtext(f"./metadata/{_NS}workspace/{_NS}workspace-id") == "ws"


def test_windows_definition_omits_vdagent_where_libvirt_lacks_it() -> None:
    with_agent = _xml(guest_for(WINDOWS))
    root = _xml(guest_for(WINDOWS), vdagent=False)

    assert root.find("./devices/channel[@type='qemu-vdagent']") is None
    # Everything else stays: pointer, display, TPM and Secure Boot.
    with_agent.find("./devices").remove(
        with_agent.find("./devices/channel[@type='qemu-vdagent']")
    )
    assert ET.tostring(root) == ET.tostring(with_agent)


@pytest.mark.parametrize(
    ("version", "resets"), [(8_000_000, False), (8_001_000, True), (10_000_000, True)]
)
def test_new_domains_reset_nvram_where_libvirt_supports_it(
    monkeypatch: pytest.MonkeyPatch, version: int, resets: bool
) -> None:
    monkeypatch.setattr(libvirt, "VIR_DOMAIN_START_RESET_NVRAM", 16, raising=False)
    conn, domain = MagicMock(), MagicMock()
    conn.getLibVersion.return_value = version

    _start_with_fresh_nvram(conn, domain)

    if resets:
        domain.createWithFlags.assert_called_once_with(16)
        domain.create.assert_not_called()
    else:
        domain.create.assert_called_once_with()
        domain.createWithFlags.assert_not_called()


def test_bindings_without_the_reset_flag_start_domains_plainly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(libvirt, "VIR_DOMAIN_START_RESET_NVRAM", raising=False)
    conn, domain = MagicMock(), MagicMock()
    conn.getLibVersion.return_value = 10_000_000

    _start_with_fresh_nvram(conn, domain)

    domain.create.assert_called_once_with()
    domain.createWithFlags.assert_not_called()


@pytest.mark.parametrize(
    ("version", "vdagent", "resets"),
    [(8_000_000, False, False), (8_001_000, False, True), (8_004_000, True, True)],
)
async def test_windows_guests_follow_the_host_libvirt(
    tmp_path, monkeypatch: pytest.MonkeyPatch, version: int, vdagent: bool, resets: bool
) -> None:
    monkeypatch.setattr(libvirt, "VIR_DOMAIN_START_RESET_NVRAM", 16, raising=False)
    runtime = _runtime(tmp_path)
    conn = MagicMock()
    conn.getLibVersion.return_value = version
    runtime._libvirt_conn = MagicMock(return_value=conn)
    runtime._create_workspace_network = AsyncMock(return_value=("10.0.0.1", "10.0.0.2"))
    runtime._create_cloud_init_iso = AsyncMock(return_value=tmp_path / "seed.iso")

    await runtime._define_and_start_guest(
        "ws", guest=guest_for(WINDOWS), disk=tmp_path / "ws.qcow2", vcpus=2,
        memory_mb=4096,
    )

    root = ET.fromstring(conn.defineXML.call_args.args[0])
    channel = root.find("./devices/channel[@type='qemu-vdagent']")
    assert (channel is not None) is vdagent
    assert root.find("./devices/tpm/backend").get("type") == "emulator"
    domain = conn.defineXML.return_value
    assert domain.createWithFlags.called is resets
    assert domain.create.called is not resets


def test_guests_without_network_have_no_interface() -> None:
    assert _xml(guest_for(WINDOWS), network=None).find("./devices/interface") is None
    assert _xml(network="net0").find("./devices/interface/source").get("network") == "net0"


def test_seed_files_follow_the_guest() -> None:
    spec = SeedSpec(bootstrap_id="id-1", public_key="ssh-ed25519 KEY", vm_ip="10.0.0.2",
                    gateway="10.0.0.1")
    linux = guest_for(LINUX).seed_files(spec)
    assert set(linux) == {"user-data", "meta-data", "network-config"}
    assert "instance-id: id-1" in linux["meta-data"]
    assert "ssh-ed25519 KEY" in linux["user-data"]

    windows = guest_for(WINDOWS).seed_files(
        SeedSpec(bootstrap_id="id-2", public_key="ssh-ed25519 KEY", action="generalize")
    )
    assert set(windows) == {"meta-data", "opencuria.json"}
    assert "instance-id: id-2" in windows["meta-data"]
    assert json.loads(windows["opencuria.json"]) == {
        "ssh_authorized_keys": ["ssh-ed25519 KEY"],
        "action": "generalize",
    }


def test_windows_first_boot_state() -> None:
    windows = guest_for(WINDOWS)
    assert windows.first_boot_state(0, "") == "pending"
    assert windows.first_boot_state(None, "") == "pending"
    assert windows.first_boot_state(0, '{"status": "starting"}') == "pending"
    assert windows.first_boot_state(0, '{"status": "ready"}') == "done"
    with pytest.raises(GuestBootstrapError):
        windows.first_boot_state(0, '{"status": "failed", "detail": "x"}')
    with pytest.raises(ValueError):
        windows.first_boot_state(0, "[]")


def test_linux_first_boot_state_keeps_cloud_init_semantics() -> None:
    linux = guest_for(LINUX)
    assert linux.first_boot_state(0, '{"status": "done"}') == "done"
    assert linux.first_boot_state(2, '{"status": "done"}') == "degraded"
    assert linux.first_boot_state(0, '{"status": "running"}') == "pending"
    with pytest.raises(GuestBootstrapError):
        linux.first_boot_state(None, '{"status": "done"}')


def test_guest_selection() -> None:
    assert guest_for(None).name == LINUX
    assert guest_for("unknown").name == LINUX
    assert guest_for_distro("ubuntu:24.04").name == LINUX
    assert guest_for_distro(" Windows:11-Enterprise ").name == WINDOWS
    assert validate_guest_os(None) == LINUX
    with pytest.raises(ValueError):
        validate_guest_os("macos")
    windows = guest_for(WINDOWS)
    assert windows.dhcp and windows.compress_publication and windows.generalize_on_publish
    assert "powershell.exe" in windows.build_script().command
    assert guest_for(LINUX).build_script().command == (
        "sudo -E bash /tmp/opencuria-image-build.sh"
    )


def _runtime(tmp_path: Path) -> QemuRuntime:
    runtime = object.__new__(QemuRuntime)
    runtime._settings = SimpleNamespace(
        qemu_generalize_timeout=5, qemu_build_vcpus=1, qemu_build_memory_mb=2048
    )
    runtime._disk_dir = tmp_path
    return runtime


def _manifest(image: Path, **fields) -> None:
    image.write_bytes(b"qcow2")
    image.with_suffix(".manifest.json").write_text(json.dumps(fields))


def test_image_guest_comes_from_the_manifest(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    linux, windows, broken = (tmp_path / f"{n}.qcow2" for n in ("l", "w", "b"))
    _manifest(linux)
    _manifest(windows, guest_os="windows")
    _manifest(broken, guest_os="macos")

    assert runtime._image_guest(linux).name == LINUX
    assert runtime._image_guest(tmp_path / "unpublished.qcow2").name == LINUX
    assert runtime._image_guest(windows, "windows").name == WINDOWS
    with pytest.raises(OperationRejectedError) as mismatch:
        runtime._image_guest(windows, "linux")
    assert mismatch.value.code == IMAGE_INVALID
    with pytest.raises(OperationRejectedError):
        runtime._image_guest(broken)


async def test_guest_is_read_from_domain_metadata_and_cached(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    domain = MagicMock()
    domain.XMLDesc.return_value = ET.tostring(_xml(guest_for(WINDOWS)), encoding="unicode")
    runtime._get_domain = MagicMock(return_value=domain)

    assert (await runtime._resolve_guest("ws")).name == WINDOWS
    assert (await runtime._resolve_guest("ws")).name == WINDOWS
    runtime._get_domain.assert_called_once_with("ws")


async def test_unreadable_domains_are_linux_but_not_cached(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._get_domain = MagicMock(side_effect=RuntimeError("libvirt down"))
    assert (await runtime._resolve_guest("ws")).name == LINUX
    assert "ws" not in runtime._guest_cache
    plain = MagicMock()
    plain.XMLDesc.return_value = ET.tostring(_xml(), encoding="unicode")
    runtime._get_domain = MagicMock(return_value=plain)
    assert (await runtime._resolve_guest("ws")).name == LINUX
    assert runtime._guest_cache["ws"] == LINUX


def _shutoff_domain() -> MagicMock:
    domain = MagicMock()
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    return domain


async def test_generalize_result_comes_from_the_serial_log(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._get_domain = MagicMock(return_value=_shutoff_domain())
    log = runtime._serial_log_path("copy")

    log.write_text("boot noise\r\nopencuria-bootstrap: generalizing\r\nOPENCURIA_GENERALIZE_OK\r\n")
    await runtime._await_generalized("copy")

    log.write_text("OPENCURIA_GENERALIZE_FAILED sysprep failed (0x8007): appx\r\n")
    with pytest.raises(OperationError) as failed:
        await runtime._await_generalized("copy")
    assert failed.value.code == GENERALIZE_FAILED
    assert "appx" in str(failed.value)

    log.write_text("")
    with pytest.raises(OperationError, match="no result reported"):
        await runtime._await_generalized("copy")


async def test_generalize_wait_is_bounded(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._settings.qemu_generalize_timeout = 0.01
    domain = MagicMock()
    domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
    runtime._get_domain = MagicMock(return_value=domain)
    with pytest.raises(OperationError) as timeout:
        await runtime._await_generalized("copy")
    assert timeout.value.code == GENERALIZE_FAILED
    domain.destroy.assert_called_once()


def test_serial_lines_strip_control_characters(tmp_path) -> None:
    log = tmp_path / "serial.log"
    log.write_bytes(b"\x1b[2Jopencuria-setup: installing MSYS2\r\nnoise\r\nopencuria-setup: done\r\n")
    assert QemuRuntime._serial_lines(log, "opencuria-setup: ") == ["installing MSYS2", "done"]
    assert QemuRuntime._serial_lines(tmp_path / "missing.log", "") == []


def _capture_runtime(tmp_path: Path, guest: str) -> QemuRuntime:
    runtime = _runtime(tmp_path)
    runtime._snapshot_dir = tmp_path
    runtime._managed_image_path = lambda reference: tmp_path / f"{reference}.qcow2"
    runtime._guest_cache["ws"] = guest
    published = {"created_at": "2026-11-15T00:00:00+00:00", "size_bytes": 7,
                 "min_disk_size_gb": 64, "virtual_size_bytes": 64 * 1024**3}
    runtime._publish_capture = AsyncMock(return_value=published)
    runtime._prepare_capture_copy = AsyncMock(return_value=tmp_path / "copy.qcow2")
    runtime._await_generalized = AsyncMock()
    runtime._cleanup_build_instance = AsyncMock()
    return runtime


_ARTIFACT = "11111111-1111-4111-8111-111111111111"
_OPERATION = "22222222-2222-4222-8222-222222222222"


async def test_windows_capture_generalizes_a_copy_and_keeps_the_source(tmp_path) -> None:
    runtime = _capture_runtime(tmp_path, WINDOWS)
    await runtime.create_image_artifact(
        "ws", "Captured", artifact_id=_ARTIFACT, operation_id=_OPERATION,
        credential_clean=True,
    )
    copy_id = f"image-capture-{_OPERATION}"
    runtime._prepare_capture_copy.assert_awaited_once()
    assert runtime._prepare_capture_copy.await_args.args[:2] == ("ws", copy_id)
    runtime._await_generalized.assert_awaited_once_with(copy_id)
    source, disk, _, manifest = runtime._publish_capture.await_args.args
    assert (source, disk) == ("ws", tmp_path / "copy.qcow2")
    assert manifest["guest_os"] == WINDOWS and manifest["kind"] == "capture"
    runtime._cleanup_build_instance.assert_awaited_once_with(copy_id)


async def test_failed_generalize_removes_the_copy_and_publishes_nothing(tmp_path) -> None:
    runtime = _capture_runtime(tmp_path, WINDOWS)
    runtime._await_generalized.side_effect = OperationError(GENERALIZE_FAILED, "x")
    with pytest.raises(OperationError):
        await runtime.create_image_artifact(
            "ws", "Captured", artifact_id=_ARTIFACT, operation_id=_OPERATION,
            credential_clean=True,
        )
    runtime._publish_capture.assert_not_awaited()
    runtime._cleanup_build_instance.assert_awaited_once_with(f"image-capture-{_OPERATION}")


async def test_linux_capture_publishes_the_source_disk_unchanged(tmp_path) -> None:
    runtime = _capture_runtime(tmp_path, LINUX)
    await runtime.create_image_artifact(
        "ws", "Captured", artifact_id=_ARTIFACT, operation_id=_OPERATION,
        credential_clean=True,
    )
    runtime._prepare_capture_copy.assert_not_awaited()
    source, disk, _, manifest = runtime._publish_capture.await_args.args
    assert (source, disk) == ("ws", runtime._disk_path("ws"))
    assert "guest_os" not in manifest


async def test_capture_still_requires_scrub_proof(tmp_path) -> None:
    runtime = _capture_runtime(tmp_path, WINDOWS)
    with pytest.raises(RuntimeError, match="scrub proof"):
        await runtime.create_image_artifact(
            "ws", "Captured", artifact_id=_ARTIFACT, operation_id=_OPERATION
        )
    runtime._prepare_capture_copy.assert_not_awaited()


async def test_reconfigure_keeps_the_cpu_topology_in_sync(monkeypatch) -> None:
    from src.runtime import qemu_runtime

    runtime = object.__new__(QemuRuntime)
    runtime._libvirt_conn = MagicMock()
    domain = runtime._libvirt_conn.return_value.lookupByName.return_value
    domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
    domain.XMLDesc.return_value = ET.tostring(
        _xml(guest_for(WINDOWS), vcpus=2), encoding="unicode"
    )
    process = SimpleNamespace(
        returncode=0,
        communicate=AsyncMock(return_value=(b'{"virtual-size": 68719476736}', b"")),
    )
    monkeypatch.setattr(
        qemu_runtime.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )

    await runtime._reconfigure_workspace(
        "ws", qemu_vcpus=6, qemu_memory_mb=8192, qemu_disk_size_gb=64
    )

    root = ET.fromstring(runtime._libvirt_conn.return_value.defineXML.call_args.args[0])
    assert root.find("./vcpu").text == "6"
    assert root.find("./cpu/topology").attrib == {
        "sockets": "1", "dies": "1", "cores": "6", "threads": "1",
    }
    # Firmware, TPM and the display survive a resource change.
    assert root.find("./os").get("firmware") == "efi"
    assert root.find("./devices/tpm") is not None
    assert root.find("./devices/graphics/listen").get("type") == "none"


def _token_namespace(files: dict[str, bytes]) -> dict:
    def fake_open(path, mode="r", *args, **kwargs):
        if path not in files:
            raise FileNotFoundError(path)
        data = files[path]
        return io.BytesIO(data) if "b" in mode else io.StringIO(data.decode())

    namespace = {"open": fake_open, "__builtins__": builtins}
    exec(GUEST_TOKEN_CODE, namespace)
    return namespace


def test_windows_guest_token_combines_instance_and_boot_counter() -> None:
    key = (
        "/proc/registry/HKEY_LOCAL_MACHINE/SYSTEM/CurrentControlSet/Control/"
        "Session Manager/Memory Management/PrefetchParameters/BootId"
    )
    namespace = _token_namespace(
        {key: (7).to_bytes(4, "little"), "/c/ProgramData/opencuria/instance-id": b"seed-1\r\n"}
    )
    assert namespace["guest_token"]() == {
        "boot_id": "windows-seed-1-7",
        "init_starttime": "7",
    }


def test_linux_guest_token_is_unchanged() -> None:
    namespace = _token_namespace(
        {
            "/proc/sys/kernel/random/boot_id": b"boot\n",
            "/proc/1/stat": b"1 (systemd) S 0 1 1 0 -1 4194560 1 2 3 4 5 6 7 8 20 0 1 0 42 1",
        }
    )
    assert namespace["guest_token"]() == {"boot_id": "boot", "init_starttime": "42"}


def _upload_ssh():
    """An SSH connection whose processes record their command and stdin."""
    calls = []

    def create_process(command, **kwargs):
        process = MagicMock()
        process.exit_status = 0
        process.wait_closed = AsyncMock()
        written = bytearray()
        process.stdin.write.side_effect = written.extend
        calls.append((command, written))
        return process

    ssh = MagicMock()
    ssh.create_process = AsyncMock(side_effect=create_process)
    return ssh, calls


def _decoded(wrapped: str) -> str:
    import base64

    prefix, rest = wrapped.split("printf %s ", 1)
    assert prefix == "bash <(" and rest.endswith("|base64 -d)")
    return base64.b64decode(rest[: -len("|base64 -d)")]).decode()


async def test_windows_commands_travel_as_base64_with_gnu_tools_first(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._guest_cache["ws"] = WINDOWS
    wrapped = await runtime._guest_command("ws", "find /workspace -name '*.py' \"x\"")
    assert set(wrapped) <= set("bash <(printf%s|64-d)") | set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    )
    assert _decoded(wrapped) == (
        'export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"\n'
        "find /workspace -name '*.py' \"x\"\n"
    )
    runtime._guest_cache["linux-ws"] = LINUX
    assert await runtime._guest_command("linux-ws", "find /x") == "find /x"


async def test_oversized_windows_commands_are_uploaded_first(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._guest_cache["ws"] = WINDOWS
    ssh, calls = _upload_ssh()
    runtime._get_ssh = AsyncMock(return_value=ssh)
    command = "python3 -c '" + "x" * 9000 + "'"

    path = await runtime._guest_command("ws", command)

    assert path.startswith("/tmp/opencuria-cmd-") and " " not in path
    ((upload, stdin),) = calls
    assert path in _decoded(upload)
    script = bytes(stdin).decode()
    assert script.startswith('#!/bin/bash\nrm -f "$0"\n')
    assert command in script


async def test_windows_terminals_start_through_a_single_token_launcher(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._guest_cache["ws"] = WINDOWS
    ssh, calls = _upload_ssh()
    runtime._get_ssh = AsyncMock(return_value=ssh)

    await runtime.exec_pty("ws", 100, 30, command=["/bin/bash", "-lc", "echo 'a b'"])

    (_, script), (launcher, _) = calls
    assert launcher.startswith("/tmp/opencuria-cmd-") and " " not in launcher
    text = bytes(script).decode()
    assert text.startswith('#!/bin/bash\nrm -f "$0"\n')
    assert "'/bin/bash' '-lc' 'echo '\\''a b'\\'''" in text


async def test_linux_terminals_keep_the_inline_command(tmp_path) -> None:
    runtime = _runtime(tmp_path)
    runtime._guest_cache["ws"] = LINUX
    ssh = MagicMock()
    ssh.create_process = AsyncMock(return_value=MagicMock())
    runtime._get_ssh = AsyncMock(return_value=ssh)
    runtime.exec_with_input = AsyncMock()

    await runtime.exec_pty("ws", 80, 24)

    assert "'/bin/bash' '-l'" in ssh.create_process.await_args.args[0]
    runtime.exec_with_input.assert_not_awaited()


async def test_clipboard_text_travels_on_stdin_to_the_session_task(tmp_path) -> None:
    import base64 as b64

    runtime = _runtime(tmp_path)
    runtime._guest_cache["ws"] = WINDOWS
    answer = json.dumps({"ok": True, "text": b64.b64encode("zurück\r\nok".encode()).decode()})
    runtime.exec_with_input = AsyncMock(return_value=(0, answer + "\n", ""))

    text = await runtime.guest_clipboard("ws", "write", "geheim 🧪")

    command, stdin = runtime.exec_with_input.await_args.args[1:]
    assert "geheim" not in command[-1] and "OpenCuria Clipboard" in command[-1]
    request = json.loads(stdin)
    assert request["action"] == "write"
    assert b64.b64decode(request["text"]).decode() == "geheim 🧪"
    assert text == "zurück\nok"

    runtime.exec_with_input = AsyncMock(return_value=(0, '{"ok": false, "error": "x"}', ""))
    with pytest.raises(RuntimeError):
        await runtime.guest_clipboard("ws", "read")
    with pytest.raises(ValueError):
        await runtime.guest_clipboard("ws", "paste")
