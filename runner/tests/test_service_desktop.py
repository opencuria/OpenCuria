import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.models import DesktopSession, WorkspaceInfo
from src.service import WorkspaceService


class DummyRuntime:
    def __init__(self) -> None:
        self.exec_command_wait = AsyncMock()

    def get_container_ip(self, instance_id: str, workspace_id: str) -> str:
        return "172.22.0.2"

    def get_workspace_network_name(self, workspace_id: str) -> str:
        return f"opencuria-ws-{workspace_id}"


class WorkspaceServiceDesktopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = DummyRuntime()
        self.service = WorkspaceService(
            runtimes={"docker": self.runtime},
            settings=RunnerSettings(),
        )
        self.workspace_id = uuid.uuid4()
        self.service._cache[self.workspace_id] = WorkspaceInfo(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
            status="running",
            runtime_type="docker",
        )

    async def test_start_desktop_restarts_stale_cached_session(self) -> None:
        self.service._desktop_sessions[self.workspace_id] = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
        )
        self.runtime.exec_command_wait.side_effect = [
            (1, "dead"),
            (0, "started"),
        ]

        session = await self.service.start_desktop(self.workspace_id)

        self.assertEqual(session.workspace_id, self.workspace_id)
        self.assertEqual(self.runtime.exec_command_wait.await_count, 2)
        self.assertIs(self.service._desktop_sessions[self.workspace_id], session)

    async def test_heartbeat_payload_prunes_stale_desktop_sessions(self) -> None:
        self.service._desktop_sessions[self.workspace_id] = DesktopSession(
            workspace_id=self.workspace_id,
            instance_id="instance-1",
        )
        self.runtime.exec_command_wait.return_value = (1, "dead")

        payload = await self.service.get_workspace_heartbeat_statuses()

        self.assertEqual(
            payload,
            [
                {
                    "workspace_id": str(self.workspace_id),
                    "status": "running",
                    "runtime_type": "docker",
                }
            ],
        )
        self.assertNotIn(self.workspace_id, self.service._desktop_sessions)
