"""Real HTTP and Socket.IO smoke against the isolated mobile backend fixture."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any

import requests
import socketio

UPLOAD_BYTES = b"OpenCuria mobile fixture upload\n" + b"x" * 420_000
UPLOAD_FILENAME = "socket-chunked-upload.txt"
UPLOAD_DIRECTORY = "/workspace/.opencuria/user-uploaded"


def _manifest() -> dict[str, Any]:
    """Load this run's private fixture identifiers."""
    return json.loads(
        (Path(os.environ["MOBILE_E2E_RUN_DIR"]) / "manifest.json").read_text()
    )


async def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    """Keep blocking HTTP requests off the Socket.IO event loop."""
    return await asyncio.to_thread(requests.request, method, url, **kwargs)


async def _wait_event(
    events: list[dict[str, Any]],
    predicate: Any,
    description: str,
    *,
    start: int = 0,
    timeout: float = 30,
) -> dict[str, Any]:
    """Wait without blocking the Socket.IO event receiver."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        for row in events[start:]:
            if predicate(row):
                return row
        await asyncio.sleep(0.05)
    recent = [
        {
            "event": row.get("event"),
            "request_id": row.get("data", {}).get("request_id"),
            "index": row.get("data", {}).get("index"),
            "status": row.get("data", {}).get("status"),
            "error": row.get("data", {}).get("error"),
        }
        for row in events[start:]
    ]
    raise AssertionError(
        f"Timed out waiting for {description}; recent_events={recent!r}"
    )


class FrontendProbe:
    """JWT-authenticated frontend client with correlated file/harness events."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.client = socketio.AsyncClient(reconnection=False)
        self.events: list[dict[str, Any]] = []
        self.file_events: list[dict[str, Any]] = []
        self._register_handlers()

    def _register_handlers(self) -> None:
        for event in (
            "harness.part_updated",
            "harness.session_status",
            "harness.permission_required",
            "harness.question_required",
        ):

            async def collect(data: dict[str, Any], name: str = event) -> None:
                self.events.append({"event": name, "data": data})

            self.client.on(event, handler=collect, namespace="/frontend")
        for event in (
            "files:list_result",
            "files:find_result",
            "files:content_result",
            "files:content_chunk",
            "files:download_result",
            "files:download_chunk",
            "files:upload_result",
        ):

            async def collect_file(data: dict[str, Any], name: str = event) -> None:
                self.file_events.append({"event": name, "data": data})

            self.client.on(event, handler=collect_file, namespace="/frontend")

    async def connect(self, workspace_ids: list[str], token: str) -> None:
        """Connect and subscribe the JWT-authenticated frontend client."""
        await self.client.connect(
            self.base + "/frontend",
            socketio_path="ws/runner",
            namespaces=["/frontend"],
            transports=["websocket"],
            auth={"token": token},
            wait_timeout=10,
        )
        for workspace_id in workspace_ids:
            await self.client.emit(
                "frontend:subscribe_workspace",
                {"workspace_id": workspace_id},
                namespace="/frontend",
            )
        await asyncio.sleep(0.1)

    async def _file_request(
        self,
        event: str,
        result_event: str,
        workspace_id: str,
        path: str,
        **payload: Any,
    ) -> dict[str, Any]:
        request_id = "mobile-file-" + uuid.uuid4().hex
        start = len(self.file_events)
        await self.client.emit(
            event,
            {
                "workspace_id": workspace_id,
                "request_id": request_id,
                "path": path,
                **payload,
            },
            namespace="/frontend",
        )
        row = await _wait_event(
            self.file_events,
            lambda item: (
                item["event"] == result_event
                and item["data"].get("request_id") == request_id
            ),
            result_event,
            start=start,
            timeout=20,
        )
        result = row["data"]
        assert "error" not in result, result
        return result

    async def list_files(self, workspace_id: str, path: str) -> dict[str, Any]:
        """Request `files:list_result.entries` through the true relay."""
        return await self._file_request(
            "frontend:files_list", "files:list_result", workspace_id, path
        )

    async def find_files(self, workspace_id: str, query: str) -> dict[str, Any]:
        """Preserve backend file-search DTOs (`paths:[{name,path}]`)."""
        return await self._file_request(
            "frontend:files_find",
            "files:find_result",
            workspace_id,
            "/workspace",
            query=query,
            limit=50,
        )

    async def upload_file(
        self,
        workspace_id: str,
        *,
        directory: str,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        """Use real frontend upload relay and verify its persisted result."""
        request_id = "mobile-upload-" + uuid.uuid4().hex
        encoded = base64.b64encode(content).decode("ascii")
        chunk_size = 256 * 1024
        chunks = [
            encoded[i : i + chunk_size] for i in range(0, len(encoded), chunk_size)
        ]
        frontend_start = len(self.file_events)
        common = {
            "workspace_id": workspace_id,
            "request_id": request_id,
            "path": directory,
        }
        await self.client.emit(
            "frontend:files_upload_start",
            {
                **common,
                "filename": filename,
                "is_directory": False,
                "chunked": True,
                "total_chunks": len(chunks),
            },
            namespace="/frontend",
        )
        await asyncio.sleep(0.1)
        for index, piece in enumerate(chunks):
            await self.client.emit(
                "frontend:files_upload_chunk",
                {
                    **common,
                    "index": index,
                    "total_chunks": len(chunks),
                    "content": piece,
                },
                namespace="/frontend",
            )
        await self.client.emit(
            "frontend:files_upload_finish",
            {**common, "total_chunks": len(chunks)},
            namespace="/frontend",
        )
        result_row = await _wait_event(
            self.file_events,
            lambda item: (
                item["event"] == "files:upload_result"
                and item["data"].get("request_id") == request_id
            ),
            "frontend upload result relay",
            start=frontend_start,
            timeout=15,
        )
        result = result_row["data"]
        assert result.get("status") == "success", result
        return {"request_id": request_id, "chunks": len(chunks), **result}

    async def read_bytes(self, workspace_id: str, path: str) -> dict[str, Any]:
        """Validate actual runner bytes using the client-compatible read shape."""
        request_id = "mobile-read-" + uuid.uuid4().hex
        start = len(self.file_events)
        await self.client.emit(
            "frontend:files_read",
            {"workspace_id": workspace_id, "request_id": request_id, "path": path},
            namespace="/frontend",
        )
        row = await _wait_event(
            self.file_events,
            lambda item: (
                item["event"] == "files:content_result"
                and item["data"].get("request_id") == request_id
            ),
            "files:content_result",
            start=start,
            timeout=20,
        )
        result = row["data"]
        assert "error" not in result, result
        if result.get("chunked"):
            chunks = [
                item["data"]
                for item in self.file_events
                if item["event"] == "files:content_chunk"
                and item["data"].get("request_id") == request_id
            ]
            chunks.sort(key=lambda item: item["index"])
            assert len(chunks) == result["total_chunks"], {
                "metadata": {
                    key: value for key, value in result.items() if key != "content"
                },
                "chunk_count": len(chunks),
            }
            encoded = "".join(item["content"] for item in chunks)
        else:
            encoded = result.get("content", "")
        raw = base64.b64decode(encoded, validate=True) if encoded else b""
        assert len(raw) == result["size"], {
            "metadata": {
                key: value for key, value in result.items() if key != "content"
            },
            "actual_bytes": len(raw),
        }
        return {"metadata": result, "bytes": raw}

    async def download_bytes(self, workspace_id: str, path: str) -> dict[str, Any]:
        """Verify real runner download content and metadata."""
        request_id = "mobile-download-" + uuid.uuid4().hex
        start = len(self.file_events)
        await self.client.emit(
            "frontend:files_download",
            {"workspace_id": workspace_id, "request_id": request_id, "path": path},
            namespace="/frontend",
        )
        row = await _wait_event(
            self.file_events,
            lambda item: (
                item["event"] == "files:download_result"
                and item["data"].get("request_id") == request_id
            ),
            "files:download_result",
            start=start,
            timeout=20,
        )
        result = row["data"]
        assert "error" not in result, result
        if result.get("chunked"):
            chunks = [
                item["data"]
                for item in self.file_events
                if item["event"] == "files:download_chunk"
                and item["data"].get("request_id") == request_id
            ]
            chunks.sort(key=lambda item: item["index"])
            assert len(chunks) == result["total_chunks"], {
                "metadata": {
                    key: value for key, value in result.items() if key != "content"
                },
                "chunk_count": len(chunks),
            }
            encoded = "".join(item["content"] for item in chunks)
        else:
            encoded = result.get("content", "")
        raw = base64.b64decode(encoded, validate=True) if encoded else b""
        assert len(raw) == result["size"], {
            "metadata": {
                key: value for key, value in result.items() if key != "content"
            },
            "actual_bytes": len(raw),
        }
        return {"metadata": result, "bytes": raw}

    async def close(self) -> None:
        """Close only this smoke's frontend socket."""
        if self.client.connected:
            await self.client.disconnect()


async def _wait_timeline_tool_detail(
    base: str,
    headers: dict[str, str],
    session_id: str,
    tool_name: str,
    timeout: float = 10,
) -> dict[str, Any]:
    """Fetch compact timeline then explicitly expand the matched tool part."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        timeline = await _timeline(base, headers, session_id)
        for message in timeline["messages"]:
            for part in message["parts"]:
                if part.get("type") != "tool" or part.get("tool") != tool_name:
                    continue
                detail = await _request(
                    "GET",
                    f"{base}/api/v1/harness/sessions/{session_id}/parts/{part['id']}",
                    headers=headers,
                    timeout=5,
                )
                if detail.status_code == 200:
                    return detail.json()
        await asyncio.sleep(0.05)
    raise AssertionError(f"No persisted {tool_name} tool detail in {session_id}")


async def _request_eventually(
    base: str,
    headers: dict[str, str],
    session_id: str,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
) -> requests.Response:
    """Retry resolver while a just-emitted live gate settles into persistence."""
    deadline = asyncio.get_running_loop().time() + 8
    last: requests.Response | None = None
    url = f"{base}/api/v1/harness/sessions/{session_id}/{path}"
    while asyncio.get_running_loop().time() < deadline:
        last = await _request(method, url, headers=headers, json=json_body, timeout=5)
        if last.status_code != 404:
            return last
        await asyncio.sleep(0.05)
    assert last is not None
    return last


async def _timeline(
    base: str, headers: dict[str, str], session_id: str
) -> dict[str, Any]:
    """Fetch a real timeline response envelope."""
    response = await _request(
        "GET",
        f"{base}/api/v1/harness/sessions/{session_id}/timeline",
        headers=headers,
        timeout=10,
    )
    assert response.status_code == 200, response.text[:500]
    return response.json()


async def _wait_idle_rest(
    base: str, headers: dict[str, str], session_id: str, timeout: float = 35
) -> dict[str, Any]:
    """Wait for persisted terminal idle after the live status event."""
    deadline = asyncio.get_running_loop().time() + timeout
    last: dict[str, Any] | None = None
    while asyncio.get_running_loop().time() < deadline:
        last = await _timeline(base, headers, session_id)
        if last["session"]["status"] == "idle":
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"Run {session_id} did not settle: {last!r}")


async def _create_ready_session(
    base: str,
    headers: dict[str, str],
    workspace_id: str,
    title: str,
) -> str:
    """Create an isolated throwaway session and wait for its bootstrap turn."""
    response = await _request(
        "POST",
        f"{base}/api/v1/workspaces/{workspace_id}/harness/sessions/",
        headers=headers,
        json={
            "prompt": f"{title} bootstrap {uuid.uuid4().hex[:8]}",
            "mode": "build",
            "agent_name": "build",
            "harness_id": "native",
            "model": "openai-compatible/fake",
        },
        timeout=10,
    )
    assert response.status_code == 201, response.text[:500]
    session_id = str(response.json()["id"])
    assert response.json()["status"] == "busy", response.json()
    await _wait_idle_rest(base, headers, session_id)
    return session_id


async def _question_and_permission_smoke(
    probe: FrontendProbe,
    base: str,
    headers: dict[str, str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify real gate socket/REST contracts and resumed persisted tools."""
    workspace_id = manifest["primary_workspace_id"]
    session_id = await _create_ready_session(
        base, headers, workspace_id, "Mobile question gate smoke"
    )
    question_prompt = "mobile-e2e:question " + uuid.uuid4().hex[:8]
    cursor = len(probe.events)
    started = await _request(
        "POST",
        f"{base}/api/v1/harness/sessions/{session_id}/message",
        headers=headers,
        json={"prompt": question_prompt},
        timeout=10,
    )
    assert started.status_code == 202, started.text[:500]
    assert started.json()["status"] == "busy", started.json()
    gate = await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.question_required"
            and row["data"].get("session_id") == session_id
        ),
        "question-required Socket.IO event",
        start=cursor,
    )
    question_id = gate["data"]["request_id"]
    answer = await _request_eventually(
        base,
        headers,
        session_id,
        "POST",
        f"questions/{question_id}",
        json_body={"answers": ["Blue"]},
    )
    assert answer.status_code == 200, answer.text[:500]
    answer_json = answer.json()
    assert answer_json == {
        "request_id": question_id,
        "status": "answered",
        "resumed": True,
    }, answer_json
    resolved = await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.question_required"
            and row["data"].get("request_id") == question_id
            and row["data"].get("status") == "answered"
        ),
        "question-resolved Socket.IO event",
        start=cursor,
    )
    assert resolved["data"]["status"] == "answered"
    await _wait_idle_rest(base, headers, session_id)
    question_parts = await _request(
        "GET",
        f"{base}/api/v1/harness/sessions/{session_id}/parts",
        headers=headers,
        timeout=10,
    )
    assert question_parts.status_code == 200, question_parts.text[:500]
    question_detail = await _wait_timeline_tool_detail(
        base, headers, session_id, "question"
    )
    assert "Blue" in str(question_detail.get("output", "")), question_detail
    delete_question = await _request(
        "DELETE",
        f"{base}/api/v1/harness/sessions/{session_id}",
        headers=headers,
        timeout=10,
    )
    assert delete_question.status_code == 204, delete_question.text[:300]

    permission_session = await _create_ready_session(
        base, headers, workspace_id, "Mobile permission gate smoke"
    )
    readme = Path(manifest["workspace_root"]) / workspace_id / "README.md"
    readme.write_text("# Mobile Fixture\n\nFresh permission fixture.\n")
    permission_prompt = "mobile-e2e:permission " + uuid.uuid4().hex[:8]
    cursor = len(probe.events)
    started = await _request(
        "POST",
        f"{base}/api/v1/harness/sessions/{permission_session}/message",
        headers=headers,
        json={"prompt": permission_prompt},
        timeout=10,
    )
    assert started.status_code == 202, started.text[:500]
    assert started.json()["status"] == "busy", started.json()
    gate = await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.permission_required"
            and row["data"].get("session_id") == permission_session
        ),
        "permission-required Socket.IO event",
        start=cursor,
    )
    permission_id = gate["data"]["request_id"]
    permission = await _request_eventually(
        base,
        headers,
        permission_session,
        "POST",
        f"permissions/{permission_id}",
        json_body={"response": "once"},
    )
    assert permission.status_code == 200, permission.text[:500]
    permission_json = permission.json()
    assert permission_json == {
        "request_id": permission_id,
        "decision": "allow",
        "remember": "once",
    }, permission_json
    resolved = await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.permission_required"
            and row["data"].get("request_id") == permission_id
            and row["data"].get("decision") == "allow"
        ),
        "permission-resolved Socket.IO event",
        start=cursor,
    )
    assert resolved["data"]["decision"] == "allow"
    await _wait_idle_rest(base, headers, permission_session)
    assert "approved edit" in readme.read_text()
    edit_detail = await _wait_timeline_tool_detail(
        base, headers, permission_session, "edit"
    )
    assert "Replaced" in str(edit_detail.get("output", "")), edit_detail
    delete_permission = await _request(
        "DELETE",
        f"{base}/api/v1/harness/sessions/{permission_session}",
        headers=headers,
        timeout=10,
    )
    assert delete_permission.status_code == 204, delete_permission.text[:300]
    return {
        "question_status": answer_json["status"],
        "question_resumed": answer_json["resumed"],
        "permission_decision": permission_json["decision"],
        "permission_tool_executed": True,
    }


async def _file_protocol_smoke(
    probe: FrontendProbe,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Test real runner upload/list/find/read/download and byte equality."""
    workspace_id = manifest["primary_workspace_id"]
    upload = await probe.upload_file(
        workspace_id,
        directory=UPLOAD_DIRECTORY,
        filename=UPLOAD_FILENAME,
        content=UPLOAD_BYTES,
    )
    expected_path = f"{UPLOAD_DIRECTORY}/{UPLOAD_FILENAME}"
    assert upload["workspace_id"] == workspace_id
    assert upload["path"] == UPLOAD_DIRECTORY
    assert upload["status"] == "success"
    listed = await probe.list_files(workspace_id, UPLOAD_DIRECTORY)
    listing = {row["name"]: row for row in listed["entries"]}
    assert UPLOAD_FILENAME in listing, listed
    assert listing[UPLOAD_FILENAME]["path"] == expected_path
    assert listing[UPLOAD_FILENAME]["size"] == len(UPLOAD_BYTES)
    found = await probe.find_files(workspace_id, UPLOAD_FILENAME)
    # Current runner and webapp contract is paths=[{name,path}], whereas the
    # shared client declaration currently says string[]. Report the wire DTO.
    assert any(
        isinstance(row, dict) and row.get("path") == expected_path
        for row in found["paths"]
    ), found
    read = await probe.read_bytes(workspace_id, expected_path)
    assert read["bytes"] == UPLOAD_BYTES
    downloaded = await probe.download_bytes(workspace_id, expected_path)
    assert downloaded["bytes"] == UPLOAD_BYTES
    disk_path = (
        Path(manifest["workspace_root"])
        / workspace_id
        / ".opencuria"
        / "user-uploaded"
        / UPLOAD_FILENAME
    )
    assert disk_path.read_bytes() == UPLOAD_BYTES
    return {
        "chunked_upload_status": upload["status"],
        "chunked_upload_bytes": len(UPLOAD_BYTES),
        "file_list_contract": "entries[{name,path,type,size}]",
        "file_find_wire_shape": "paths[{name,path}]",
        "file_read_matches_upload": True,
        "file_download_matches_upload": True,
        "uploaded_workspace_file_matches": True,
    }


async def _socket_harness_smoke(
    probe: FrontendProbe,
    base: str,
    headers: dict[str, str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Create an ordinary harness session and prove incremental socket text."""
    workspace_id = manifest["primary_workspace_id"]
    start = len(probe.events)
    created = await _request(
        "POST",
        f"{base}/api/v1/workspaces/{workspace_id}/harness/sessions/",
        headers=headers,
        json={
            "prompt": "Mobile socket stream " + uuid.uuid4().hex[:8],
            "mode": "build",
            "agent_name": "build",
            "harness_id": "native",
            "model": "openai-compatible/fake",
        },
        timeout=10,
    )
    assert created.status_code == 201, created.text[:500]
    session_id = str(created.json()["id"])
    assert created.json()["status"] == "busy", created.json()
    await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.session_status"
            and row["data"].get("session_id") == session_id
            and row["data"].get("status") == "idle"
        ),
        "new harness session idle status",
        start=start,
        timeout=35,
    )
    parts = [
        row
        for row in probe.events[start:]
        if row["event"] == "harness.part_updated"
        and row["data"].get("session_id") == session_id
    ]
    text = "".join(
        str((row["data"].get("delta") or {}).get("text") or "") for row in parts
    )
    assert "Mobile fixture stream is live." in text, parts
    await _timeline(base, headers, session_id)
    persisted_text = await _request(
        "GET",
        f"{base}/api/v1/harness/sessions/{session_id}/parts",
        headers=headers,
        timeout=10,
    )
    assert persisted_text.status_code == 200, persisted_text.text[:500]
    text_outputs = [
        part.get("output", "")
        for message in persisted_text.json()["messages"]
        for part in message["parts"]
        if part.get("type") == "text"
    ]
    assert any("Mobile fixture stream is live." in value for value in text_outputs)
    return {
        "stream_session_id": session_id,
        "socket_text_part_events": len(parts),
        "persisted_text_parts": len(text_outputs),
        "terminal_idle_status": True,
    }


async def _live_abort_smoke(
    probe: FrontendProbe,
    base: str,
    headers: dict[str, str],
    session_id: str,
) -> dict[str, Any]:
    """Observe active busy state, reject double-send, abort, and verify idle."""
    start = len(probe.events)
    started = await _request(
        "POST",
        f"{base}/api/v1/harness/sessions/{session_id}/message",
        headers=headers,
        json={"prompt": "mobile-e2e:abort"},
        timeout=10,
    )
    assert started.status_code == 202, started.text[:500]
    assert started.json()["status"] == "busy", started.json()
    await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.part_updated"
            and row["data"].get("session_id") == session_id
            and (row["data"].get("delta") or {}).get("text")
        ),
        "first slow abort-test delta",
        start=start,
        timeout=10,
    )
    duplicate = await _request(
        "POST",
        f"{base}/api/v1/harness/sessions/{session_id}/message",
        headers=headers,
        json={"prompt": "mobile-e2e:abort"},
        timeout=10,
    )
    assert duplicate.status_code == 409, duplicate.text[:500]
    abort = await _request(
        "POST",
        f"{base}/api/v1/harness/sessions/{session_id}/abort",
        headers=headers,
        timeout=10,
    )
    assert abort.status_code == 200 and abort.json()["status"] == "idle", abort.text[
        :500
    ]
    idle = await _wait_event(
        probe.events,
        lambda row: (
            row["event"] == "harness.session_status"
            and row["data"].get("session_id") == session_id
            and row["data"].get("status") == "idle"
        ),
        "abort terminal idle event",
        start=start,
    )
    assert idle["data"]["status"] == "idle"
    return {
        "busy_status": started.json()["status"],
        "duplicate_send_status": duplicate.status_code,
        "abort_status": abort.status_code,
    }


async def _http_lifecycle_smoke(
    base: str,
    headers: dict[str, str],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Exercise real rename/edit/fork/read/unread/delete lifecycle routes."""
    session_id = manifest["editable_session_id"]
    message_id = manifest["editable_message_id"]
    session_url = f"{base}/api/v1/harness/sessions/{session_id}"
    renamed = await _request(
        "PATCH",
        session_url,
        headers=headers,
        json={"title": "Mobile fixture renamed"},
        timeout=10,
    )
    assert (
        renamed.status_code == 200
        and renamed.json()["title"] == "Mobile fixture renamed"
    ), renamed.text[:400]
    for name, expected in (("unread", True), ("read", False)):
        status = await _request(
            "POST", f"{session_url}/{name}", headers=headers, timeout=10
        )
        assert status.status_code == 204, status.text[:300]
        state = await _timeline(base, headers, session_id)
        assert state["session"]["manual_unread"] is expected, state["session"]

    fork = await _request(
        "POST",
        f"{session_url}/fork",
        headers=headers,
        json={"message_id": message_id, "include_message": True},
        timeout=10,
    )
    assert fork.status_code == 201, fork.text[:500]
    fork_id = str(fork.json()["id"])
    fork_timeline = await _timeline(base, headers, fork_id)
    source_timeline = await _timeline(base, headers, session_id)
    original = next(
        row for row in source_timeline["messages"] if row["id"] == message_id
    )
    assert any(
        row["role"] == original["role"] and row["content"] == original["content"]
        for row in fork_timeline["messages"]
    ), fork_timeline["messages"]
    fork_delete = await _request(
        "DELETE",
        f"{base}/api/v1/harness/sessions/{fork_id}",
        headers=headers,
        timeout=10,
    )
    assert fork_delete.status_code == 204, fork_delete.text[:300]
    assert (
        await _request(
            "GET",
            f"{base}/api/v1/harness/sessions/{fork_id}/timeline",
            headers=headers,
            timeout=10,
        )
    ).status_code == 404

    edited = await _request(
        "POST",
        f"{base}/api/v1/harness/sessions/{session_id}/messages/{message_id}/edit",
        headers=headers,
        json={"prompt": "mobile-e2e:edit"},
        timeout=10,
    )
    assert edited.status_code == 202 and edited.json()["status"] == "busy", edited.text[
        :500
    ]
    timeline = await _wait_idle_rest(base, headers, session_id)
    edited_message = next(
        row
        for row in timeline["messages"]
        if row["role"] == "user" and row["content"] == "mobile-e2e:edit"
    )
    manifest["editable_message_id"] = edited_message["id"]
    manifest_path = Path(os.environ["MOBILE_E2E_RUN_DIR"]) / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    manifest_path.chmod(0o600)
    title_restore = await _request(
        "PATCH",
        session_url,
        headers=headers,
        json={"title": "Editable mobile test"},
        timeout=10,
    )
    assert title_restore.status_code == 200, title_restore.text[:300]

    delete_create = await _request(
        "POST",
        f"{base}/api/v1/workspaces/{manifest['workspace_ids'][1]}/harness/sessions/",
        headers=headers,
        json={
            "prompt": "Delete lifecycle smoke fixture",
            "mode": "build",
            "agent_name": "build",
            "harness_id": "native",
            "model": "openai-compatible/fake",
        },
        timeout=10,
    )
    assert delete_create.status_code == 201, delete_create.text[:400]
    delete_id = str(delete_create.json()["id"])
    await _wait_idle_rest(base, headers, delete_id)
    deleted = await _request(
        "DELETE",
        f"{base}/api/v1/harness/sessions/{delete_id}",
        headers=headers,
        timeout=10,
    )
    assert deleted.status_code == 204, deleted.text[:300]
    missing = await _request(
        "GET",
        f"{base}/api/v1/harness/sessions/{delete_id}/timeline",
        headers=headers,
        timeout=10,
    )
    assert missing.status_code == 404, missing.text[:300]
    return {
        "rename_status": renamed.status_code,
        "unread_read_round_trip": True,
        "fork_status": fork.status_code,
        "fork_delete_status": fork_delete.status_code,
        "edit_rerun_status": edited.status_code,
        "edit_run_settled": True,
        "delete_status": deleted.status_code,
    }


async def main_async() -> dict[str, Any]:
    """Run this run's real auth, file, socket, gate and lifecycle contracts."""
    manifest = _manifest()
    base = manifest["http_url"].rstrip("/")
    login = await _request(
        "POST",
        base + "/api/v1/auth/login/",
        json={
            "email": manifest["email"],
            "password": os.environ["MOBILE_E2E_PASSWORD"],
        },
        timeout=10,
    )
    assert login.status_code == 200, login.text[:500]
    token = login.json()["access_token"]
    headers = {
        "Authorization": "Bearer " + token,
        "X-Organization-Id": manifest["org_id"],
    }
    probe = FrontendProbe(base)
    try:
        await probe.connect(manifest["workspace_ids"][:2], token)
        file_summary = await _file_protocol_smoke(probe, manifest)
        socket_summary = await _socket_harness_smoke(probe, base, headers, manifest)
        lifecycle = await _http_lifecycle_smoke(base, headers, manifest)
        gates = await _question_and_permission_smoke(probe, base, headers, manifest)
        abort = await _live_abort_smoke(
            probe, base, headers, manifest["abort_session_id"]
        )
        return {
            "authenticated_frontend_socket": True,
            **file_summary,
            **socket_summary,
            **lifecycle,
            **gates,
            **abort,
        }
    finally:
        await probe.close()


def main() -> None:
    """Run against the separately started mobile fixture backend."""
    print(json.dumps(asyncio.run(main_async()), indent=2))


if __name__ == "__main__":
    main()
