import io
import tarfile
from unittest.mock import AsyncMock, Mock

import pytest
from src.config import RunnerSettings
from src.service import WorkspaceService
from src.services.credentials import (
    WORKSPACE_CREDENTIAL_DIR,
    WORKSPACE_CREDENTIAL_ENV_FILE,
)
from src.services.files import convert_archive_to_tar


@pytest.mark.parametrize("link_type", ["sym", "hard"])
def test_convert_archive_to_tar_rejects_links(link_type: str) -> None:
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode="w") as tar:
        info = tarfile.TarInfo(name="malicious-link")
        info.linkname = "/etc/passwd"
        info.type = tarfile.SYMTYPE if link_type == "sym" else tarfile.LNKTYPE
        tar.addfile(info)
    source.seek(0)

    with pytest.raises(ValueError, match="unsafe links"):
        convert_archive_to_tar(source.getvalue())


@pytest.mark.asyncio
async def test_create_workspace_injects_credentials_and_leaves_them() -> None:
    class DummyRuntime:
        def __init__(self):
            self.exec_command_wait = AsyncMock(return_value=(0, ""))
            self.put_archive = AsyncMock()

        async def create_workspace(self, config):
            return "instance-1"

    runtime = DummyRuntime()
    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    service.exec_kernel.exec_command = AsyncMock(side_effect=[(0, "")])

    workspace_id, credentials_present = await service.create_workspace(
        repos=["git@github.com:example/private-repo.git"],
        env_vars={"GITHUB_TOKEN": "secret"},
        ssh_keys=["-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"],
        runtime_type="docker",
        image_tag="opencuria/workspace:test",
    )

    assert credentials_present is True
    runtime.put_archive.assert_awaited_once()
    with tarfile.open(
        fileobj=io.BytesIO(runtime.put_archive.await_args.args[2]), mode="r"
    ) as archive:
        env_file = archive.extractfile("env.sh").read().decode("utf-8")
        ssh_key = archive.extractfile("ssh/id_ed25519").read().decode("utf-8")
    assert "GITHUB_TOKEN=secret" in env_file
    assert "mock" in ssh_key
    assert service.exec_kernel.exec_command.await_count == 1
    clone_call = service.exec_kernel.exec_command.await_args_list[0]
    assert "credential_context" not in clone_call.kwargs
    assert clone_call.args[2]["args"] == [
        "git",
        "clone",
        "git@github.com:example/private-repo.git",
    ]
    assert str(workspace_id)


@pytest.mark.asyncio
async def test_inject_workspace_credentials_materializes_file_credentials() -> None:
    class DummyRuntime:
        def __init__(self) -> None:
            self.archives: list[tuple[str, str, bytes]] = []
            self.commands: list[list[str]] = []

        async def put_archive(self, instance_id, target_dir, archive_data):
            self.archives.append((instance_id, target_dir, archive_data))

        async def exec_command_wait(self, instance_id, command, workdir):
            self.commands.append(command)
            return 0, ""

    runtime = DummyRuntime()
    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    service.credentials.remove_workspace_credentials = AsyncMock()

    present = await service.credentials.inject_workspace_credentials(
        runtime,
        "instance-1",
        {},
        [
            {
                "target_path": "~/.codex/auth.json",
                "content": '{"access_token":"abc"}',
                "mode": 0o600,
            }
        ],
        [],
        log=Mock(),
    )

    assert present is True
    assert runtime.archives
    assert runtime.archives[0][1] == WORKSPACE_CREDENTIAL_DIR
    archive = runtime.archives[0][2]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r") as tar:
        assert tar.extractfile("files/credential_1").read() == b'{"access_token":"abc"}'
        install = tar.extractfile("install.sh").read().decode("utf-8")
        env_file = tar.extractfile("env.sh").read().decode("utf-8")
        manifest = tar.extractfile("manifest").read().decode("utf-8")

    assert "~/.codex/auth.json" in install
    assert "install -m 600" in install
    assert WORKSPACE_CREDENTIAL_ENV_FILE.split("/")[-1] in env_file or "PATH=" in env_file
    assert "~/.codex/auth.json" in manifest
    assert any("install.sh" in " ".join(cmd) for cmd in runtime.commands)


@pytest.mark.asyncio
async def test_inject_workspace_credentials_empty_set_returns_false() -> None:
    runtime = Mock()
    runtime.exec_command_wait = AsyncMock(return_value=(0, ""))
    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    service.credentials.remove_workspace_credentials = AsyncMock()

    present = await service.credentials.inject_workspace_credentials(
        runtime,
        "instance-1",
        {},
        [],
        [],
        log=Mock(),
    )

    assert present is False
    service.credentials.remove_workspace_credentials.assert_awaited_once()
    runtime.exec_command_wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_workspace_removes_credentials_before_runtime_stop(tmp_path) -> None:
    runtime = Mock()
    runtime.stop_workspace = AsyncMock()
    runtime.get_workspace_status = AsyncMock(return_value=Mock(status="running"))
    service = WorkspaceService(
        {"docker": runtime}, RunnerSettings(state_dir=str(tmp_path))
    )
    service.configure_desktop_leases(str(tmp_path), "security-test-epoch")
    service.credentials.remove_workspace_credentials = AsyncMock()
    workspace_id = __import__("uuid").uuid4()
    from src.models import WorkspaceInfo

    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    present = await service.stop_workspace(workspace_id)

    assert present is False
    service.credentials.remove_workspace_credentials.assert_awaited_once()
    runtime.stop_workspace.assert_awaited_once_with("instance-1")
    assert service.credentials.remove_workspace_credentials.await_args.args[1] == "instance-1"


@pytest.mark.asyncio
async def test_start_terminal_sources_persistent_env() -> None:
    runtime = Mock()
    runtime.exec_pty = AsyncMock(return_value=Mock())
    runtime.workspace_exists = AsyncMock(return_value=True)

    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    workspace_id = __import__("uuid").uuid4()
    from src.models import WorkspaceInfo

    service.registry._cache[workspace_id] = WorkspaceInfo(
        workspace_id=workspace_id,
        instance_id="instance-1",
        status="running",
        runtime_type="docker",
    )

    await service.terminal_manager.start_terminal(workspace_id=workspace_id)

    runtime.exec_pty.assert_awaited_once()
    call = runtime.exec_pty.await_args
    assert call.args[0] == "instance-1"
    command = call.kwargs["command"]
    assert command[0] == "/bin/bash"
    assert WORKSPACE_CREDENTIAL_ENV_FILE in command[2]
    assert "TERM" in call.kwargs["env"]


def test_wrap_command_sources_persistent_env() -> None:
    service = WorkspaceService({}, RunnerSettings())
    wrapped = service.exec_kernel.wrap_command_with_persistent_env(
        {"args": ["git", "clone", "repo"], "env": {"EXTRA": "1"}}
    )
    assert wrapped["args"][0] == "bash"
    snippet = wrapped["args"][2]
    assert WORKSPACE_CREDENTIAL_ENV_FILE in snippet
    assert "EXTRA" in snippet
