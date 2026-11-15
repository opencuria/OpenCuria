import asyncio
import tempfile
import unittest
import uuid
from unittest.mock import AsyncMock

from src.config import RunnerSettings
from src.models import WorkspaceInfo
from src.service import WorkspaceService


class _Runtime:
    def __init__(self):
        self.exec_command_wait = AsyncMock(return_value=(0, "snapshot"))


class DesktopClipboardSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = _Runtime()
        self.state_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.state_dir.cleanup)
        self.service = WorkspaceService(
            self.runtime,
            RunnerSettings(state_dir=self.state_dir.name),
        )
        self.service.configure_desktop_leases(self.state_dir.name, "clipboard-epoch")
        self.workspace_id = uuid.uuid4()
        self.service.registry._cache[self.workspace_id] = WorkspaceInfo(
            self.workspace_id,
            "guest",
            "running",
        )
        self.service.desktop._is_desktop_session_live = AsyncMock(return_value=True)
        self.service.desktop._exec_desktop_shell = AsyncMock(
            return_value=(0, "fresh unicode 🧪")
        )

    async def test_default_read_preserves_existing_snapshot_operation(self):
        result = await self.service.desktop.read_desktop_clipboard(self.workspace_id)
        self.assertEqual(result, "snapshot")
        args = self.runtime.exec_command_wait.await_args.args
        self.assertIn("xclip -selection clipboard -o", args[1][2])
        self.service.desktop._exec_desktop_shell.assert_not_awaited()

    async def test_selection_copy_uses_bounded_local_python_and_returns_fresh_text(self):
        result = await self.service.desktop.read_desktop_clipboard(
            self.workspace_id, copy_selection=True, shift=True
        )
        self.assertEqual(result, "fresh unicode 🧪")
        self.service.desktop._exec_desktop_shell.assert_awaited_once()
        command = self.service.desktop._exec_desktop_shell.await_args.args[1]
        self.assertIn("timeout --signal=TERM 2.5s python3 -c", command)
        self.assertTrue(command.endswith(" shift"))
        self.assertNotIn("apt-get", command)

    async def test_invalid_argument_types_and_shift_without_copy_fail_closed(self):
        for kwargs in (
            {"copy_selection": 1},
            {"copy_selection": True, "shift": 1},
            {"shift": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.service.desktop.read_desktop_clipboard(self.workspace_id, **kwargs)
        self.service.desktop._exec_desktop_shell.assert_not_awaited()

    async def test_unactivated_agent_and_computeruse_leases_block_keyboard(self):
        for kind in ("agent", "computeruse"):
            with self.subTest(kind=kind):
                await self.service.desktop.lease_store.reserve(
                    str(self.workspace_id),
                    "guest",
                    f"{kind}-lease",
                    kind,
                    f"{kind}-run",
                    "epoch",
                    1,
                    180,
                )
                with self.assertRaisesRegex(RuntimeError, "during computer use"):
                    await self.service.desktop.read_desktop_clipboard(
                        self.workspace_id, copy_selection=True
                    )
        self.service.desktop._exec_desktop_shell.assert_not_awaited()

    async def test_guest_failure_and_outer_timeout_are_generic(self):
        self.service.desktop._exec_desktop_shell.return_value = (
            1,
            "previous clipboard secret",
        )
        with self.assertRaisesRegex(
            RuntimeError, "Failed to copy desktop selection"
        ) as exc:
            await self.service.desktop.read_desktop_clipboard(
                self.workspace_id, copy_selection=True
            )
        self.assertNotIn("secret", str(exc.exception))

        async def hang(*args):
            await asyncio.sleep(4)

        self.service.desktop._exec_desktop_shell.side_effect = hang
        with self.assertRaisesRegex(
            RuntimeError, "Failed to copy desktop selection"
        ):
            await self.service.desktop.read_desktop_clipboard(
                self.workspace_id, copy_selection=True
            )

    async def test_capture_admission_rejects_copy_without_keyboard_input(self):
        self.service.capture_fence._capturing.add(self.workspace_id)
        try:
            with self.assertRaisesRegex(RuntimeError, "capturing"):
                await self.service.desktop.read_desktop_clipboard(
                    self.workspace_id, copy_selection=True
                )
        finally:
            self.service.capture_fence._capturing.discard(self.workspace_id)
        self.service.desktop._exec_desktop_shell.assert_not_awaited()
