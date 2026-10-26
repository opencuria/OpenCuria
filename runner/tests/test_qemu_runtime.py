import asyncio
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import libvirt

from src.runtime.base import WorkspaceConfig
from src.runtime.qemu_runtime import QemuRuntime


class QemuRuntimeCloudInitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = object.__new__(QemuRuntime)
        self.runtime._disk_dir = Path(self.directory.name)
        self.runtime._ssh_states = {}
        self.runtime._get_ssh_public_key = MagicMock(
            return_value="ssh-ed25519 test-runner-key"
        )
        self.workspace_id = str(uuid.uuid4())

    async def test_recreation_refreshes_identity_even_when_network_is_unchanged(
        self,
    ) -> None:
        """Reset must invalidate both legacy and newly captured cloud-init caches."""
        seeds: list[tuple[str, str]] = []

        async def create_iso(*args: str, **kwargs: object) -> SimpleNamespace:
            files = {Path(arg).name: Path(arg) for arg in args[-3:]}
            seeds.append(
                (files["meta-data"].read_text(), files["network-config"].read_text())
            )
            Path(args[2]).touch()
            return SimpleNamespace(
                returncode=0, communicate=AsyncMock(return_value=(b"", b""))
            )

        with patch("src.runtime.qemu_runtime.shutil.which", return_value="tool"), patch(
            "src.runtime.qemu_runtime.asyncio.create_subprocess_exec",
            side_effect=create_iso,
        ):
            for ip, gateway in (
                ("10.100.0.14", "10.100.0.13"),
                ("10.100.0.2", "10.100.0.1"),
                ("10.100.0.2", "10.100.0.1"),
            ):
                iso = await self.runtime._create_cloud_init_iso(
                    self.workspace_id, vm_ip=ip, gateway=gateway
                )
                self.assertEqual(
                    iso, self.runtime._cloud_init_iso_path(self.workspace_id)
                )
                self.assertEqual(iso.stat().st_mode & 0o777, 0o644)
                self.assertIn(f"{ip}/30", seeds[-1][1])
                self.assertIn(f"via: {gateway}", seeds[-1][1])

        identities = [
            metadata.splitlines()[0].split(": ", 1)[1] for metadata, _ in seeds
        ]
        self.assertEqual(len(set(identities + [self.workspace_id])), 4)
        for identity in identities:
            self.assertEqual(uuid.UUID(identity).version, 4)

    async def test_seed_raises_the_ssh_session_cap_before_sshd_starts(
        self,
    ) -> None:
        """All runner channels share one SSH connection; 10 sessions is too few.

        runcmd restarts sshd only after the runner's connection is open, and
        that connection keeps its boot-time limits, so the cap must be written
        by write_files, which runs before sshd starts.
        """
        seeds: list[str] = []

        async def create_iso(*args: str, **kwargs: object) -> SimpleNamespace:
            files = {Path(arg).name: Path(arg) for arg in args[-3:]}
            seeds.append(files["user-data"].read_text())
            Path(args[2]).touch()
            return SimpleNamespace(
                returncode=0, communicate=AsyncMock(return_value=(b"", b""))
            )

        with patch("src.runtime.qemu_runtime.shutil.which", return_value="tool"), patch(
            "src.runtime.qemu_runtime.asyncio.create_subprocess_exec",
            side_effect=create_iso,
        ):
            await self.runtime._create_cloud_init_iso(
                self.workspace_id, vm_ip="10.100.0.2", gateway="10.100.0.1"
            )

        write_files, runcmd = seeds[0].split("write_files:", 1)[1].split("runcmd:", 1)
        caps = [
            int(line.split()[1])
            for entry in write_files.split("\n  - path: ")[1:]
            if entry.startswith("/etc/ssh/sshd_config.d/")
            for line in entry.splitlines()
            if line.strip().startswith("MaxSessions ")
        ]
        self.assertEqual(len(caps), 1)
        self.assertGreaterEqual(caps[0], 64)
        self.assertNotIn("MaxSessions", runcmd)

    async def test_start_reuses_the_existing_seed(self) -> None:
        """A normal start must not rerun first-boot provisioning."""
        iso = self.runtime._cloud_init_iso_path(self.workspace_id)
        iso.write_bytes(b"existing cloud-init seed")
        domain = MagicMock()
        domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
        self.runtime._get_domain = MagicMock(return_value=domain)
        self.runtime._wait_for_ssh = AsyncMock()
        self.runtime._create_cloud_init_iso = AsyncMock(
            side_effect=AssertionError("Start must reuse the existing seed")
        )

        await self.runtime.start_workspace(self.workspace_id)

        domain.create.assert_called_once_with()
        self.runtime._wait_for_ssh.assert_awaited_once_with(self.workspace_id)
        self.assertEqual(iso.read_bytes(), b"existing cloud-init seed")

    async def test_seed_failure_prevents_booting_an_unconfigured_vm(self) -> None:
        """An ISO failure must stop creation before defining or starting a VM."""
        self.runtime._settings = SimpleNamespace(
            qemu_vcpus=2, qemu_memory_mb=4096, qemu_disk_size_gb=50
        )
        self.runtime._create_workspace_network = AsyncMock(
            return_value=("10.100.0.1", "10.100.0.2")
        )
        self.runtime._create_overlay_disk = AsyncMock(
            return_value=self.runtime._disk_path(self.workspace_id)
        )
        self.runtime._libvirt_conn = MagicMock()
        self.runtime._wait_for_ssh = AsyncMock()
        process = SimpleNamespace(
            returncode=1,
            communicate=AsyncMock(return_value=(b"", b"ISO creation failed")),
        )

        with patch("src.runtime.qemu_runtime.shutil.which", return_value="tool"), patch(
            "src.runtime.qemu_runtime.asyncio.create_subprocess_exec",
            return_value=process,
        ), self.assertRaisesRegex(RuntimeError, "ISO creation failed"):
            await self.runtime.create_workspace(
                WorkspaceConfig(workspace_id=self.workspace_id, image="base.qcow2")
            )

        self.runtime._libvirt_conn.assert_not_called()
        self.runtime._wait_for_ssh.assert_not_awaited()

    @unittest.skipUnless(
        shutil.which("genisoimage") and shutil.which("isoinfo"),
        "requires genisoimage and isoinfo",
    )
    async def test_real_iso_contains_the_new_identity_and_network(self) -> None:
        """Verify the generated NoCloud ISO, including the permissions QEMU needs."""
        identities: list[str] = []
        for _ in range(2):
            iso = await self.runtime._create_cloud_init_iso(
                self.workspace_id, vm_ip="10.100.0.2", gateway="10.100.0.1"
            )
            for name in ("meta-data", "network-config", "user-data"):
                process = await asyncio.create_subprocess_exec(
                    "isoinfo", "-R", "-i", str(iso), "-x", f"/{name}",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
                self.assertEqual(process.returncode, 0, stderr.decode())
                text = stdout.decode()
                if name == "meta-data":
                    identity = text.splitlines()[0].split(": ", 1)[1]
                    self.assertNotEqual(identity, self.workspace_id)
                    self.assertIn("local-hostname: opencuria-workspace", text)
                    identities.append(identity)
                elif name == "network-config":
                    self.assertIn("10.100.0.2/30", text)
                    self.assertIn("via: 10.100.0.1", text)
                else:
                    self.assertIn("ssh-ed25519 test-runner-key", text)
            self.assertEqual(iso.stat().st_mode & 0o777, 0o644)
            work_dir = self.runtime._disk_dir / f"{self.workspace_id}-cloud-init"
            self.assertFalse(work_dir.exists())
        self.assertNotEqual(identities[0], identities[1])


class QemuRuntimeHostDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = object.__new__(QemuRuntime)

    def test_ensure_host_directory_creates_world_traversable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nested" / "dir"

            self.runtime._ensure_host_directory(target)

            self.assertTrue(target.is_dir())
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_ensure_host_directory_wraps_permission_error_with_fix_hint(self) -> None:
        target = Path("/var/lib/opencuria/base-images")

        with patch.object(
            Path,
            "mkdir",
            side_effect=PermissionError("permission denied"),
        ), self.assertRaises(RuntimeError) as ctx:
            self.runtime._ensure_host_directory(
                target,
                writable_hint="sudo install -d -o $USER -g $USER -m 755 '/var/lib/opencuria/base-images'",
            )

        self.assertIn(str(target), str(ctx.exception))
        self.assertIn("sudo install -d -o $USER -g $USER -m 755", str(ctx.exception))


class QemuRuntimeBuildBaseImageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = object.__new__(QemuRuntime)

    def test_resolve_build_base_image_uses_legacy_ubuntu_2204_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path("/var/lib/opencuria/images/ubuntu-base.img")
            self.runtime._settings = SimpleNamespace(qemu_image_cache_dir=tmpdir)

            with patch("src.runtime.qemu_runtime.Path") as mock_path:
                real_path = Path
                mock_path.side_effect = lambda *args, **kwargs: real_path(
                    *args, **kwargs
                )
                mock_path.return_value = legacy
                with patch.object(
                    self.runtime,
                    "_ensure_ubuntu_cloud_image",
                    side_effect=AssertionError("should not download 22.04"),
                ), patch.object(
                    real_path, "exists", autospec=True
                ) as exists_mock:
                    exists_mock.side_effect = lambda path_obj: (
                        str(path_obj) == str(legacy)
                    )
                    resolved = self.runtime._resolve_build_base_image(
                        "ubuntu:22.04"
                    )

            self.assertEqual(resolved, legacy)

    def test_resolve_build_base_image_downloads_release_specific_ubuntu_image(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.runtime._settings = SimpleNamespace(qemu_image_cache_dir=tmpdir)

            expected = Path(tmpdir) / "ubuntu-24.04-server-cloudimg-amd64.img"
            with patch.object(
                self.runtime,
                "_ensure_ubuntu_cloud_image",
                return_value=expected,
            ) as ensure_mock:
                resolved = self.runtime._resolve_build_base_image("ubuntu:24.04")

            self.assertEqual(resolved, expected)
            ensure_mock.assert_called_once_with("24.04")

    def test_resolve_build_base_image_rejects_non_ubuntu_distros(self) -> None:
        self.runtime._settings = SimpleNamespace(qemu_image_cache_dir="/tmp")

        with self.assertRaises(RuntimeError) as ctx:
            self.runtime._resolve_build_base_image("debian:12")

        self.assertIn("Unsupported QEMU base distro", str(ctx.exception))

    def test_ensure_ubuntu_cloud_image_downloads_into_runner_image_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            images_dir = Path(tmpdir) / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            self.runtime._settings = SimpleNamespace(
                qemu_image_cache_dir=str(images_dir)
            )

            response = MagicMock()
            response.read.side_effect = [b"chunk-1", b"chunk-2", b""]
            response.__enter__.return_value = response
            response.__exit__.return_value = False

            with (
                patch.object(
                self.runtime, "_ensure_host_directory"
            ) as ensure_dir_mock, patch(
                    "src.runtime.qemu_runtime.urllib.request.urlopen",
                    return_value=response,
                ) as urlopen_mock,
                patch(
                    "src.runtime.qemu_runtime.subprocess.run",
                    return_value=SimpleNamespace(stdout=b'{"format": "qcow2"}'),
                ),
            ):
                target = self.runtime._ensure_ubuntu_cloud_image("24.04")

            self.assertEqual(
                target, images_dir / "ubuntu-24.04-server-cloudimg-amd64.img"
            )
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"chunk-1chunk-2")
            ensure_dir_mock.assert_called_once()
            urlopen_mock.assert_called_once_with(
                "https://cloud-images.ubuntu.com/releases/server/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img",
                timeout=300,
            )


class QemuRuntimeArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_put_archive_quotes_external_directory(self) -> None:
        """A guest path with quotes cannot inject shell commands."""
        runtime = object.__new__(QemuRuntime)
        ssh = MagicMock()
        ssh.run = AsyncMock()
        process = MagicMock()
        process.wait = AsyncMock()
        ssh.create_process = AsyncMock(return_value=process)
        runtime._get_ssh = AsyncMock(return_value=ssh)
        path = "/tmp/with'quote"

        await runtime.put_archive("instance-1", path, b"archive")

        ssh.run.assert_awaited_once_with("mkdir -p '/tmp/with'\"'\"'quote'", check=True)
        self.assertEqual(
            ssh.create_process.await_args.args[0],
            "tar xf - -C '/tmp/with'\"'\"'quote'",
        )
        process.stdin.write.assert_called_once_with(b"archive")
        process.wait.assert_awaited_once()


class QemuRuntimeBuildImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_image_runs_init_script_with_sudo(self) -> None:
        runtime = object.__new__(QemuRuntime)
        runtime._settings = SimpleNamespace(
            qemu_disk_size_gb=20,
            qemu_vcpus=2,
            qemu_memory_mb=4096,
        )
        runtime._ensure_host_directory = MagicMock()
        runtime._resolve_build_base_image = MagicMock(
            return_value=Path("/tmp/ubuntu-24.04-server-cloudimg-amd64.img")
        )
        runtime._create_workspace_network = AsyncMock(
            return_value=("10.0.0.1", "10.0.0.2")
        )
        runtime._create_overlay_disk = AsyncMock(return_value=Path("/tmp/build.qcow2"))
        runtime._create_cloud_init_iso = AsyncMock(return_value=Path("/tmp/build.iso"))
        runtime._wait_for_ssh = AsyncMock()
        runtime.put_archive = AsyncMock()
        runtime._stream_ssh_process = AsyncMock()
        runtime.stop_workspace = AsyncMock()
        runtime._cleanup_build_instance = AsyncMock()
        runtime._libvirt_conn = MagicMock()

        with tempfile.TemporaryDirectory() as tmpdir:
            target_path = Path(tmpdir) / "image.qcow2"
            runtime._snapshot_dir = Path(tmpdir)

            async def publish(disk, target, manifest):
                target.write_bytes(b"qcow2")

            runtime._publish_image = AsyncMock(side_effect=publish)

            process = AsyncMock()
            process.communicate.return_value = (b"", b"")
            process.returncode = 0

            async def _create_subprocess_exec(*args, **kwargs):
                tmp_target = target_path.with_suffix(".qcow2.tmp")
                tmp_target.write_bytes(b"qcow2")
                return process

            with patch(
                "src.runtime.qemu_runtime._domain_xml", return_value="<domain/>"
            ), patch(
                "src.runtime.qemu_runtime.asyncio.to_thread", new=AsyncMock()
            ), patch(
                "src.runtime.qemu_runtime.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=_create_subprocess_exec),
            ):
                result = await runtime.build_image(
                    base_distro="ubuntu:24.04",
                    init_script="#!/bin/bash\napt-get update\n",
                    image_path=str(target_path),
                    operation_id="op",
                    image_instance_id="image",
                )

        runtime._stream_ssh_process.assert_awaited_once_with(
            unittest.mock.ANY,
            "sudo -E bash /tmp/opencuria-image-build.sh",
            None,
        )
        self.assertEqual(result["image_path"], str(target_path))


class QemuRuntimeAgentStdinTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_bootstrap_frame_keeps_stdin_open_for_sdk_messages(self) -> None:
        from src.runtime.managed_process import isolated_agent_env_preamble

        runtime = object.__new__(QemuRuntime)
        stdin = MagicMock()
        stdin.drain = AsyncMock()
        process = SimpleNamespace(stdin=stdin, close=MagicMock())
        ssh = SimpleNamespace(create_process=AsyncMock(return_value=process))
        runtime._get_ssh = AsyncMock(return_value=ssh)
        runtime._stream_pidfile = MagicMock(return_value="/tmp/stream.pid")
        env = {
            "ANTHROPIC_API_KEY": "test-secret",
            "CLAUDE_CONFIG_DIR": "/workspace/.opencuria/harness/claude/12345678-1234-1234-1234-123456789abc/config",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "0.2.174",
            "CLAUDE_CODE_SDK_READS_SESSION_STATE": "1",
            "DISABLE_UPDATES": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_FORK_SUBAGENT": "0",
        }

        await runtime.spawn_process(
            "instance",
            ["/usr/bin/claude", "--input-format", "stream-json"],
            workdir="/workspace",
            env=env,
            control_path="/var/lib/opencuria/streams/test",
            expected_token={"boot_id": "boot", "init_starttime": "1"},
            isolated_env=True,
            isolated_home="/tmp/opencuria-agent-home-0123456789abcdef0123456789abcdef",
        )

        stdin.write.assert_called_once_with(isolated_agent_env_preamble(env))
        stdin.drain.assert_awaited_once()
        stdin.write_eof.assert_not_called()


class QemuRuntimeUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = object.__new__(QemuRuntime)

    async def test_usage_uses_allocated_overlay_bytes_and_running_domain_capacity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            disk_path = Path(tmpdir) / "workspace.qcow2"
            with disk_path.open("wb") as disk:
                disk.truncate(64 * 1024 * 1024)
                disk.seek(1024 * 1024)
                disk.write(b"allocated")
            disk_stat = disk_path.stat()
            expected_used = disk_stat.st_blocks * 512
            self.assertNotEqual(disk_stat.st_size, expected_used)
            self.assertNotEqual(expected_used, 987654321)

            self.runtime._disk_path = MagicMock(return_value=disk_path)
            domain = MagicMock()
            domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
            domain.info.return_value = (1, 4096, 3072, 4, 123456)
            domain.memoryStats.return_value = {
                "actual": 4096,
                "rss": 3000,
                "unused": 1024,
            }
            domain.blockInfo.return_value = (128 * 1024**3, 987654321, 0)
            self.runtime._get_domain = MagicMock(return_value=domain)
            self.runtime.exec_command_wait = AsyncMock(
                return_value=(
                    0,
                    (
                        "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                        "/dev/vda1 20000 15000 4000 79% /\n"
                    ),
                )
            )

            usage = await self.runtime.get_workspace_usage("workspace")

        self.assertEqual(
            usage,
            {
                "cpu_time_ns": 123456,
                "vcpu_count": 4,
                "ram_used_bytes": 3 * 1024 * 1024,
                "ram_total_bytes": 4 * 1024 * 1024,
                "disk_used_bytes": expected_used,
                "disk_total_bytes": 128 * 1024**3,
                "guest_disk_total_bytes": 20000 * 1024,
                "guest_disk_used_bytes": 15000 * 1024,
                "guest_disk_free_bytes": 4000 * 1024,
            },
        )
        domain.blockInfo.assert_called_once_with("vda", 0)
        self.runtime.exec_command_wait.assert_awaited_once_with(
            "workspace", ["df", "-Pk", "/workspace"]
        )

    async def test_usage_omits_guest_disk_when_guest_probe_fails(self) -> None:
        async def slow_df(*args, **kwargs):
            await asyncio.sleep(1)
            return 0, ""

        failures = {
            "ssh_error": AsyncMock(side_effect=RuntimeError("SSH reconnect failed")),
            "non_zero_exit": AsyncMock(return_value=(1, "df: /workspace: missing")),
            "unparseable": AsyncMock(return_value=(0, "no df output")),
            "timeout": AsyncMock(side_effect=slow_df),
        }
        for name, exec_mock in failures.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmpdir:
                disk_path = Path(tmpdir) / "workspace.qcow2"
                disk_path.write_bytes(b"overlay")
                self.runtime._disk_path = MagicMock(return_value=disk_path)
                domain = MagicMock()
                domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
                domain.info.return_value = (1, 2048, 1024, 2, 50)
                domain.memoryStats.return_value = {"actual": 2048, "rss": 1024}
                domain.blockInfo.return_value = (16 * 1024**3, 0, 0)
                self.runtime._get_domain = MagicMock(return_value=domain)
                self.runtime.exec_command_wait = exec_mock

                with patch(
                    "src.runtime.qemu_runtime._GUEST_DISK_USAGE_TIMEOUT", 0.01
                ):
                    usage = await self.runtime.get_workspace_usage("workspace")

                self.assertEqual(usage["disk_total_bytes"], 16 * 1024**3)
                self.assertNotIn("guest_disk_used_bytes", usage)

    async def test_usage_returns_host_disk_usage_for_stopped_domain(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            disk_path = Path(tmpdir) / "workspace.qcow2"
            disk_path.write_bytes(b"overlay content")
            expected_used = disk_path.stat().st_blocks * 512
            self.runtime._disk_path = MagicMock(return_value=disk_path)
            domain = MagicMock()
            domain.state.return_value = (libvirt.VIR_DOMAIN_SHUTOFF, 0)
            self.runtime._get_domain = MagicMock(return_value=domain)
            self.runtime.exec_command_wait = AsyncMock()

            usage = await self.runtime.get_workspace_usage("workspace")

        self.assertEqual(
            usage,
            {
                "cpu_time_ns": 0,
                "vcpu_count": 0,
                "ram_used_bytes": 0,
                "ram_total_bytes": 0,
                "disk_used_bytes": expected_used,
                "disk_total_bytes": 0,
            },
        )
        domain.info.assert_not_called()
        domain.memoryStats.assert_not_called()
        domain.blockInfo.assert_not_called()
        self.runtime.exec_command_wait.assert_not_called()

    async def test_usage_uses_xml_disk_target_when_default_bus_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            disk_path = Path(tmpdir) / "workspace.qcow2"
            disk_path.write_bytes(b"overlay")
            expected_used = disk_path.stat().st_blocks * 512
            self.runtime._disk_path = MagicMock(return_value=disk_path)
            domain = MagicMock()
            domain.state.return_value = (libvirt.VIR_DOMAIN_RUNNING, 0)
            domain.info.return_value = (1, 2048, 1024, 2, 50)
            domain.memoryStats.return_value = {"actual": 2048, "rss": 1024}
            domain.blockInfo.side_effect = [
                libvirt.libvirtError("unknown disk vda"),
                (16 * 1024**3, 87654321, 0),
            ]
            domain.XMLDesc.return_value = (
                "<domain><devices><disk device='disk'>"
                "<target dev='sda' bus='scsi'/></disk></devices></domain>"
            )
            self.runtime._get_domain = MagicMock(return_value=domain)
            self.runtime.exec_command_wait = AsyncMock(return_value=(1, ""))

            usage = await self.runtime.get_workspace_usage("workspace")

        self.assertEqual(usage["disk_used_bytes"], expected_used)
        self.assertEqual(usage["disk_total_bytes"], 16 * 1024**3)
        self.assertEqual(domain.blockInfo.call_args_list[0].args, ("vda", 0))
        self.assertEqual(domain.blockInfo.call_args_list[1].args, ("sda", 0))

    async def test_usage_returns_none_when_overlay_stat_fails(self) -> None:
        for error in (
            PermissionError("permission denied"),
            FileNotFoundError("missing overlay"),
        ):
            with self.subTest(error=type(error).__name__):
                self.runtime._disk_path = MagicMock()
                self.runtime._disk_path.return_value.stat.side_effect = error
                self.runtime._get_domain = MagicMock()

                usage = await self.runtime.get_workspace_usage("workspace")

                self.assertIsNone(usage)
                self.runtime._get_domain.assert_not_called()


class QemuRuntimeDesktopProxyTests(unittest.TestCase):
    def test_get_guest_ip_returns_workspace_vm_ip(self) -> None:
        runtime = object.__new__(QemuRuntime)
        runtime._get_workspace_vm_ip = MagicMock(return_value="10.100.0.2")

        result = runtime.get_guest_ip("instance-1", "workspace-1")

        self.assertEqual(result, "10.100.0.2")
        runtime._get_workspace_vm_ip.assert_called_once_with("instance-1")


if __name__ == "__main__":
    unittest.main()
