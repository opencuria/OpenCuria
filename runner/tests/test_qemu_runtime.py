import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.runtime.qemu_runtime import QemuRuntime


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
        ):
            with self.assertRaises(RuntimeError) as ctx:
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
                mock_path.side_effect = lambda *args, **kwargs: real_path(*args, **kwargs)
                mock_path.return_value = legacy
                with patch.object(
                    self.runtime,
                    "_ensure_ubuntu_cloud_image",
                    side_effect=AssertionError("should not download 22.04"),
                ):
                    with patch.object(real_path, "exists", autospec=True) as exists_mock:
                        exists_mock.side_effect = lambda path_obj: str(path_obj) == str(legacy)
                        resolved = self.runtime._resolve_build_base_image("ubuntu:22.04")

            self.assertEqual(resolved, legacy)

    def test_resolve_build_base_image_downloads_release_specific_ubuntu_image(self) -> None:
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
            self.runtime._settings = SimpleNamespace(qemu_image_cache_dir=str(images_dir))

            response = MagicMock()
            response.read.side_effect = [b"chunk-1", b"chunk-2", b""]
            response.__enter__.return_value = response
            response.__exit__.return_value = False

            with patch.object(self.runtime, "_ensure_host_directory") as ensure_dir_mock:
                with patch("src.runtime.qemu_runtime.urllib.request.urlopen", return_value=response) as urlopen_mock:
                    target = self.runtime._ensure_ubuntu_cloud_image("24.04")

            self.assertEqual(target, images_dir / "ubuntu-24.04-server-cloudimg-amd64.img")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_bytes(), b"chunk-1chunk-2")
            ensure_dir_mock.assert_called_once()
            urlopen_mock.assert_called_once_with(
                "https://cloud-images.ubuntu.com/releases/server/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img",
                timeout=300,
            )


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

            process = AsyncMock()
            process.communicate.return_value = (b"", b"")
            process.returncode = 0

            async def _create_subprocess_exec(*args, **kwargs):
                tmp_target = target_path.with_suffix(".qcow2.tmp")
                tmp_target.write_bytes(b"qcow2")
                return process

            with patch("src.runtime.qemu_runtime._domain_xml", return_value="<domain/>"):
                with patch("src.runtime.qemu_runtime.asyncio.to_thread", new=AsyncMock()):
                    with patch(
                        "src.runtime.qemu_runtime.asyncio.create_subprocess_exec",
                        new=AsyncMock(side_effect=_create_subprocess_exec),
                    ):
                        result = await runtime.build_image(
                            base_distro="ubuntu:24.04",
                            init_script="#!/bin/bash\napt-get update\n",
                            image_path=str(target_path),
                        )

        runtime._stream_ssh_process.assert_awaited_once_with(
            unittest.mock.ANY,
            "sudo -E bash /tmp/opencuria-image-build.sh",
            None,
        )
        self.assertEqual(result["image_path"], str(target_path))


class QemuRuntimeDesktopProxyTests(unittest.TestCase):
    def test_get_container_ip_returns_workspace_vm_ip(self) -> None:
        runtime = object.__new__(QemuRuntime)
        runtime._get_workspace_vm_ip = MagicMock(return_value="10.100.0.2")

        result = runtime.get_container_ip("instance-1", "workspace-1")

        self.assertEqual(result, "10.100.0.2")
        runtime._get_workspace_vm_ip.assert_called_once_with("instance-1")

    def test_get_workspace_network_name_returns_empty_string(self) -> None:
        runtime = object.__new__(QemuRuntime)

        self.assertEqual(runtime.get_workspace_network_name("workspace-1"), "")

    def test_resolve_image_artifact_path_accepts_absolute_base_image_path(self) -> None:
        runtime = object.__new__(QemuRuntime)
        runtime._snapshot_dir = Path("/var/lib/opencuria/snapshots")

        result = runtime._resolve_image_artifact_path(
            "/var/lib/opencuria/base-images/image.qcow2"
        )

        self.assertEqual(result, Path("/var/lib/opencuria/base-images/image.qcow2"))

    def test_resolve_image_artifact_path_uses_snapshot_dir_for_snapshot_ids(self) -> None:
        runtime = object.__new__(QemuRuntime)
        runtime._snapshot_dir = Path("/var/lib/opencuria/snapshots")

        result = runtime._resolve_image_artifact_path("artifact-123")

        self.assertEqual(
            result,
            Path("/var/lib/opencuria/snapshots/artifact-123.qcow2"),
        )


class QemuRuntimeImageArtifactDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_image_artifact_blocks_when_workspace_disk_depends_on_snapshot(self) -> None:
        runtime = object.__new__(QemuRuntime)

        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_dir = Path(tmpdir) / "snapshots"
            disk_dir = Path(tmpdir) / "disks"
            snapshot_dir.mkdir()
            disk_dir.mkdir()

            runtime._snapshot_dir = snapshot_dir
            runtime._disk_dir = disk_dir

            snapshot_path = snapshot_dir / "artifact-123.qcow2"
            snapshot_path.write_bytes(b"snapshot")
            (snapshot_dir / "artifact-123.meta").write_text("snapshot_id=artifact-123\n")
            (disk_dir / "workspace-1.qcow2").write_bytes(b"overlay")

            runtime._get_qcow2_backing_path = AsyncMock(return_value=snapshot_path.resolve())

            with self.assertRaises(RuntimeError) as ctx:
                await runtime.delete_image_artifact("artifact-123")

            self.assertIn("workspace-1", str(ctx.exception))
            self.assertTrue(snapshot_path.exists())

    async def test_delete_image_artifact_removes_files_when_snapshot_has_no_dependents(self) -> None:
        runtime = object.__new__(QemuRuntime)

        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_dir = Path(tmpdir) / "snapshots"
            disk_dir = Path(tmpdir) / "disks"
            snapshot_dir.mkdir()
            disk_dir.mkdir()

            runtime._snapshot_dir = snapshot_dir
            runtime._disk_dir = disk_dir

            snapshot_path = snapshot_dir / "artifact-123.qcow2"
            meta_path = snapshot_dir / "artifact-123.meta"
            snapshot_path.write_bytes(b"snapshot")
            meta_path.write_text("snapshot_id=artifact-123\n")

            runtime._get_qcow2_backing_path = AsyncMock(return_value=None)

            await runtime.delete_image_artifact("artifact-123")

            self.assertFalse(snapshot_path.exists())
            self.assertFalse(meta_path.exists())


if __name__ == "__main__":
    unittest.main()
