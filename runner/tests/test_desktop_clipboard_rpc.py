import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface


class _Desktop:
    def __init__(self):
        self.read_desktop_clipboard = AsyncMock(return_value="fresh")


class _Service:
    def __init__(self):
        self.desktop = _Desktop()


class ClipboardReadRpcTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = _Service()
        self.interface = WebSocketInterface(
            self.service, RunnerSettings(state_dir=tempfile.mkdtemp())
        )
        self.handler = self.interface._sio.handlers["/"]["desktop:clipboard_read"]
        self.workspace_id = uuid.uuid4()

    async def test_legacy_read_passes_no_new_operation_options(self):
        result = await self.handler({"workspace_id": str(self.workspace_id)})
        self.assertEqual(result, {"ok": True, "text": "fresh"})
        self.service.desktop.read_desktop_clipboard.assert_awaited_once_with(
            self.workspace_id
        )

    async def test_selection_options_are_strict_and_shift_requires_copy(self):
        result = await self.handler(
            {
                "workspace_id": str(self.workspace_id),
                "copy_selection": True,
                "shift": True,
            }
        )
        self.assertEqual(result, {"ok": True, "text": "fresh"})
        self.service.desktop.read_desktop_clipboard.assert_awaited_once_with(
            self.workspace_id, copy_selection=True, shift=True
        )

        for extra in (
            {"copy_selection": 1},
            {"copy_selection": True, "shift": "true"},
            {"shift": True},
        ):
            self.service.desktop.read_desktop_clipboard.reset_mock()
            result = await self.handler(
                {"workspace_id": str(self.workspace_id), **extra}
            )
            self.assertFalse(result["ok"])
            self.service.desktop.read_desktop_clipboard.assert_not_awaited()
