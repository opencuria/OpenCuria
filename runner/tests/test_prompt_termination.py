import uuid
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.service import WorkspaceService


def test_build_prompt_termination_script_kills_process_group() -> None:
    script = WorkspaceService._build_prompt_termination_script(
        "/tmp/opencuria-prompt-task-123.pid"
    )

    assert "ps -o pgid= -p \"$pid\"" in script
    assert "kill -TERM -- \"-$pgid\"" in script
    assert "kill -KILL -- \"-$pgid\"" in script
    assert "kill -TERM \"$pid\"" in script
    assert "kill -KILL \"$pid\"" in script


@pytest.mark.asyncio
async def test_terminate_prompt_process_uses_group_termination_script() -> None:
    service = WorkspaceService({}, RunnerSettings())
    service.run_command_wait = AsyncMock()
    workspace_id = uuid.uuid4()
    pid_file = "/tmp/opencuria-prompt-task-456.pid"

    await service.terminate_prompt_process(workspace_id, pid_file)

    service.run_command_wait.assert_awaited_once()
    call = service.run_command_wait.await_args
    assert call.args[0] == workspace_id
    assert call.args[1]["args"] == [
        "sh",
        "-lc",
        WorkspaceService._build_prompt_termination_script(pid_file),
    ]
    assert call.args[1]["description"] == "Terminate active prompt process"
