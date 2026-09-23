"""Port/delegation contract tests for the runners service facade (Phase 5).

Only ports, delegation, aliases and stores — no business-logic
duplicates. Covers: constructor injection, ``ensure_definition_mutable``
alias delegation, repository-delegation methods (mocked repos),
``PendingMap``/``SessionMap`` store behaviour, and facade attribute
reachability (``__all__``, whitelists, timeouts).
"""

from __future__ import annotations

import asyncio
import types
import uuid
from unittest.mock import MagicMock

import pytest

from apps.runners.services import RunnerService, _PendingGitRequest
from apps.runners.services.infra.state import PendingMap, SessionMap


class _FakeResolver:
    def __init__(self):
        self.calls = []

    def resolve_workspace_credentials(self, workspace):
        self.calls.append(workspace)
        return {"ok": True}


class _FakeStreamRouter:
    def route_stream_output(self, data):
        return True

    def route_stream_closed(self, data):
        return True


class _FakeReplyRouter:
    def route_harness_chunk(self, data):
        return None

    def route_harness_done(self, data):
        return None

    def route_harness_result(self, data):
        return None

    def route_harness_file_chunk(self, data):
        return None


async def _fake_emit(event, data, workspace_id):
    return None


def test_constructor_injects_ports():
    resolver = _FakeResolver()
    stream_router = _FakeStreamRouter()
    reply_router = _FakeReplyRouter()
    svc = RunnerService(
        None,
        credential_resolver=resolver,
        harness_stream_router=stream_router,
        harness_reply_router=reply_router,
        frontend_bus=_fake_emit,
    )
    assert svc.sio is None
    assert svc._credential_resolver is resolver
    assert svc._harness_stream_router is stream_router
    assert svc._harness_reply_router is reply_router
    assert svc._frontend_bus is _fake_emit


def test_constructor_defaults_and_positional():
    svc_default = RunnerService()
    assert svc_default.sio is None
    assert svc_default._credential_resolver is None
    assert svc_default._harness_stream_router is None
    assert svc_default._harness_reply_router is None
    assert svc_default._frontend_bus is None
    sentinel = object()
    svc_positional = RunnerService(sentinel)
    assert svc_positional.sio is sentinel


def test_constructor_repositories_and_stores():
    svc = RunnerService()
    assert svc.runners is not None
    assert svc.workspaces is not None
    assert svc.tasks is not None
    assert svc.image_instances is not None
    assert svc.image_definitions is not None
    assert svc.build_jobs is not None
    assert svc.processes is not None
    assert isinstance(svc._process_pending, dict)
    assert isinstance(svc._git_pending, dict)
    assert isinstance(svc._pending_process_verify, dict)
    assert isinstance(svc._git_pending, PendingMap)
    assert isinstance(svc._process_pending, PendingMap)
    assert isinstance(svc._pending_process_verify, PendingMap)


def test_ensure_definition_mutable_delegates():
    svc = RunnerService()
    calls = []
    svc._ensure_definition_mutable = lambda definition: calls.append(definition)  # type: ignore[method-assign]
    sentinel = object()
    svc.ensure_definition_mutable(sentinel)
    assert calls == [sentinel]


def test_ensure_definition_mutable_frozen_raises_in_both():
    from apps.runners.models import ImageDefinition
    from common.exceptions import ConflictError

    frozen = types.SimpleNamespace(status=ImageDefinition.Status.DELETED)
    svc = RunnerService()
    with pytest.raises(ConflictError):
        svc._ensure_definition_mutable(frozen)
    with pytest.raises(ConflictError):
        svc.ensure_definition_mutable(frozen)


def test_repository_delegation_methods_mocked():
    svc = RunnerService()
    artifact_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    svc.image_instances = MagicMock()
    svc.image_instances.get_by_id.return_value = "artifact"
    svc.image_instances.update_name.return_value = True
    svc.image_instances.timeout_stale.return_value = 3
    svc.image_definitions = MagicMock()
    svc.image_definitions.get_by_id.return_value = "definition"
    svc.workspaces = MagicMock()
    svc.workspaces.get_by_id.return_value = "workspace"

    assert svc.get_image_artifact(artifact_id) == "artifact"
    svc.image_instances.get_by_id.assert_called_with(artifact_id)
    assert svc.rename_image_artifact(artifact_id, "new-name") is True
    svc.image_instances.update_name.assert_called_with(artifact_id, "new-name")
    assert svc.timeout_stale_image_artifacts(timeout_hours=2) == 3
    svc.image_instances.timeout_stale.assert_called_with(timeout_hours=2)
    assert svc.get_image_definition(definition_id) == "definition"
    svc.image_definitions.get_by_id.assert_called_with(definition_id)
    assert svc.get_workspace_or_none(artifact_id) == "workspace"
    svc.workspaces.get_by_id.assert_called_with(artifact_id)


def test_pending_map_store_behaviour():
    store: PendingMap = PendingMap()
    assert isinstance(store, dict)
    store["req-1"] = ("payload", 100.0)
    assert store.contains("req-1")
    assert store["req-1"] == ("payload", 100.0)
    store.discard("missing-never-raises")
    store.discard("req-1")
    assert "req-1" not in store
    store["old"] = ("payload", 0.0)
    store["fresh"] = ("payload", 1_000_000_000.0)
    store["plain"] = {"not": "timestamped"}
    dropped = store.sweep_expired(now=10_000.0, ttl_seconds=60.0)
    assert dropped == ["old"]
    assert "fresh" in store
    assert "plain" in store


def test_pending_git_request_record_and_session_map():
    loop = asyncio.new_event_loop()
    try:
        future = loop.create_future()
        record = _PendingGitRequest(
            future=future, workspace_id="ws-1", operation="list_repos"
        )
        assert record.workspace_id == "ws-1"
        assert record.operation == "list_repos"
    finally:
        loop.close()
    sessions = SessionMap()
    sessions.put_session("ws-1", {"port": 6901})
    assert sessions.get_session("ws-1") == {"port": 6901}
    assert sessions.contains("ws-1")
    assert sessions.drop_session("ws-1") == {"port": 6901}
    assert not sessions.contains("ws-1")


def test_facade_constants_reachable():
    svc = RunnerService()
    assert "list_repos" in svc.GIT_OPERATIONS
    assert "list_repos" in svc.GIT_READ_OPERATIONS
    assert isinstance(svc._GIT_ALLOWED_ARGS, dict)
    assert svc.BUILD_LOG_MAX_CHARS == 200_000
    assert svc._PROCESS_RPC_TIMEOUT_SECONDS == 30
    assert svc._STREAM_CHUNK_SIZE == 64 * 1024
    assert "workspace:stream_start" in svc._CALL_REPLY_EVENTS
    assert isinstance(RunnerService._active_terminals, dict)
    assert isinstance(RunnerService._active_desktops, dict)
