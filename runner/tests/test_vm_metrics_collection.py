from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from src.models import WorkspaceInfo
from src.services.workspace_registry import WorkspaceRegistry


def _registry(usage_by_instance) -> tuple[WorkspaceRegistry, dict[str, uuid.UUID]]:
    async def get_workspace_usage(instance_id: str):
        result = usage_by_instance[instance_id]
        if isinstance(result, Exception):
            raise result
        if asyncio.iscoroutine(result):
            return await result
        return result

    registry = WorkspaceRegistry(
        runtime=SimpleNamespace(get_workspace_usage=get_workspace_usage)
    )
    ids: dict[str, uuid.UUID] = {}
    for instance_id in usage_by_instance:
        workspace_id = uuid.uuid4()
        ids[instance_id] = workspace_id
        registry._cache[workspace_id] = WorkspaceInfo(
            workspace_id=workspace_id, instance_id=instance_id, status="running"
        )
    return registry, ids


async def test_vm_metrics_skip_failed_and_unknown_workspaces() -> None:
    registry, ids = _registry(
        {
            "ok": {"disk_used_bytes": 1},
            "missing": None,
            "broken": RuntimeError("libvirt gone"),
        }
    )

    metrics = await registry.get_vm_metrics()

    assert metrics == {str(ids["ok"]): {"disk_used_bytes": 1}}


async def test_vm_metrics_collect_workspaces_concurrently() -> None:
    release = asyncio.Event()
    started: list[str] = []

    async def blocked(name: str) -> dict[str, int]:
        started.append(name)
        if len(started) == 2:
            release.set()
        await release.wait()
        return {"disk_used_bytes": len(name)}

    registry, ids = _registry({"a": blocked("a"), "bb": blocked("bb")})

    metrics = await asyncio.wait_for(registry.get_vm_metrics(), timeout=1)

    assert metrics == {
        str(ids["a"]): {"disk_used_bytes": 1},
        str(ids["bb"]): {"disk_used_bytes": 2},
    }
