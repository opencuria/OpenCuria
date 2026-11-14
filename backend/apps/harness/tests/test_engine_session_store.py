"""Tests for the Claude SDK SessionStore adapter and safety bounds."""

from __future__ import annotations

import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.engines.session_store import HarnessClaudeSessionStore
from apps.harness.models import HarnessSession
from apps.harness.repositories import HarnessSessionRepository


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_session_store_batches_idempotent_entries_and_lists_subkeys(
    harness_workspace,
) -> None:
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    project_key = "workspace"
    external_id = str(uuid.uuid4())
    store = HarnessClaudeSessionStore(
        session_id=session.id,
        external_session_id=external_id,
        project_key=project_key,
    )
    key = {"project_key": project_key, "session_id": external_id}
    entries = [
        {"type": "user", "uuid": "stable-1", "message": "hello"},
        {"type": "assistant", "uuid": "stable-2", "message": "world"},
    ]
    await store.append(key, entries)
    await store.append(key, entries)
    await store.append(
        {**key, "subpath": "subagents/agent-123"},
        [{"type": "agent_metadata", "uuid": "meta-1"}],
    )

    assert await store.load(key) == entries
    assert await store.list_subkeys(key) == ["subagents/agent-123"]
    assert await store.load({**key, "subpath": "subagents/agent-123"}) == [
        {"type": "agent_metadata", "uuid": "meta-1"}
    ]
    await sync_to_async(session.refresh_from_db, thread_sensitive=True)()
    assert session.external_session_id == external_id
    sessions = await store.list_sessions(project_key)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == external_id
    assert isinstance(sessions[0]["mtime"], int)
    assert sessions[0]["mtime"] == HarnessEngineRepository.get_transcript_mtime(
        session.id, external_session_id=external_id
    )
    first_mtime = sessions[0]["mtime"]
    await store.append(
        key, [{"type": "result", "uuid": "later", "message": "last"}]
    )
    updated_sessions = await store.list_sessions(project_key)
    assert updated_sessions[0]["mtime"] >= first_mtime
    assert (
        len(
            HarnessEngineRepository.list_transcript_entries(
                session.id, external_session_id=external_id
            )
        )
        == 3
    )
    await store.delete(key)
    assert await store.load(key) is None
    assert await store.load({**key, "subpath": "subagents/agent-123"}) is None
    assert await store.list_sessions(project_key) == []
    assert await store.delete_session() == 0
    assert await store.list_subkeys(key) == []
    assert await store.list_sessions(project_key) == []


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_session_store_rejects_invalid_project_and_path(
    harness_workspace,
) -> None:
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
    )
    external_id = str(uuid.uuid4())
    store = HarnessClaudeSessionStore(
        session_id=session.id,
        external_session_id=external_id,
        project_key="workspace",
    )
    with pytest.raises(ValueError, match="project_key"):
        await store.load({"project_key": "../other", "session_id": external_id})
    with pytest.raises(ValueError, match="does not match workspace"):
        await store.load({"project_key": "other", "session_id": external_id})
    assert await store.list_sessions("other") == []
    with pytest.raises(ValueError, match="session_id"):
        await store.load({"project_key": "workspace", "session_id": "../../etc"})
    with pytest.raises(ValueError, match="subpath"):
        await store.append(
            {
                "project_key": "workspace",
                "session_id": external_id,
                "subpath": "subagents/../../escape",
            },
            [{"type": "assistant"}],
        )
    with pytest.raises(ValueError, match="does not match harness"):
        await store.load({"project_key": "workspace", "session_id": str(uuid.uuid4())})


def _read_image_entry(encoded: str) -> dict:
    """Return a native Read result row as Claude Code mirrors it."""
    return {
        "type": "user",
        "uuid": "read-image",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded,
                            },
                        }
                    ],
                }
            ],
        },
        "toolUseResult": {
            "type": "image",
            "file": {"base64": encoded, "type": "image/png", "originalSize": 3},
        },
    }


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_session_store_never_persists_inline_media(harness_workspace) -> None:
    """Mirrored rows keep their structure but replace base64 with a notice."""
    from apps.harness.media.tool_media import OMITTED_MEDIA_NOTICE

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    external_id = str(uuid.uuid4())
    store = HarnessClaudeSessionStore(
        session_id=session.id, external_session_id=external_id
    )
    key = {"project_key": "workspace", "session_id": external_id}
    encoded = "QUJD" * 1000
    entry = _read_image_entry(encoded)

    await store.append(key, [entry])

    stored = (await store.load(key))[0]
    result = stored["message"]["content"][0]
    assert result["content"] == [{"type": "text", "text": OMITTED_MEDIA_NOTICE}]
    assert stored["toolUseResult"]["file"] == {
        "base64": "",
        "type": "image/png",
        "originalSize": 3,
    }
    assert encoded not in str(stored)
    assert entry["toolUseResult"]["file"]["base64"] == encoded


@pytest.mark.django_db(transaction=True)
@pytest.mark.asyncio
async def test_session_store_keeps_rows_with_object_type_fields(
    harness_workspace,
) -> None:
    """A ``type`` key holding an object (e.g. a JSON schema) is plain data."""
    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    external_id = str(uuid.uuid4())
    store = HarnessClaudeSessionStore(
        session_id=session.id, external_session_id=external_id
    )
    key = {"project_key": "workspace", "session_id": external_id}
    schema = {"type": "object", "properties": {"type": {"type": "string"}}}
    entry = {
        "type": "assistant",
        "uuid": str(uuid.uuid4()),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "mcp_schema",
                    "input": {"type": {"enum": ["a"]}, "schema": schema},
                }
            ]
        },
    }

    await store.append(key, [entry])

    assert await store.load(key) == [entry]
