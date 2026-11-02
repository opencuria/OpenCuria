"""Codex checkpoints keep rollout structure but never inline image bytes."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid

import pytest
from asgiref.sync import async_to_sync

from apps.harness.engines.codex.rollout_media import strip_snapshot_media
from apps.harness.engines.codex.session_store import (
    SUBPATH,
    CodexSessionStore,
    home_for,
    validate_snapshot,
)
from apps.harness.engines.repositories import HarnessEngineRepository
from apps.harness.media.tool_media import OMITTED_MEDIA_NOTICE

IMAGE_URL = "data:image/png;base64," + "QUJD" * 512
ROLLOUT = "sessions/2026/11/02/rollout-2026-11-02T10-00-00-example.jsonl"


def _lines() -> list[dict]:
    """Return the three rollout line shapes that carry tool and user images."""
    image = {"type": "input_image", "image_url": IMAGE_URL, "detail": "high"}
    return [
        {"type": "session_meta", "payload": {"id": "thread"}},
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-1",
                "output": [{"type": "input_text", "text": "Image read"}, image],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [image]},
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "item": {"content": [{"type": "image", "image_url": IMAGE_URL}]},
            },
        },
    ]


def _file(path: str, data: bytes) -> dict:
    return {
        "path": path,
        "data": base64.b64encode(data).decode(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _record(session_id: uuid.UUID) -> dict:
    rollout = "\n".join(json.dumps(line) for line in _lines()).encode() + b"\n"
    return {
        "version": 1,
        "home": home_for(str(session_id)),
        "files": [_file(ROLLOUT, rollout), _file("state_5.sqlite", b"SQLite bytes")],
    }


def _rollout(record: dict) -> list[dict]:
    data = base64.b64decode(record["files"][0]["data"])
    return [json.loads(line) for line in data.splitlines() if line]


def test_strip_replaces_images_with_same_schema_text_and_fresh_digests() -> None:
    """Every image shape becomes text; unrelated lines and files stay intact."""
    session_id = uuid.uuid4()
    record = _record(session_id)

    stripped = strip_snapshot_media(record)

    validate_snapshot(stripped, home_for(str(session_id)))
    assert stripped["files"][1] == record["files"][1]
    lines = _rollout(stripped)
    assert lines[0] == _lines()[0]
    assert lines[1]["payload"]["output"] == [
        {"type": "input_text", "text": "Image read"},
        {"type": "input_text", "text": OMITTED_MEDIA_NOTICE},
    ]
    assert lines[2]["payload"]["content"] == [
        {"type": "input_text", "text": OMITTED_MEDIA_NOTICE}
    ]
    assert lines[3]["payload"]["item"]["content"] == [
        {"type": "text", "text": OMITTED_MEDIA_NOTICE}
    ]
    assert "data:image/" not in json.dumps(lines)
    assert strip_snapshot_media(stripped) == stripped


@pytest.mark.django_db(transaction=True)
def test_checkpoints_are_stored_and_restored_without_images(harness_workspace):
    """New saves strip media; legacy rows are stripped before any restore."""
    from apps.harness.models import HarnessMessage, HarnessRun
    from apps.harness.repositories import HarnessSessionRepository

    session = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    message = HarnessMessage.objects.create(session=session, role="assistant")
    run = HarnessRun.objects.create(
        session=session,
        assistant_message=message,
        organization_id=session.organization_id,
        harness_id="codex",
    )
    external = str(uuid.uuid4())
    store = CodexSessionStore(
        session_id=session.id, run_id=run.id, owner_token=run.owner_token
    )
    home = home_for(str(session.id))

    async_to_sync(store.save)(external, str(uuid.uuid4()), _record(session.id))
    [saved] = HarnessEngineRepository.list_transcript_entries(
        session.id, external_session_id=external, subpath=SUBPATH
    )
    assert "data:image/" not in json.dumps(_rollout(saved["snapshot"]))

    legacy = str(uuid.uuid4())
    HarnessEngineRepository.replace_owned_checkpoint(
        session.id,
        run_id=run.id,
        owner_token=run.owner_token,
        external_session_id=external,
        subpath=SUBPATH,
        generation=legacy,
        snapshot=_record(session.id),
    )
    loaded = async_to_sync(store.load)(external, home)
    assert loaded["generation"] == legacy
    assert "data:image/" not in json.dumps(_rollout(loaded["snapshot"]))
    validate_snapshot(loaded["snapshot"], home)
