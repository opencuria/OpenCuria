"""Opt-in real Docker canonical data-volume lifecycle; no guest tooling needed."""

import asyncio
import os
import sys
import uuid
from pathlib import Path

from support import save

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runner"))
from src.runtime.base import WorkspaceConfig
from src.runtime.docker_runtime import DockerRuntime


async def main() -> None:
    """Keep failure evidence; never prune or delete another run's objects."""
    rt = DockerRuntime()
    wid = str(uuid.uuid4())
    name = f"opencuria-workspace-{wid}"
    image = os.environ.get("INTEGRATION_DOCKER_IMAGE", "alpine:3.21")
    save("docker-volume.json", {"workspace_id": wid, "volume": name})
    # Image must already exist. Normal runtime CMD + tty keeps Alpine alive.
    cid = await rt.create_workspace(
        WorkspaceConfig(wid, image, {}, {name: {"bind": "/workspace", "mode": "rw"}})
    )
    code, _ = await rt.exec_command_wait(
        cid, ["sh", "-c", "echo volume-proof > /workspace/proof"]
    )
    assert code == 0
    client = rt._get_client()
    assert client.volumes.get(name).attrs["Labels"] == rt._volume_labels(wid)
    scan = await rt.inventory()
    assert scan.complete, scan.errors
    observed = next(r for r in scan.resources if r.resource_id == "volume:" + name)
    assert observed.managed and observed.metadata["workspace_id"] == wid
    assert observed.logical_bytes is not None and observed.logical_bytes > 0
    # A stopped foreign consumer still blocks before container destruction.
    peer = await asyncio.to_thread(
        client.containers.create, image, volumes={name: {"bind": "/data", "mode": "ro"}}
    )
    try:
        try:
            await rt.remove_workspace(wid)
        except RuntimeError as error:
            assert "external consumers" in str(error)
        else:
            raise AssertionError("Shared volume deletion accepted")
        assert client.containers.get(cid)
    finally:
        await asyncio.to_thread(peer.remove)
    await rt.remove_workspace(wid)
    # Stable UUID repeat proves complete absence (volume and network too).
    await rt.remove_workspace(wid)
    save(
        "docker-volume-result.json",
        {
            "workspace_id": wid,
            "complete": True,
            "observed_bytes": observed.logical_bytes,
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
