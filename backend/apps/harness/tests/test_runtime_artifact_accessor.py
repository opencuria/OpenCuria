"""Runner RPC contracts for pinned runtime artifact provisioning."""

from __future__ import annotations

import asyncio
import re

import pytest

from apps.harness.access.runner_accessor import (
    _ACCESSORS_BY_REQUEST,
    RunnerAccessorError,
    RunnerWorkspaceAccessor,
    route_harness_result,
)


class _ArtifactTransport:
    """Record accessor emits and optionally deliver a fake runner result."""

    def __init__(self) -> None:
        self.emitted: list[tuple[str, dict]] = []
        self.on_emit = None
        self.emitted_event = asyncio.Event()

    async def __call__(self, event: str, payload: dict) -> None:
        self.emitted.append((event, payload))
        self.emitted_event.set()
        if self.on_emit is not None:
            await self.on_emit(event, payload)


@pytest.mark.asyncio
async def test_ensure_runtime_artifact_emits_correlated_runner_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _ArtifactTransport()
    accessor = RunnerWorkspaceAccessor("workspace-owned", emit=transport)
    original_await_result = accessor._await_result
    waits: list[tuple[str, float | None]] = []

    async def record_wait(request_id, event, payload, timeout):  # type: ignore[no-untyped-def]
        waits.append((event, timeout))
        return await original_await_result(request_id, event, payload, timeout)

    monkeypatch.setattr(accessor, "_await_result", record_wait)

    async def reply(event: str, payload: dict) -> None:
        assert event == "workspace:artifact_ensure"
        assert route_harness_result(
            {
                **payload,
                "ok": True,
                "path": "/opt/opencuria/runtimes/claude-agent/2.1.292/claude",
                "version": "2.1.292",
                "platform": "linux-x64",
            }
        )

    transport.on_emit = reply
    result = await accessor.ensure_runtime_artifact("claude-agent", "2.1.292")

    event, payload = transport.emitted[0]
    assert event == "workspace:artifact_ensure"
    assert payload == {
        "workspace_id": "workspace-owned",
        "request_id": payload["request_id"],
        "artifact_id": "claude-agent",
        "version": "2.1.292",
    }
    assert re.fullmatch(r"[0-9a-f]{32}", payload["request_id"])
    assert waits == [("workspace:artifact_ensure", 600.0)]
    assert result["ok"] is True
    assert result["version"] == "2.1.292"
    assert result["path"] == ("/opt/opencuria/runtimes/claude-agent/2.1.292/claude")
    assert payload["request_id"] not in accessor._pending
    assert payload["request_id"] not in _ACCESSORS_BY_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"ok": False, "error": "runtime denied"}, "runtime denied"),
        ({"ok": False, "version": "2.1.292"}, "not confirmed"),
        ({"ok": True, "version": "wrong-version"}, "not confirmed"),
        ({"version": "2.1.292"}, "not confirmed"),
    ],
)
async def test_ensure_runtime_artifact_rejects_unconfirmed_reply(
    reply: dict, message: str
) -> None:
    transport = _ArtifactTransport()
    accessor = RunnerWorkspaceAccessor("workspace-owned", emit=transport)

    async def answer(_event: str, payload: dict) -> None:
        assert route_harness_result({**payload, **reply})

    transport.on_emit = answer
    with pytest.raises(RunnerAccessorError, match=message):
        await accessor.ensure_runtime_artifact("claude-agent", "2.1.292")

    request_id = transport.emitted[0][1]["request_id"]
    assert request_id not in accessor._pending
    assert request_id not in _ACCESSORS_BY_REQUEST


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact_id", "version"),
    [
        ("", "2.1.292"),
        ("claude-agent", ""),
        ("a" * 65, "2.1.292"),
        ("claude-agent", "v" * 65),
        (None, "2.1.292"),
    ],
)
async def test_ensure_runtime_artifact_rejects_invalid_identity(
    artifact_id: str | None, version: str
) -> None:
    transport = _ArtifactTransport()
    accessor = RunnerWorkspaceAccessor("workspace-owned", emit=transport)

    with pytest.raises(ValueError, match="Invalid runtime artifact identity"):
        await accessor.ensure_runtime_artifact(artifact_id, version)  # type: ignore[arg-type]
    assert transport.emitted == []


@pytest.mark.asyncio
async def test_cancelling_artifact_request_emits_cancel_and_unregisters() -> None:
    transport = _ArtifactTransport()
    accessor = RunnerWorkspaceAccessor("workspace-owned", emit=transport)
    task = asyncio.create_task(
        accessor.ensure_runtime_artifact("claude-agent", "2.1.292")
    )

    await transport.emitted_event.wait()
    request_id = transport.emitted[0][1]["request_id"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event for event, _ in transport.emitted] == [
        "workspace:artifact_ensure",
        "harness:cancel",
    ]
    assert transport.emitted[-1][1] == {
        "request_id": request_id,
        "workspace_id": "workspace-owned",
    }
    assert request_id not in accessor._pending
    assert request_id not in _ACCESSORS_BY_REQUEST
    assert not route_harness_result(
        {"request_id": request_id, "workspace_id": "workspace-owned", "ok": True}
    )
