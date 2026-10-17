"""PTY reattach, replay and uncertain stdin failure boundaries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.runtime.base import PtyHandle
from src.runtime.docker_runtime import DockerRuntime
from src.services.sessions.terminals import (
    TerminalManager,
    TerminalReplayError,
    TerminalSession,
)


def manager():
    """Build an isolated real session manager without a container dependency."""
    runtime = SimpleNamespace(pty_write=AsyncMock(), pty_close=AsyncMock())
    handle = PtyHandle("instance", object())
    value = TerminalManager()
    value._terminals["terminal"] = TerminalSession(handle, runtime)
    return value, runtime


async def test_stdin_duplicate_is_not_written_twice():
    value, runtime = manager()
    assert await value.write_terminal("terminal", b"command\n", input_id="same")
    assert not await value.write_terminal("terminal", b"command\n", input_id="same")
    runtime.pty_write.assert_awaited_once()
    assert value.confirmed_inputs("terminal") == ["same"]


async def test_failed_stdin_is_not_claimed_successful():
    value, runtime = manager()
    runtime.pty_write.side_effect = OSError("transport lost: outcome unknown")
    with pytest.raises(OSError):
        await value.write_terminal("terminal", b"command\n", input_id="uncertain")
    assert value.confirmed_inputs("terminal") == []


async def test_replay_cursor_overflow_is_explicit_and_preserves_pty():
    value, runtime = manager()
    session = value._terminals["terminal"]
    session.sequence = 3
    session.output.extend([(2, b"second"), (3, b"third")])
    with pytest.raises(TerminalReplayError):
        await value.reattach_terminal("terminal", 0)
    assert await value.reattach_terminal("terminal", 1) == [
        (2, b"second"),
        (3, b"third"),
    ]
    assert value.has_terminal("terminal")
    runtime.pty_close.assert_not_awaited()
    with pytest.raises(ValueError, match="ahead"):
        await value.reattach_terminal("terminal", 4)


async def test_replay_and_live_output_are_serialized():
    value, _runtime = manager()
    session = value._terminals["terminal"]
    session.sequence = 1
    session.output.append((1, b"before"))
    session.attached = False
    seen = []

    async def deliver(seq, data):
        seen.append((seq, data))
        await asyncio.sleep(0)

    await value.reattach_terminal("terminal", 0, deliver=deliver)
    assert seen == [(1, b"before")]
    assert session.attached


async def test_docker_pty_does_not_use_http_idle_timeout():
    runtime = DockerRuntime()
    api = MagicMock()
    api.exec_create.return_value = {"Id": "pty-exec"}
    socket = MagicMock()
    api.exec_start.return_value = socket
    runtime._client = MagicMock(api=api)
    handle = await runtime.exec_pty("instance")
    assert handle.metadata["exec_id"] == "pty-exec"
    socket._sock.settimeout.assert_called_once_with(None)
