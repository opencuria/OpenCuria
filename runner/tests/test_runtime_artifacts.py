"""Pinned runtime artifact provisioning and a real local guest integration."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import shutil
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.runtime import artifacts
from src.runtime.artifacts import ArtifactProvisionError, RuntimeArtifactManager


class FakeArtifactRuntime:
    """Runtime fake for fast unit coverage; guest commands are canned."""

    runtime_type = "docker"

    def __init__(self, platform="linux-x64"):
        self.platform = platform
        self.exec_command_wait = AsyncMock(side_effect=self._exec)
        self.put_archive = AsyncMock()
        self.install_rc = 0
        self.output = "2.1.292\n"

    async def _exec(self, instance_id, command, workdir=None, env=None):
        if command[0] == "sh" and command[1] == "-c":
            if "uname" in command[2]:
                if getattr(self, "platform_probe_failure", False):
                    return (0, "")
                return (0, self.platform + "\n")
            return (self.install_rc, self.output)
        raise AssertionError(command)


def _fixture_manifest(tmp_path, payload: bytes, monkeypatch):
    digest = hashlib.sha256(payload).hexdigest()
    pins = {**artifacts.CLAUDE_PLATFORM_PINS, "linux-x64": (digest, len(payload))}
    monkeypatch.setattr(artifacts, "CLAUDE_PLATFORM_PINS", pins)
    manifest = json.loads(artifacts.ARTIFACT_MANIFEST_PATH.read_text("utf-8"))
    manifest["artifacts"]["claude-agent"]["platforms"]["linux-x64"].update(
        sha256=digest,
        size=len(payload),
    )
    entry = manifest["artifacts"]["claude-agent"]
    entry["manifest_sha256"] = "x"
    canonical = (json.dumps(manifest, indent=2) + "\n").encode()
    spec_digest = hashlib.sha256(canonical).hexdigest()
    monkeypatch.setattr(artifacts, "CLAUDE_ARTIFACT_SPEC_SHA256", spec_digest)
    entry["manifest_sha256"] = spec_digest
    manifest_path = tmp_path / "artifact_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest_path, digest


@pytest.fixture
def setup_manager(tmp_path, monkeypatch):
    payload = b"small verified fixture binary"
    manifest_path, digest = _fixture_manifest(tmp_path, payload, monkeypatch)
    runtime = FakeArtifactRuntime()
    info = SimpleNamespace(instance_id="container-1")
    manager = RuntimeArtifactManager(
        {"docker": runtime},
        lambda _: info,
        lambda _: runtime,
        tmp_path / "runner-state",
        manifest_path=manifest_path,
        guest_runtime_root=str(tmp_path / "guest" / "runtimes"),
    )

    async def write_download(url, size, expected_digest, destination):
        assert url.endswith("/linux-x64/claude")
        assert size == len(payload)
        assert expected_digest == digest
        destination.write_bytes(payload)

    manager._download = AsyncMock(side_effect=write_download)
    return manager, runtime, info, payload, digest


@pytest.mark.asyncio
async def test_ensure_downloads_once_caches_hash_and_returns_pinned_path(setup_manager):
    manager, runtime, _info, _payload, digest = setup_manager
    workspace_id = uuid.uuid4()

    first = await manager.ensure_runtime_artifact(
        workspace_id, "claude-agent", "2.1.292"
    )
    second = await manager.ensure_runtime_artifact(
        workspace_id, "claude-agent", "2.1.292"
    )

    assert first == {
        "ok": True,
        "path": f"{manager._guest_runtime_root}/claude-agent/2.1.292/claude",
        "version": "2.1.292",
        "platform": "linux-x64",
    }
    assert second == first
    manager._download.assert_awaited_once()
    assert runtime.put_archive.await_count == 2
    archive = runtime.put_archive.await_args.args[2]
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        assert len(tar.getnames()) == 1
        assert tar.getnames()[0].startswith(f"opencuria-claude-agent-{digest}-")


@pytest.mark.asyncio
async def test_cache_hash_mismatch_triggers_a_verified_redownload(setup_manager):
    manager, _runtime, _info, _payload, digest = setup_manager
    await manager.ensure_runtime_artifact(uuid.uuid4(), "claude-agent", "2.1.292")
    cache = next(manager._cache_root.rglob("claude"))
    cache.write_bytes(b"corrupt")

    await manager.ensure_runtime_artifact(uuid.uuid4(), "claude-agent", "2.1.292")

    assert manager._download.await_count == 2
    assert manager._hash_file(cache) == digest


@pytest.mark.asyncio
async def test_rejects_unsupported_guest_platform_and_unapproved_artifact(setup_manager):
    manager, runtime, _info, _payload, _digest = setup_manager
    runtime.platform = "linux-riscv64"
    with pytest.raises(ValueError, match="Unsupported workspace platform"):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "claude-agent", "2.1.292")
    with pytest.raises(ValueError, match="Unapproved"):
        await manager.ensure_runtime_artifact(uuid.uuid4(), "anything", "2.1.292")
    manager._download.assert_not_awaited()


@pytest.mark.asyncio
async def test_provision_failure_is_sanitized(setup_manager):
    manager, runtime, _info, _payload, _digest = setup_manager
    runtime.put_archive.side_effect = RuntimeError("secret stdout or env")

    with pytest.raises(ArtifactProvisionError, match="Runtime artifact provisioning failed") as exc:
        await manager.ensure_runtime_artifact(uuid.uuid4(), "claude-agent", "2.1.292")

    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_cancelled_archive_upload_is_settled_then_temp_is_removed(setup_manager):
    manager, runtime, _info, _payload, _digest = setup_manager
    runtime.platform_probe_failure = False
    entered = asyncio.Event()
    finish_upload = asyncio.Event()
    cleaned = asyncio.Event()

    async def upload(_instance, _directory, _archive):
        entered.set()
        await finish_upload.wait()

    async def cleanup(_instance, command, workdir=None, env=None):
        assert command[0:2] == ["sh", "-c"]
        if "uname -s" in command[2]:
            return 0, "linux-x64\n"
        cleaned.set()
        return 0, ""

    runtime.platform = "linux-x64"
    runtime.put_archive.side_effect = upload
    async def platform_exec(_instance, command, workdir=None, env=None):
        if "uname" in command[2]:
            return 0, "linux-x64\n"
        return await cleanup(_instance, command, workdir=workdir, env=env)

    runtime.exec_command_wait.side_effect = platform_exec
    request = asyncio.create_task(
        manager.ensure_runtime_artifact(uuid.uuid4(), "claude-agent", "2.1.292")
    )
    await asyncio.wait_for(entered.wait(), 2)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not cleaned.is_set()
    finish_upload.set()
    for _ in range(100):
        if cleaned.is_set():
            break
        await asyncio.sleep(0.01)
    assert cleaned.is_set()
    assert not manager._background_guest_tasks


@pytest.mark.skipif(not Path("/tmp/claude-x64").is_file(), reason="real pinned CLI not available")
@pytest.mark.asyncio
async def test_real_pinned_claude_binary_install_cache_and_corruption(
    tmp_path, monkeypatch
):
    """Run actual release bytes and guest installer through local runtime fakes."""
    source = Path("/tmp/claude-x64")
    expected, size = artifacts.CLAUDE_PLATFORM_PINS["linux-x64"]
    assert source.stat().st_size == size
    digest = await asyncio.to_thread(lambda: hashlib.file_digest(source.open("rb"), "sha256").hexdigest())
    assert digest == expected

    root = tmp_path / "guest"
    root.mkdir()
    runtime = FakeArtifactRuntime()
    info = SimpleNamespace(instance_id="local-guest")
    manager = RuntimeArtifactManager(
        {"docker": runtime},
        lambda _: info,
        lambda _: runtime,
        tmp_path / "runner-state",
        manifest_path=artifacts.ARTIFACT_MANIFEST_PATH,
        guest_runtime_root="/opt/opencuria/runtimes",
    )
    class FakeResponseContent:
        async def iter_chunked(self, chunk_size):
            stream = await asyncio.to_thread(source.open, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(stream.read, chunk_size)
                    if not chunk:
                        return
                    yield chunk
            finally:
                await asyncio.to_thread(stream.close)

    class FakeResponse:
        def __init__(self):
            self.status = 200
            self.headers = {"Content-Length": str(size)}
            self.content = FakeResponseContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    class FakeHTTPSession:
        def __init__(self, *, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        def get(self, url, *, allow_redirects):
            assert url == "https://downloads.claude.ai/claude-code-releases/2.1.292/linux-x64/claude"
            assert allow_redirects is False
            return FakeResponse()

    monkeypatch.setattr(artifacts.aiohttp, "ClientSession", FakeHTTPSession)

    uploaded_names = []

    async def put_archive(_instance, _directory, archive):
        with tarfile.open(fileobj=io.BytesIO(archive)) as archive_file:
            members = archive_file.getmembers()
            assert len(members) == 1
            member = members[0]
            assert member.name.startswith(f"opencuria-claude-agent-{expected}-")
            uploaded_names.append(member.name)
            destination = root / "tmp" / member.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive_file.extractfile(member) as contents, destination.open("wb") as output:
                shutil.copyfileobj(contents, output, length=1024 * 1024)
            destination.chmod(0o600)

    async def guest_exec(_instance, command, workdir=None, env=None):
        if "uname -s" in command[2]:
            return 0, "linux-x64\n"
        script = command[2]
        archive_source = f"/tmp/{uploaded_names[-1]}"
        local_source = str(root / "tmp" / archive_source.rsplit("/", 1)[1])
        script = script.replace(archive_source, local_source)
        script = script.replace("/opt/opencuria/runtimes", str(root / "opt/opencuria/runtimes"))
        args = [
            local_source if arg == archive_source else
            str(root / "opt/opencuria/runtimes") if arg == "/opt/opencuria/runtimes" else arg
            for arg in command[3:]
        ]
        completed = await asyncio.create_subprocess_exec(
            "sh", "-c", script, *args,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await completed.communicate()
        output = (stdout + stderr).decode()
        if completed.returncode:
            pytest.fail(f"guest script failed {completed.returncode}: {output[-1000:]}")
        return completed.returncode, output

    runtime.put_archive = AsyncMock(side_effect=put_archive)
    runtime.exec_command_wait = AsyncMock(side_effect=guest_exec)
    workspace_id = uuid.uuid4()
    result = await manager.ensure_runtime_artifact(
        workspace_id, "claude-agent", "2.1.292"
    )
    installed = root / result["path"].lstrip("/")
    cached_binary = next(manager._cache_root.rglob("claude"))
    assert cached_binary.stat().st_size == size
    assert manager._hash_file(cached_binary) == expected
    assert installed.is_file() and not installed.is_symlink()
    assert installed.stat().st_mode & 0o777 == 0o555
    assert manager._hash_file(installed) == expected
    version = await asyncio.create_subprocess_exec(
        str(installed), "--version", stdout=asyncio.subprocess.PIPE
    )
    output, _ = await version.communicate()
    assert version.returncode == 0 and output.decode().startswith("2.1.292 ")
    inode = installed.stat().st_ino

    # Idempotent ensure keeps the same pinned executable inode.
    assert await manager.ensure_runtime_artifact(
        workspace_id, "claude-agent", "2.1.292"
    ) == result
    assert installed.stat().st_ino == inode

    installed.chmod(0o755)
    installed.write_bytes(b"tampered")
    runtime.exec_command_wait = AsyncMock(
        side_effect=RuntimeError("corrupt guest executable")
    )
    with pytest.raises(ArtifactProvisionError):
        await manager.ensure_runtime_artifact(
            workspace_id, "claude-agent", "2.1.292"
        )
