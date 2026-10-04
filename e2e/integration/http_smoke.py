"""Staged HTTP assertions against real ASGI, recovery worker, Redis and runner.

No fake network, inventory, terminal result, or size data. Failures preserve raw
HTTP responses for diagnosis. Secrets live only in a mode-0600 auth artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import structlog
from support import command, load, run_dir, save

log = structlog.get_logger()
BASE = os.environ.get("INTEGRATION_BACKEND_URL", "http://127.0.0.1:8011")


def request(
    method: str,
    path: str,
    payload: Any = None,
    expected: int = 200,
    authenticated: bool = True,
) -> Any:
    """Send an actual HTTP request and assert its exact response status."""
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers.update(
            {
                "Authorization": "Bearer " + load("auth.json")["access_token"],
                "X-Organization-Id": load()["org_id"],
            }
        )
    req = urllib.request.Request(
        BASE + "/api/v1" + path,
        method=method,
        headers=headers,
        data=None if payload is None else json.dumps(payload).encode(),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    body = json.loads(raw) if raw else None
    if path != "/auth/login/":
        save(
            "last-response.json",
            {"method": method, "path": path, "status": status, "body": body},
        )
    assert status == expected, (method, path, expected, status, body)
    return body


async def until(
    get: Callable[[], Any], ready: Callable[[Any], bool], timeout: int = 180
) -> Any:
    """Poll with a deadline; never silently accept incomplete evidence."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await asyncio.to_thread(get)
        if ready(value):
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"Timed out awaiting live evidence: {value}")
        await asyncio.sleep(2)


def login() -> None:
    """Store JWT privately for HTTP and browser use; never log token contents."""
    body = request(
        "POST",
        "/auth/login/",
        {
            "email": "images-admin@example.test",
            "password": os.environ["INTEGRATION_ADMIN_PASSWORD"],
        },
        authenticated=False,
    )
    save("auth.json", body)
    me = request("GET", "/auth/me/")
    assert str(me["id"]) == str(load()["user_id"])
    log.info("integration_authenticated")


async def storage() -> dict:
    """Require newer fresh complete inventory after an explicit refresh."""
    path = f"/runners/{load()['runner_id']}/storage/"
    old = request("GET", path)
    request("POST", path + "refresh/", {}, expected=202)
    result = await until(
        lambda: request("GET", path),
        lambda row: bool(
            row["runner_online"]
            and row["latest_complete"]
            and row["latest_snapshot_id"] != old["latest_snapshot_id"]
            and row["runtimes"]
            and all(r["fresh"] for r in row["runtimes"])
        ),
    )
    save("storage.json", result)
    return result


async def wait_build(expected_generation: str | None = None) -> list:
    """Wait until a real Docker build is active; refuse terminal failures."""
    definition_id = load("build.json")["definition_id"]

    def done(rows: list) -> bool:
        assert not any(r["status"] == "failed" for r in rows), rows
        return bool(
            rows
            and rows[0]["status"] == "active"
            and (
                expected_generation is None
                or rows[0]["current_generation_id"] == expected_generation
            )
        )

    rows = await until(
        lambda: request("GET", f"/image-definitions/{definition_id}/runner-builds/"),
        done,
        timeout=600,
    )
    save("build-result.json", rows)
    return rows


async def build() -> None:
    """HTTP creates recipe/revision/generation and dispatches a tiny real build."""
    definition = request(
        "POST",
        "/image-definitions/",
        {
            "name": "Integration tiny " + load()["run_id"][:8],
            "description": "Live integration fixture, not agent/desktop provisioned",
            "runtime_type": "docker",
            "base_distro": "alpine:3.21",
            "packages": [],
            "custom_dockerfile": "RUN echo generation-one > /fixture",
        },
        expected=201,
    )
    save("build.json", {"definition_id": definition["id"]})
    request(
        "POST",
        f"/image-definitions/{definition['id']}/runner-builds/",
        {
            "runner_id": load()["runner_id"],
            "activate": True,
        },
        expected=202,
    )
    await wait_build()
    await storage()
    log.info("integration_real_build_passed")


async def slow_build() -> None:
    """Stage a unique slow real rebuild and return before runtime completion."""
    import uuid

    definition_id = load("build.json")["definition_id"]
    marker = uuid.uuid4().hex
    # Unique instruction defeats Docker's layer cache on repeated replay tests.
    request(
        "PATCH",
        f"/image-definitions/{definition_id}/",
        {
            "custom_dockerfile": f"RUN sleep 12 && echo {marker} > /fixture",
        },
    )
    row = request(
        "PATCH",
        f"/image-definitions/{definition_id}/runner-builds/{load()['runner_id']}/",
        {"action": "rebuild"},
    )
    save(
        "slow-build.json",
        {
            "definition_id": definition_id,
            "generation_id": row["pending_generation_id"],
            "task_id": row["build_task_id"],
            "marker": marker,
            "response": row,
        },
    )
    assert row["pending_generation_id"] and row["build_task_id"], row
    log.info(
        "integration_slow_build_staged",
        generation_id=row["pending_generation_id"],
        task_id=row["build_task_id"],
    )


async def slow_poll() -> None:
    """Verify staged build converges to its exact generation and journal ack."""
    staged = load("slow-build.json")
    rows = await wait_build(staged["generation_id"])
    assert rows[0]["current_generation_id"] == staged["generation_id"], rows
    assert rows[0]["build_task_id"] == staged["task_id"], rows
    database = run_dir() / "runner-state" / "operations.sqlite3"

    def evidence() -> list:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            return db.execute(
                "SELECT id, event, ack, result IS NOT NULL FROM operations WHERE id=?",
                (staged["task_id"],),
            ).fetchall()

    journal = await until(
        evidence,
        lambda rows: bool(len(rows) == 1 and rows[0][1:] == ("image:built", 1, 1)),
        timeout=60,
    )
    graph = await storage()
    images = [
        r
        for runtime in graph["runtimes"]
        for r in runtime["resources"]
        if r["image_id"] == staged["generation_id"]
    ]
    assert len(images) == 1, images
    physical = images[0]["physical_id"]
    label = command(
        "docker",
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "opencuria.operation-id"}}',
        physical,
    ).strip()
    assert label == staged["task_id"], (label, staged)
    save(
        "slow-build-result.json",
        {"journal": journal, "image": images[0], "build": rows[0]},
    )
    log.info("integration_slow_build_converged", task_id=staged["task_id"], passed=True)


async def verify() -> None:
    """Read-only preview, deferred waiting, cancellation, rebuild and old pin."""
    manifest = load()
    graph = await storage()
    docker = next(r for r in graph["runtimes"] if r["runtime_type"] == "docker")
    resources = docker["resources"]
    actual = next(r for r in resources if r["image_id"] == manifest["image_id"])
    assert actual["physical_id"] == manifest["physical_image_id"], actual
    for workspace_id in manifest["workspace_ids"]:
        resource = next(
            r
            for r in resources
            if r.get("workspace") and r["workspace"]["id"] == workspace_id
        )
        assert actual["physical_id"] in resource["dependencies"], resource
        assert resource["workspace"]["owner_label"], resource
    target = {"target_type": "image", "target_id": manifest["image_id"]}
    preview = request("POST", "/image-artifacts/deletions/preview/", target)
    assert not preview["blockers"], preview
    assert {w["id"] for w in preview["workspaces"]} == set(manifest["workspace_ids"])
    # Invalid force approval must never allocate physical work.
    request(
        "POST",
        "/image-artifacts/deletions/",
        {
            **target,
            "mode": "force",
            "fingerprint": "invalid",
        },
        expected=409,
    )
    deferred = request("POST", "/image-artifacts/deletions/", target)
    save("deferred.json", deferred)
    assert deferred["can_cancel"] and not deferred["children"], deferred
    await asyncio.sleep(6)  # allow independent worker ticks to observe dependency
    status = request("GET", f"/image-artifacts/deletions/{deferred['id']}/")[0]
    assert status["can_cancel"] and not status["children"], status
    cancelled = request(
        "POST", f"/image-artifacts/deletions/{deferred['id']}/cancel/", {}
    )
    assert cancelled["phase"] == "cancelled", cancelled
    command("docker", "image", "inspect", manifest["physical_image_id"])
    # Rebuild a changed immutable recipe; old consumers stay pinned to generation one.
    definition_id = load("build.json")["definition_id"]
    request(
        "PATCH",
        f"/image-definitions/{definition_id}/",
        {
            "custom_dockerfile": "RUN echo generation-two > /fixture",
        },
    )
    request(
        "PATCH",
        f"/image-definitions/{definition_id}/runner-builds/{manifest['runner_id']}/",
        {"action": "rebuild"},
    )
    await wait_build()
    graph = await storage()
    resources = next(r for r in graph["runtimes"] if r["runtime_type"] == "docker")[
        "resources"
    ]
    old = next(r for r in resources if r["image_id"] == manifest["image_id"])
    assert old["physical_id"] == manifest["physical_image_id"]
    assert any(
        r["image_id"] and r["image_id"] != manifest["image_id"] for r in resources
    )
    save("preview.json", preview)
    log.info("integration_preview_deferred_cancel_rebuild_passed")


async def force() -> None:
    """Stage exact graph-approved deletion; polling is separate for restart tests."""
    await storage()
    target = {
        "target_type": "definition",
        "target_id": load("build.json")["definition_id"],
    }
    preview = request("POST", "/image-artifacts/deletions/preview/", target)
    assert not preview["blockers"], preview
    assert {w["id"] for w in preview["workspaces"]} == set(load()["workspace_ids"])
    save("force-preview.json", preview)
    row = request(
        "POST",
        "/image-artifacts/deletions/",
        {
            **target,
            "mode": "force",
            "fingerprint": preview["fingerprint"],
        },
    )
    save("force.json", row)
    log.info("integration_force_staged", deletion_id=row["id"])


async def poll() -> None:
    """Assert terminal DB outcome, physical removal and exactly-once journal rows."""
    deletion_id = load("force.json")["id"]
    row = await until(
        lambda: request("GET", f"/image-artifacts/deletions/{deletion_id}/")[0],
        lambda row: row["phase"] == "completed",
        timeout=300,
    )
    save("force-result.json", row)
    assert row["children"], row
    request(
        "POST", f"/image-artifacts/deletions/{deletion_id}/cancel/", {}, expected=409
    )
    for container in load()["container_ids"]:
        assert not command("docker", "ps", "-aq", "--filter", f"id={container}").strip()
    # Each approved image UUID tag is absent; unrelated base layers are not pruned.
    graph = await storage()
    approved = {i["id"] for i in load("force-preview.json")["images"]}
    assert not any(
        r["image_id"] in approved
        for runtime in graph["runtimes"]
        for r in runtime["resources"]
    )
    database = run_dir() / "runner-state" / "operations.sqlite3"

    def read_journal() -> list:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            return db.execute("SELECT id, event, ack FROM operations").fetchall()

    child_ids = set(row["children"].values())
    evidence = await until(
        read_journal,
        lambda rows: child_ids.issubset({entry[0] for entry in rows if entry[2]}),
        timeout=60,
    )
    save("journal-evidence.json", evidence)
    # UUID primary key enforces one journal execution identity; require terminal ack.
    for child in child_ids:
        matches = [entry for entry in evidence if entry[0] == child]
        assert (
            len(matches) == 1
            and matches[0][1] in {"workspace:removed", "image_artifact:deleted"}
            and matches[0][2] == 1
        ), (child, evidence)
    log.info("integration_force_physical_removal_and_journal_passed")


async def snapshot() -> None:
    """Record sanitized read-only journal evidence, including unacked outcomes."""
    database = run_dir() / "runner-state" / "operations.sqlite3"
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        evidence = db.execute(
            "SELECT id, event, ack, result IS NOT NULL FROM operations ORDER BY id"
        ).fetchall()
    import time

    save(f"journal-snapshot-{time.time_ns()}.json", evidence)
    log.info(
        "integration_journal_snapshot",
        records=len(evidence),
        unacknowledged_terminal=sum(bool(r[3] and not r[2]) for r in evidence),
    )


async def guest_http_pipeline() -> None:
    """Run the real HTTP/worker stop -> capture -> resume pipeline, opt-in only.

    The parent must first attach the recorded guest and restart its own runner
    with guest-env, keeping the live runner-state journal. No monkeypatches.
    """
    state, attached, manifest = load("guest.json"), load("guest-attachment.json"), load()
    assert attached["workspace_id"] == state["workspace_id"]
    wid = state["workspace_id"]
    path = f"/workspaces/{wid}/"
    storage_path = f"/runners/{manifest['runner_id']}/storage/"
    initial = request("GET", path)
    # Parent must HTTP-stop an already resumed guest before rerunning. A
    # controlled stop can legitimately leave credentials absent; never rewrite DB
    # state or accept a running guest to bypass this lifecycle precondition.
    assert initial["status"] == "stopped" and initial["active_operation"] is None, initial
    assert not initial["intervention_required"], initial
    assert initial["created_by_id"] == int(manifest["user_id"]), initial
    assert initial["credential_ids"] == [], initial
    assert initial["runtime_type"] == "qemu", initial
    assert [initial[k] for k in ("qemu_vcpus", "qemu_memory_mb", "qemu_disk_size_gb")] == [1, 1024, 20]
    resumed = request("POST", path + "resume/", {}, expected=202)

    def running(row: dict) -> bool:
        assert not row["intervention_required"], row
        return row["status"] == "running" and row["active_operation"] is None

    first = await until(lambda: request("GET", path), running, timeout=600)
    assert not first["credentials_present"], first
    capture = request("POST", "/image-artifacts/", {
        "workspace_id": wid, "name": "Integration HTTP booted guest capture",
        "stop_and_restart": True,
    }, expected=202)
    save("guest-http-staged.json", {"resume": resumed, "capture": capture,
        "started_at": resumed["created_at"]})
    await guest_http_poll()


async def guest_http_poll() -> None:
    """Read-only verification of the staged invocation; never dispatch new work."""
    state, manifest, staged = load("guest.json"), load(), load("guest-http-staged.json")
    resumed, capture = staged["resume"], staged["capture"]
    wid = state["workspace_id"]
    assert str(capture["workspace_id"]) == wid == str(resumed["workspace_id"])
    path = f"/workspaces/{wid}/"
    storage_path = f"/runners/{manifest['runner_id']}/storage/"

    def running(row: dict) -> bool:
        assert not row["intervention_required"], row
        return row["status"] == "running" and row["active_operation"] is None

    def recover_request_id() -> str:
        # Compatibility with an already staged invocation whose stop child has
        # advanced. Request allocation and its initial stop commit atomically;
        # select the request immediately preceding that exact persisted task.
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
        import django
        django.setup()
        from apps.runners.models import CaptureRequest, Task
        stop = Task.objects.get(pk=capture["task_id"], workspace_id=wid, type="stop_workspace")
        start = Task.objects.get(pk=resumed["id"], workspace_id=wid, type="resume_workspace")
        rows = list(CaptureRequest.objects.filter(workspace_id=wid,
            created_at__gte=start.created_at, created_at__lte=stop.created_at))
        assert len(rows) == 1, "Ambiguous staged capture; refusing to guess request identity"
        return str(rows[0].id)

    allocation = request("GET", storage_path)
    allocated = [r for r in allocation["capture_requests"]
                 if str(r["workspace_id"]) == wid and str(r["child_id"]) == str(capture["task_id"])]
    assert len(allocated) <= 1, allocated
    request_id = staged.get("capture_request_id")
    if request_id is None:
        request_id = str(allocated[0]["id"]) if allocated else await asyncio.to_thread(recover_request_id)
        staged.update(capture_request_id=request_id, started_at=resumed["created_at"])
        save("guest-http-staged.json", staged)
    selected = [r for r in allocation["capture_requests"] if str(r["id"]) == request_id]
    assert len(selected) == 1 and str(selected[0]["workspace_id"]) == wid, selected
    image_id = str(selected[0]["image_id"])

    def completed(graph: dict) -> bool:
        rows = [r for r in graph["capture_requests"] if str(r["id"]) == request_id]
        if not rows:
            return False
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["phase"] != "failed" and not row["diagnostic"], row
        return row["phase"] == "completed"

    graph = await until(lambda: request("GET", storage_path), completed, timeout=900)
    capture_row = next(r for r in graph["capture_requests"] if str(r["id"]) == request_id)
    assert capture_row["prior_running"] and not capture_row["resume_suppressed"], capture_row
    final = await until(lambda: request("GET", path), running, timeout=60)
    assert not final["credentials_present"], final
    image = next(i for i in request("GET", "/image-artifacts/") if str(i["id"]) == image_id)
    assert image["status"] == "ready", image
    # Real durable journal proves all child executions, not just coordinator phase.
    database = run_dir() / "runner-state" / "operations.sqlite3"

    def journal_rows() -> list:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            return db.execute(
                "SELECT id, event, ack, result FROM operations WHERE id IN (?,?) OR result LIKE ?",
                (str(resumed["id"]), str(capture_row["child_id"]), "%" + image_id + "%"),
            ).fetchall()

    journal = await until(journal_rows,
        lambda rows: any(r[0] == str(capture_row["child_id"]) and r[1] == "workspace:resumed" and r[2] for r in rows),
        timeout=60)
    resume_child = next(r for r in journal if r[0] == str(capture_row["child_id"]))
    assert resume_child[1] == "workspace:resumed" and resume_child[2], resume_child[:3]
    # Verify exact persisted children/tasks independently, read-only, with backend Python.
    def persisted() -> dict:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
        import django
        django.setup()
        from apps.runners.models import CaptureRequest, LifecycleCommand, Workspace
        row = CaptureRequest.objects.get(pk=capture_row["id"])
        ws = Workspace.objects.get(pk=wid)
        assert ws.status == "running" and ws.current_task_id is None
        assert row.phase == "completed" and not row.diagnostic
        start = LifecycleCommand.objects.get(task_id=resumed["id"])
        end = LifecycleCommand.objects.get(task_id=row.child_id)
        commands = list(LifecycleCommand.objects.filter(task__workspace_id=wid, created_at__gte=start.created_at, created_at__lte=end.created_at).select_related("task").order_by("created_at"))
        kinds = [c.task.type for c in commands]
        assert kinds == ["resume_workspace", "stop_workspace", "create_image_artifact", "resume_workspace"], kinds
        assert all(c.phase == "result" and c.task.status == "completed" for c in commands), [(c.phase, c.task.status) for c in commands]
        assert str(commands[1].task_id) == str(capture["task_id"])
        assert str(row.image.creating_task_id) == str(commands[2].task_id)
        assert str(row.child_id) == str(commands[-1].task_id)
        return {"workspace_current_task": None, "commands": [
            {"task_id": str(c.task_id), "type": c.task.type, "phase": c.phase,
             "status": c.task.status, "payload": c.payload} for c in commands]}

    db_evidence = await asyncio.to_thread(persisted)
    ids = [c["task_id"] for c in db_evidence["commands"]]
    def exact_journal() -> list:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
            return db.execute("SELECT id, event, ack FROM operations WHERE id IN (?,?,?,?)", ids).fetchall()
    journal = await until(exact_journal, lambda rows: len(rows) == 4 and all(r[2] for r in rows), timeout=60)
    events = {r[0]: r[1] for r in journal}
    assert [events[i] for i in ids] == ["workspace:resumed", "workspace:stopped", "image_artifact:created", "workspace:resumed"], journal
    # Runtime file verification is read-only and checks actual standalone bytes.
    from pathlib import Path
    import uuid
    safe_id = str(uuid.UUID(image_id))
    root = Path(state["snapshot_dir"]).resolve()
    resources = [r for runtime in graph["runtimes"] for r in runtime["resources"]
                 if str(r.get("image_id")) == image_id and r["kind"] == "image"]
    assert len(resources) <= 1, resources
    target = Path(resources[0]["physical_id"]) if resources else root / (safe_id + ".qcow2")
    assert target.is_absolute() and not target.is_symlink()
    assert target.resolve() == root / (safe_id + ".qcow2"), target
    info = json.loads(command("qemu-img", "info", "--output=json", str(target)))
    assert not info.get("backing-filename") and not info.get("full-backing-filename"), info
    command("qemu-img", "check", str(target))
    save("guest-http-result.json", {"workspace": final, "image": image,
        "capture_request": capture_row, "persisted": db_evidence,
        "journal": [r[:3] for r in journal], "qemu_info": info,
        "coverage": "real HTTP + independent worker + booted QEMU guest", "passed": True})
    log.info("integration_guest_http_pipeline_passed", workspace_id=wid, image_id=image_id)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "login",
            "storage",
            "build",
            "verify",
            "force",
            "poll",
            "snapshot",
            "slow-build",
            "slow-poll",
            "guest-http-pipeline",
            "guest-http-poll",
        ],
    )
    action = parser.parse_args().action
    if action == "login":
        login()
    else:
        asyncio.run(
            {
                "storage": storage,
                "build": build,
                "verify": verify,
                "force": force,
                "poll": poll,
                "snapshot": snapshot,
                "slow-build": slow_build,
                "slow-poll": slow_poll,
                "guest-http-pipeline": guest_http_pipeline,
                "guest-http-poll": guest_http_poll,
            }[action]()
        )
