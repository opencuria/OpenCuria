import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

asyncssh = pytest.importorskip("asyncssh")
pytest.importorskip("libvirt")

from src.runtime.base import ProcessHandle
from src.runtime.qemu_runtime import QemuRuntime


class _Lines:
    def __init__(self, *lines: str) -> None:
        self._lines = iter(lines)

    def __aiter__(self) -> AsyncIterator[str]:
        return self

    async def __anext__(self) -> str:
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration from None


class _ExitStatus:
    def __init__(self, status: int | Exception | None, **attributes: object) -> None:
        self._status = status
        for name, value in attributes.items():
            setattr(self, name, value)

    @property
    def exit_status(self) -> int | None:
        if isinstance(self._status, Exception):
            raise self._status
        return self._status


def _runtime_with_ssh(ssh: SimpleNamespace) -> QemuRuntime:
    runtime = object.__new__(QemuRuntime)
    runtime._get_ssh = AsyncMock(return_value=ssh)
    return runtime


@pytest.mark.parametrize("exit_status", [0, 19])
async def test_process_wait_preserves_known_exit_status(exit_status: int) -> None:
    process = SimpleNamespace(
        wait_closed=AsyncMock(),
        exit_status=exit_status,
    )
    runtime = object.__new__(QemuRuntime)

    assert await runtime.process_wait(ProcessHandle("vm", process)) == exit_status
    process.wait_closed.assert_awaited_once()


async def test_process_wait_returns_none_for_missing_exit_status() -> None:
    process = _ExitStatus(None, wait_closed=AsyncMock())
    runtime = object.__new__(QemuRuntime)

    assert await runtime.process_wait(ProcessHandle("vm", process)) is None


@pytest.mark.parametrize(
    "status_error",
    [asyncssh.Error(1, "status unavailable"), OSError("status unavailable")],
)
async def test_process_wait_returns_none_when_exit_status_cannot_be_read(
    status_error: Exception,
) -> None:
    process = _ExitStatus(status_error, wait_closed=AsyncMock())
    runtime = object.__new__(QemuRuntime)

    assert await runtime.process_wait(ProcessHandle("vm", process)) is None


@pytest.mark.parametrize(
    ("wait_error", "exit_status"),
    [
        (asyncssh.Error(1, "channel lost"), 0),
        (OSError("channel lost"), 12),
    ],
)
async def test_process_wait_keeps_known_status_after_wait_error(
    wait_error: Exception, exit_status: int
) -> None:
    process = SimpleNamespace(
        wait_closed=AsyncMock(side_effect=wait_error), exit_status=exit_status
    )
    runtime = object.__new__(QemuRuntime)

    assert await runtime.process_wait(ProcessHandle("vm", process)) == exit_status


async def test_process_wait_returns_none_for_closed_handle_without_waiting() -> None:
    process = SimpleNamespace(wait_closed=AsyncMock(), exit_status=0)
    handle = ProcessHandle("vm", process)
    handle.closed = True
    runtime = object.__new__(QemuRuntime)

    assert await runtime.process_wait(handle) is None
    process.wait_closed.assert_not_awaited()


@pytest.mark.parametrize(
    "wait_error",
    [asyncssh.Error(1, "channel lost"), OSError("channel lost")],
)
async def test_process_wait_swallows_transport_wait_error_and_returns_unknown(
    wait_error: Exception,
) -> None:
    process = SimpleNamespace(
        wait_closed=AsyncMock(side_effect=wait_error),
        exit_status=None,
    )
    runtime = object.__new__(QemuRuntime)

    assert await runtime.process_wait(ProcessHandle("vm", process)) is None


async def test_process_wait_propagates_cancellation() -> None:
    process = SimpleNamespace(
        wait_closed=AsyncMock(side_effect=asyncio.CancelledError()),
        exit_status=None,
    )
    runtime = object.__new__(QemuRuntime)

    with pytest.raises(asyncio.CancelledError):
        await runtime.process_wait(ProcessHandle("vm", process))


@pytest.mark.parametrize(
    "unknown_status",
    [None, asyncssh.Error(1, "status unavailable"), OSError("status unavailable")],
)
async def test_exec_command_wait_rejects_unknown_exit_without_replaying(
    unknown_status: int | Exception | None,
) -> None:
    ssh = SimpleNamespace(
        run=AsyncMock(return_value=_ExitStatus(unknown_status, stdout="", stderr=""))
    )
    runtime = _runtime_with_ssh(ssh)

    with pytest.raises(RuntimeError, match="exit status is unknown"):
        await runtime.exec_command_wait("vm", ["echo", "test"])

    runtime._get_ssh.assert_awaited_once_with("vm")
    ssh.run.assert_awaited_once()


@pytest.mark.parametrize("exit_status", [0, 17])
async def test_exec_command_wait_preserves_known_exit_status(exit_status: int) -> None:
    ssh = SimpleNamespace(
        run=AsyncMock(
            return_value=SimpleNamespace(
                exit_status=exit_status, stdout="stdout", stderr="stderr"
            )
        )
    )
    runtime = _runtime_with_ssh(ssh)

    result = await runtime.exec_command_wait("vm", ["command"])

    assert result == (exit_status, "stdoutstderr")
    ssh.run.assert_awaited_once()


@pytest.mark.parametrize(
    "unknown_status",
    [None, asyncssh.Error(1, "status unavailable"), OSError("status unavailable")],
)
async def test_stream_ssh_process_rejects_unknown_exit_without_replaying(
    unknown_status: int | Exception | None,
) -> None:
    process = _ExitStatus(
        unknown_status,
        stdout=_Lines("output\n"),
        wait_closed=AsyncMock(),
    )
    ssh = SimpleNamespace(create_process=AsyncMock(return_value=process))
    runtime = _runtime_with_ssh(ssh)

    with pytest.raises(RuntimeError, match="exit status is unknown"):
        await runtime._stream_ssh_process("vm", "echo test", None)

    runtime._get_ssh.assert_awaited_once_with("vm")
    ssh.create_process.assert_awaited_once()


async def test_stream_ssh_process_keeps_nonzero_runtime_error() -> None:
    process = SimpleNamespace(
        stdout=_Lines("output\n"),
        wait_closed=AsyncMock(),
        exit_status=17,
    )
    ssh = SimpleNamespace(create_process=AsyncMock(return_value=process))
    runtime = _runtime_with_ssh(ssh)

    with pytest.raises(RuntimeError, match="exit code 17"):
        await runtime._stream_ssh_process("vm", "false", None)

    ssh.create_process.assert_awaited_once()
