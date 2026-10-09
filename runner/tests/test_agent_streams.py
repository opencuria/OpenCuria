"""Agent and MCP process ownership have identical managed stream constraints."""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.runtime.base import ProcessHandle
from src.services.sessions.streams import StreamManager


class Runtime:
    runtime_type = "docker"
    supports_managed_process = True

    def __init__(self):
        self.spawn_process = AsyncMock(return_value=ProcessHandle("guest", object()))
        self.probe_managed_token = AsyncMock(
            return_value={"boot_id": "boot", "init_starttime": "1"}
        )
        self.close_managed_process = AsyncMock(return_value=True)
        self.process_close = AsyncMock(return_value=True)
        self.process_write = AsyncMock()
        self.process_write_eof = AsyncMock()


class LeaseStore:
    def __init__(self, kind):
        self.lease = {
            "kind": kind,
            "epoch": "epoch",
            "expires_at": time.time() + 180,
            "state": "reserved",
            "workspace_id": "00000000-0000-0000-0000-000000000001",
            "instance_id": "guest",
        }

    async def get(self, _lease_id):
        return dict(self.lease)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["mcp", "agent"])
async def test_managed_stream_accepts_mcp_and_agent_leases(tmp_path, kind):
    workspace = uuid.UUID("00000000-0000-0000-0000-000000000001")
    runtime = Runtime()
    manager = StreamManager(
        get_cached=lambda _: SimpleNamespace(instance_id="guest"),
        get_runtime=lambda _: runtime,
        state_dir=tmp_path,
        lease_store=LeaseStore(kind),
        epoch="epoch",
    )

    session = await manager.stream_start_process(
        workspace,
        "agent-stream",
        ["/opt/opencuria/runtimes/claude-agent/2.1.292/claude", "--version"],
        owner={"lease_id": "lease", "epoch": "epoch"},
    )

    assert session.owner == {"lease_id": "lease", "epoch": "epoch"}
    runtime.spawn_process.assert_awaited_once()
    await manager.close_all_streams()
