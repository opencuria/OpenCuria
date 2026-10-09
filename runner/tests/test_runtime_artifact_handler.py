"""Workspace artifact ensure RPC correlation and sanitized errors."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface


class DummyService:
    def __init__(self):
        self.supported_runtimes = []
        self.ensure_runtime_artifact = AsyncMock(
            return_value={
                "ok": True,
                "path": "/opt/opencuria/runtimes/claude-agent/2.1.292/claude",
                "version": "2.1.292",
                "platform": "linux-x64",
            }
        )


class ArtifactEnsureHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = DummyService()
        self.interface = WebSocketInterface(
            self.service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        self.interface._sio.emit = AsyncMock()
        self.handler = self.interface._sio.handlers["/"]["workspace:artifact_ensure"]
        self.workspace_id = str(uuid.uuid4())

    async def test_emits_correlated_result_for_approved_artifact(self):
        request = {
            "workspace_id": self.workspace_id,
            "request_id": "agent-1",
            "artifact_id": "claude-agent",
            "version": "2.1.292",
        }

        result = await self.handler(request)

        self.service.ensure_runtime_artifact.assert_awaited_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["workspace_id"], self.workspace_id)
        self.assertEqual(result["request_id"], "agent-1")
        self.interface._sio.emit.assert_awaited_once_with(
            "workspace:artifact_ensure_result", result
        )

    async def test_failure_reply_does_not_leak_runtime_output(self):
        self.service.ensure_runtime_artifact.side_effect = RuntimeError(
            "PRIVATE GUEST STDERR"
        )

        result = await self.handler(
            {
                "workspace_id": self.workspace_id,
                "request_id": "agent-failed",
                "artifact_id": "claude-agent",
                "version": "2.1.292",
            }
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "Runtime artifact provisioning failed")
        self.assertNotIn("PRIVATE", str(result))
        self.interface._sio.emit.assert_awaited_once_with(
            "workspace:artifact_ensure_result", result
        )
