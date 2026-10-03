"""Deterministic inventory/lifecycle interleavings using real service + handlers."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import RunnerSettings
from src.interfaces.websocket import WebSocketInterface
from src.models import WorkspaceInfo
from src.runtime.base import RuntimeWorkspaceInfo
from src.service import WorkspaceService


class BarrierRuntime:
    supports_image_artifacts = True

    def __init__(self, workspace_id):
        self.workspace_id = workspace_id
        self.instance_id = "original"
        self.state = "stopped"
        self.exists = True
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.scan_entered = asyncio.Event()
        self.scan_release = asyncio.Event()
        self.hold_scan = False
        self.scrubs = []

    async def list_workspaces(self):
        snapshot = (
            [
                RuntimeWorkspaceInfo(
                    workspace_id=str(self.workspace_id),
                    instance_id=self.instance_id,
                    status=self.state,
                    name="guest",
                )
            ]
            if self.exists
            else []
        )
        if self.hold_scan:
            self.scan_entered.set()
            await self.scan_release.wait()
        return snapshot

    async def get_workspace_status(self, instance_id):
        assert instance_id == self.instance_id
        return SimpleNamespace(status=self.state)

    async def workspace_incarnation(self, workspace_id):
        return (
            {"instance_id": self.instance_id, "metadata": {"disk": "exact"}},
            self.state,
        )

    async def reconfigure_workspace(self, *args, **kwargs):
        pass

    async def start_workspace(self, instance_id):
        self.entered.set()
        await self.release.wait()
        self.state = "running"

    async def stop_workspace(self, instance_id):
        self.entered.set()
        await self.release.wait()
        self.state = "stopped"

    async def create_workspace(self, config):
        self.entered.set()
        await self.release.wait()
        self.exists = True
        self.state = "running"
        return self.instance_id

    async def create_workspace_from_image_artifact(
        self, artifact, workspace_id, **kwargs
    ):
        return await self.create_workspace(None)

    async def exec_command_wait(self, instance_id, **kwargs):
        # Real credential manager scrub path, not lifecycle-hook bypass.
        assert self.state == "running"
        self.scrubs.append(instance_id)
        return 0, ""


def setup(tmp_path):
    ws_id = uuid.uuid4()
    runtime = BarrierRuntime(ws_id)
    settings = RunnerSettings(state_dir=str(tmp_path))
    service = WorkspaceService({"qemu": runtime}, settings)
    service._cache[ws_id] = WorkspaceInfo(
        ws_id, runtime.instance_id, "stopped", runtime_type="qemu"
    )
    interface = WebSocketInterface(service, settings)
    interface._operations.emit = AsyncMock()
    return ws_id, runtime, service, interface


def command(ws_id, task_id):
    return dict(
        task_id=task_id,
        operation_id=task_id,
        attempt=1,
        workspace_id=str(ws_id),
        runner_id="runner",
        target="workspace",
        qemu_vcpus=1,
        qemu_memory_mb=512,
        qemu_disk_size_gb=10,
    )


@pytest.mark.parametrize("operation", ["resume", "stop", "create"])
async def test_sync_during_real_handler_and_immediate_next_stop(tmp_path, operation):
    ws_id, runtime, service, interface = setup(tmp_path)
    if operation == "stop":
        runtime.state = "running"
        service._cache[ws_id].status = "running"
    if operation == "create":
        runtime.exists = False
        service._cache.clear()
    data = command(ws_id, operation)
    if operation == "create":
        data.update(
            runtime_type="qemu", base_image_path="/managed/base.qcow2", repos=[]
        )
    task = asyncio.create_task(
        interface._sio.handlers["/"][f"task:{operation}_workspace"](data)
    )
    await asyncio.wait_for(runtime.entered.wait(), 3)
    canonical = service._get_cached(ws_id)
    cache = service._cache
    await service.sync_from_runtime()
    assert service.registry.get_cached(ws_id) is canonical
    assert service._cache is cache
    runtime.release.set()
    await asyncio.wait_for(task, 3)
    assert (
        interface._operations.journal.pending(operation)[0][0]
        == {
            "resume": "workspace:resumed",
            "stop": "workspace:stopped",
            "create": "workspace:created",
        }[operation]
    )
    assert service._get_cached(ws_id) is canonical
    assert canonical.status == ("exited" if operation == "stop" else "running")
    assert canonical.credentials_present is False
    await interface._sio.handlers["/"]["task:stop_workspace"](
        command(ws_id, "next-stop")
    )
    assert (
        interface._operations.journal.pending("next-stop")[0][0] == "workspace:stopped"
    )
    assert runtime.state == "stopped"
    interface._operations.journal.close()


@pytest.mark.parametrize("operation", ["resume", "stop", "create"])
async def test_snapshot_started_before_operation_cannot_overwrite_completion(
    tmp_path, operation
):
    ws_id, runtime, service, interface = setup(tmp_path)
    if operation == "stop":
        runtime.state = "running"
        service._cache[ws_id].status = "running"
    if operation == "create":
        runtime.exists = False
        service._cache.clear()
    runtime.hold_scan = True
    refresh = asyncio.create_task(service.sync_from_runtime())
    await runtime.scan_entered.wait()
    runtime.release.set()
    if operation == "create":
        await service.create_workspace(
            [], workspace_id=ws_id, runtime_type="qemu", base_image_path="/base"
        )
    elif operation == "resume":
        await service.resume_workspace(
            ws_id, qemu_vcpus=1, qemu_memory_mb=512, qemu_disk_size_gb=10
        )
    else:
        await service.stop_workspace(ws_id)
    canonical = service._get_cached(ws_id)
    runtime.scan_release.set()
    await refresh
    assert service._get_cached(ws_id) is canonical
    assert canonical.status == ("exited" if operation == "stop" else "running")
    await service.stop_workspace(ws_id)
    interface._operations.journal.close()


async def test_same_instance_merge_preserves_credentials_different_instance_does_not(
    tmp_path,
):
    ws_id, runtime, service, interface = setup(tmp_path)
    canonical = service._get_cached(ws_id)
    canonical.credentials_present = False
    await interface._checkpoint_workspace(ws_id, False, "scrubbed")
    await service.sync_from_runtime()
    assert service._get_cached(ws_id) is canonical
    assert canonical.credentials_present is False
    runtime.instance_id = "replacement"
    await service.sync_from_runtime()
    assert service._get_cached(ws_id) is not canonical
    assert service._get_cached(ws_id).credentials_present is None
    assert not await interface._workspace_scrub_proof(ws_id)
    with pytest.raises(RuntimeError, match="no exact scrub proof"):
        await service.stop_workspace(ws_id)
    interface._operations.journal.close()


async def test_live_running_status_overrides_stale_stopped_cache(tmp_path):
    ws_id, runtime, service, interface = setup(tmp_path)
    runtime.state = "running"
    runtime.release.set()
    await service.stop_workspace(ws_id)
    assert runtime.scrubs
    assert runtime.state == "stopped"
    interface._operations.journal.close()


async def test_failed_live_observation_never_scrubs_or_stops(tmp_path):
    ws_id, runtime, service, interface = setup(tmp_path)
    runtime.get_workspace_status = AsyncMock(side_effect=OSError("unreachable"))
    await interface._sio.handlers["/"]["task:stop_workspace"](command(ws_id, "failed"))
    event, result = interface._operations.journal.pending("failed")[0]
    assert event == "workspace:error"
    assert result["outcome_known"] is True  # real handler caught a known failed stop
    assert not runtime.scrubs
    assert not runtime.entered.is_set()
    interface._operations.journal.close()


async def test_clone_preserves_creating_entry_and_identity_during_inventory(tmp_path):
    ws_id, runtime, service, interface = setup(tmp_path)
    runtime.exists = False
    service._cache.clear()
    task = asyncio.create_task(
        service.images.create_workspace_from_image_artifact("base", ws_id, "qemu")
    )
    await runtime.entered.wait()
    canonical = service._get_cached(ws_id)
    assert canonical.status == "creating"
    await service.sync_from_runtime()
    assert service._get_cached(ws_id) is canonical
    runtime.release.set()
    await task
    assert service._get_cached(ws_id) is canonical
    assert canonical.status == "running"
    assert canonical.credentials_present is False
    await service.stop_workspace(ws_id)
    interface._operations.journal.close()


async def test_failed_refresh_and_failed_resume_keep_unknown_credentials(tmp_path):
    ws_id, runtime, service, interface = setup(tmp_path)
    canonical = service._get_cached(ws_id)
    runtime.list_workspaces = AsyncMock(side_effect=OSError("inventory unavailable"))
    with pytest.raises(OSError):
        await service.sync_from_runtime()
    assert service._get_cached(ws_id) is canonical
    canonical.credentials_present = False
    runtime.start_workspace = AsyncMock(side_effect=OSError("boot uncertain"))
    with pytest.raises(OSError):
        await service.resume_workspace(
            ws_id, qemu_vcpus=1, qemu_memory_mb=512, qemu_disk_size_gb=10
        )
    assert canonical.credentials_present is None
    assert ws_id not in service.registry._active
    interface._operations.journal.close()


async def test_incomplete_create_failure_is_preserved_for_recovery(tmp_path):
    ws_id, runtime, service, interface = setup(tmp_path)
    service._cache.clear()
    runtime.exists = False
    runtime.create_workspace = AsyncMock(side_effect=OSError("creation uncertain"))
    with pytest.raises(OSError):
        await service.create_workspace(
            [], workspace_id=ws_id, runtime_type="qemu", base_image_path="/base"
        )
    await service.sync_from_runtime()
    assert service._get_cached(ws_id).status == "creating"
    assert service._get_cached(ws_id).credentials_present is None
    interface._operations.journal.close()
