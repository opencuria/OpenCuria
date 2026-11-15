"""Exec frames retain separate streams, permissive decoding and failure behavior."""

from __future__ import annotations

import asyncio
import base64
import binascii
import random
import tracemalloc
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from src.models import WorkspaceInfo
from src.services.harness_exec import HarnessExecService

from tests.directory_download_runtime import LocalFileRuntime


def _frame(stdout: bytes, stderr: bytes, code: int = 0) -> str:
    return (
        "OPENCURIA_STDOUT\n"
        + base64.encodebytes(stdout).decode("ascii")
        + "OPENCURIA_STDERR\n"
        + base64.encodebytes(stderr).decode("ascii")
        + f"EXIT:{code}\n"
    )


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        (b"", b""),
        (b"output\n", b""),
        (b"", b"error\n"),
        (b"out\n" * 1000, b"err\n" * 1000),
        ("Grüße 🌍\n".encode(), "警告\n".encode()),
        (b"\x00\xff\xc3\x28", b"\xfe\x00\n"),
        (b"OPENCURIA_STDERR\nEXIT:9", b"OPENCURIA_STDOUT"),
    ],
)
def test_separate_streams_and_utf8_replacement(stdout: bytes, stderr: bytes) -> None:
    """Wrapped/binary output and marker-like command text survive decoding."""
    assert HarnessExecService._parse_harness_exec_output(_frame(stdout, stderr)) == (
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


@pytest.mark.parametrize(
    "output", ["", "raw error: ü", "OPENCURIA_STDOUT\nb3V0", "OPENCURIA_STDERR\n"]
)
def test_missing_markers_return_raw_output(output: str) -> None:
    """A failed wrapper's diagnostic remains available without decoding."""
    assert HarnessExecService._parse_harness_exec_output(output) == (output, "")


@pytest.mark.parametrize(
    "whitespace", [" \t\r\n\v\f\x1c\x1f", "\x85\xa0\u2003\u2028\u3000"]
)
def test_whitespace_and_permissive_base64(whitespace: str) -> None:
    """The original split/b64decode contract accepts all Unicode whitespace."""
    out = whitespace.join("b3V0") + "!?"
    err = whitespace.join("ZXJy")
    frame = f"OPENCURIA_STDOUT{out}OPENCURIA_STDERR{err}EXIT:0"
    assert HarnessExecService._parse_harness_exec_output(frame) == ("out", "err")


@pytest.mark.parametrize("whitespace", ["\n\t\x1e", "\u2003\xa0"])
def test_whitespace_only_streams(whitespace: str) -> None:
    frame = f"OPENCURIA_STDOUT{whitespace}OPENCURIA_STDERR{whitespace}EXIT:0"
    assert HarnessExecService._parse_harness_exec_output(frame) == ("", "")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "payload,exception", [("a", binascii.Error), ("ñ", ValueError)]
)
def test_invalid_payload_still_raises(
    stream: str, payload: str, exception: type[Exception]
) -> None:
    """Malformed data cannot silently turn into a successful empty result."""
    out = payload if stream == "stdout" else "b3V0"
    err = payload if stream == "stderr" else "ZXJy"
    with pytest.raises(exception):
        HarnessExecService._parse_harness_exec_output(
            f"OPENCURIA_STDOUT\n{out}\nOPENCURIA_STDERR\n{err}\nEXIT:0"
        )


def test_permissive_payloads_match_base64_contract() -> None:
    """Exercise whitespace, punctuation, padding and invalid data together."""
    rng = random.Random(20261029)
    whitespace = " \t\r\n\v\f\x1c\x85\xa0\u2003\u2028\u3000"
    for index in range(256):
        encoded = base64.b64encode(rng.randbytes(index)).decode()
        payload = "".join(char + rng.choice(whitespace + "!?") for char in encoded)
        if index % 3 == 0:
            payload = payload.rstrip("=" + whitespace + "!?")
        if index % 5 == 0:
            payload += "ñ"
        frame = f"OPENCURIA_STDOUT{payload}OPENCURIA_STDERR\nEXIT:0"
        try:
            expected = base64.b64decode("".join(payload.split())).decode(
                "utf-8", errors="replace"
            )
        except (ValueError, binascii.Error) as exc:
            with pytest.raises(type(exc)) as actual:
                HarnessExecService._parse_harness_exec_output(frame)
            assert str(actual.value) == str(exc)
        else:
            assert HarnessExecService._parse_harness_exec_output(frame) == (
                expected,
                "",
            )


def test_large_frame_allocation_budget() -> None:
    """Retain decoded output without full-frame copies or per-line token lists.

    Input/fixture allocation is excluded. The budget has headroom for decoded
    bytes and returned strings; the former parser exceeds it by over 50%.
    """
    stdout, stderr = b"o" * (2 * 1024 * 1024), b"e" * (2 * 1024 * 1024)
    frame = _frame(stdout, stderr)
    tracemalloc.start()
    try:
        result = HarnessExecService._parse_harness_exec_output(frame)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == (stdout.decode(), stderr.decode())
    assert peak < 2 * len(frame)


@pytest.mark.parametrize(
    "frame,expected",
    [
        ("login noise\nOPENCURIA_STDOUT\nb3V0\nOPENCURIA_STDERR\nZXJy", ("out", "err")),
        ("OPENCURIA_STDERR\nZXJy\nEXIT:7\nOPENCURIA_STDOUT\nb3V0", ("out", "err")),
        (
            "OPENCURIA_STDERR\naWdu\nEXIT:7\nOPENCURIA_STDOUT\nb3V0\n"
            "OPENCURIA_STDERR\nZXJy\nEXIT:0",
            ("out", "ign"),
        ),
        (
            "OPENCURIA_STDOUT\nb3V0\nOPENCURIA_STDERR\nZXJy\nEXIT:bad\nnoise",
            ("out", "err"),
        ),
    ],
)
def test_partial_and_reordered_markers(frame: str, expected: tuple[str, str]) -> None:
    """Keep first-marker boundaries even on partial or unusual wrapper output."""
    assert HarnessExecService._parse_harness_exec_output(frame) == expected


def _service(runtime: object, tmp_path: Path) -> tuple[HarnessExecService, uuid.UUID]:
    workspace_id = uuid.uuid4()
    info = WorkspaceInfo(workspace_id, "local", "running")
    return (
        HarnessExecService(
            get_cached=lambda _: info,
            get_runtime=lambda _: runtime,
            credential_env_file=str(tmp_path / "absent-credentials"),
        ),
        workspace_id,
    )


@pytest.mark.parametrize("code", [0, 7])
async def test_real_wrapper_preserves_large_streams_and_exit(
    tmp_path: Path, code: int
) -> None:
    """Use the actual sh/mktemp/base64 wrapper with independent large streams."""
    runtime = LocalFileRuntime()
    service, workspace_id = _service(runtime, tmp_path)
    stdout = b"build progress\n" * 100_000 + "完成 🌍".encode()
    stderr = b"diagnostic\n" * 100_000 + b"\xff"
    (tmp_path / "out").write_bytes(stdout)
    (tmp_path / "err").write_bytes(stderr)

    result = await service.exec_harness_command(
        workspace_id,
        ["sh", "-c", 'cat out; cat err >&2; exit "$1"', "test", str(code)],
        workdir=str(tmp_path),
    )

    assert result == (
        code,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )
    assert len(runtime.calls) == 1
    assert all(process.returncode is not None for process in runtime.processes)


async def test_concurrent_results_stay_independent(tmp_path: Path) -> None:
    """Decoding retains no state shared by concurrent commands."""
    runtime = LocalFileRuntime()
    service, workspace_id = _service(runtime, tmp_path)
    results = await asyncio.gather(
        *(
            service.exec_harness_command(
                workspace_id,
                [
                    "sh",
                    "-c",
                    'printf "%s" "$1"; printf "%s" "$2" >&2',
                    "test",
                    str(i),
                    f"err-{i}",
                ],
                workdir=str(tmp_path),
            )
            for i in range(8)
        )
    )
    assert results == [(0, str(i), f"err-{i}") for i in range(8)]


async def test_rejected_workdir_never_executes(tmp_path: Path) -> None:
    runtime = AsyncMock()
    service, workspace_id = _service(runtime, tmp_path)
    with pytest.raises(ValueError, match="Invalid workdir"):
        await service.exec_harness_command(workspace_id, "true", workdir="/tmp\nfalse")
    runtime.exec_command_wait.assert_not_called()


async def test_runtime_failure_propagates(tmp_path: Path) -> None:
    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = RuntimeError("transport failed")
    service, workspace_id = _service(runtime, tmp_path)
    with pytest.raises(RuntimeError, match="transport failed"):
        await service.exec_harness_command(workspace_id, "true")


async def test_runtime_cancellation_propagates(tmp_path: Path) -> None:
    entered = asyncio.Event()

    async def wait(*args: object, **kwargs: object) -> tuple[int, str]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    runtime = AsyncMock()
    runtime.exec_command_wait.side_effect = wait
    service, workspace_id = _service(runtime, tmp_path)
    task = asyncio.create_task(service.exec_harness_command(workspace_id, "true"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
