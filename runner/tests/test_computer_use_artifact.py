"""Reviewed bundle, strict archive layout and actual guest installation."""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.runtime import computer_use_artifact as package
from src.runtime.artifacts import ArtifactProvisionError, RuntimeArtifactManager


def test_shipped_bundle_hash_and_layout():
    spec = json.loads(package.ASSET.with_suffix("").with_suffix(".json").read_text())
    assert package.ASSET.stat().st_size == spec["size"]
    assert package.sha_file(package.ASSET) == spec["sha256"]
    hashes = package.inspect_bundle(package.ASSET)
    with tarfile.open(package.ASSET, "r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
    assert manifest["version"] == package.VERSION
    assert {k: v for k, v in hashes.items() if k != "manifest.json"} == manifest[
        "files"
    ]


@pytest.mark.parametrize(
    "name,kind", [("../server.js", tarfile.REGTYPE), ("server.js", tarfile.SYMTYPE)]
)
def test_rejects_archive_traversal_or_links(tmp_path, name, kind):
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Invalid"):
        package.inspect_bundle(path)


@pytest.mark.skipif(
    not os.environ.get("CUA_TEST_NODE_ARCHIVE"),
    reason="set CUA_TEST_NODE_ARCHIVE to the pinned official Node archive",
)
async def test_real_manager_guest_install_cache_and_tampering(tmp_path):
    """Use real subprocesses and package bytes through the shared manager."""

    class Guest:
        uploads = 0

        async def exec_command_wait(self, instance, command):
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            return process.returncode, stdout.decode()

        async def put_archive(self, instance, directory, payload):
            self.uploads += 1
            with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                for member in archive:
                    assert "/" not in member.name and member.isfile()
                    Path(directory, member.name).write_bytes(
                        archive.extractfile(member).read()
                    )

    guest = Guest()
    manager = RuntimeArtifactManager(
        guest,
        lambda _: SimpleNamespace(instance_id="guest"),
        lambda _: guest,
        tmp_path / "cache",
        guest_runtime_root=str(tmp_path / "runtimes"),
    )

    async def download(url, size, digest, destination):
        source = Path(os.environ["CUA_TEST_NODE_ARCHIVE"])
        assert source.stat().st_size == size and package.sha_file(source) == digest
        shutil.copyfile(source, destination)

    manager._download = download
    workspace = uuid.uuid4()
    result = await manager.ensure_runtime_artifact(
        workspace, "computer-use", package.VERSION
    )
    assert Path(result["path"]).is_file()
    assert (
        await manager.ensure_runtime_artifact(
            workspace, "computer-use", package.VERSION
        )
        == result
    )
    assert guest.uploads == 1
    server = Path(result["path"]).with_name("server.js")
    server.chmod(0o644)
    server.write_text("changed")
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(
            workspace, "computer-use", package.VERSION
        )
    # pytest can remove immutable installed trees after the verification test.
    for path in tmp_path.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
