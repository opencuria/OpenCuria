import io
import tarfile
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.service import WorkspaceService


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
        WorkspaceService._convert_archive_to_tar(source.getvalue())


@pytest.mark.asyncio
async def test_create_workspace_uses_operation_credentials_for_clone_and_configure() -> None:
    class DummyRuntime:
        async def create_workspace(self, config):  # noqa: ANN001
            return "instance-1"

    runtime = DummyRuntime()
    service = WorkspaceService({"docker": runtime}, RunnerSettings())
    service._cleanup_legacy_workspace_credentials = AsyncMock()
    credential_context = object()
    service._create_operation_credential_context = AsyncMock(
        return_value=credential_context
    )
    service._cleanup_operation_credential_context = AsyncMock()
    service._exec_command = AsyncMock(side_effect=[(0, ""), (0, "")])

    await service.create_workspace(
        repos=["git@github.com:example/private-repo.git"],
        configure_commands=[
            {
                "args": ["echo", "configured"],
                "workdir": "/workspace",
                "env": {},
                "description": "configure",
            }
        ],
        env_vars={"GITHUB_TOKEN": "secret"},
        ssh_keys=["-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"],
        runtime_type="docker",
        image_tag="opencuria/workspace:test",
    )

    create_ctx_args = service._create_operation_credential_context.await_args.args
    assert create_ctx_args[0] is runtime
    assert create_ctx_args[1] == "instance-1"
    assert create_ctx_args[2] == {"GITHUB_TOKEN": "secret"}
    assert create_ctx_args[3] == [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nmock\n-----END OPENSSH PRIVATE KEY-----"
    ]
    assert service._exec_command.await_count == 2
    clone_call = service._exec_command.await_args_list[0]
    assert clone_call.kwargs["credential_context"] is credential_context
    assert clone_call.args[2]["args"] == [
        "git",
        "clone",
        "git@github.com:example/private-repo.git",
    ]
    configure_call = service._exec_command.await_args_list[1]
    assert configure_call.kwargs["credential_context"] is credential_context
    service._cleanup_operation_credential_context.assert_awaited_once()
