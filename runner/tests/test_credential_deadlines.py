"""Credential IO cannot retain a workspace lifecycle reservation forever."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest
from src.services.credentials import CredentialManager


@pytest.mark.parametrize("stage", ["scrub", "upload", "install"])
async def test_hung_credential_io_times_out_and_later_sync_can_succeed(stage: str):
    """A timeout cancels waiting, never reports clean disk, and permits recovery."""
    cancelled = asyncio.Event()
    hanging = True
    calls = 0

    async def block() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def execute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if hanging and (stage == "scrub" or stage == "install" and calls == 2):
            await block()
        return 0, ""

    async def upload(*args, **kwargs):
        if hanging and stage == "upload":
            await block()

    runtime = Mock(
        exec_command_wait=AsyncMock(side_effect=execute),
        put_archive=AsyncMock(side_effect=upload),
    )
    manager = CredentialManager(sync_timeout=0.02)
    with pytest.raises(RuntimeError, match="execution limit"):
        await manager.inject_workspace_credentials(
            runtime, "guest", {"TOKEN": "example"}, [], [], Mock()
        )
    assert cancelled.is_set()
    hanging = False
    assert await manager.inject_workspace_credentials(
        runtime, "guest", {"TOKEN": "example"}, [], [], Mock()
    )
    await manager.remove_workspace_credentials(runtime, "guest", Mock())
