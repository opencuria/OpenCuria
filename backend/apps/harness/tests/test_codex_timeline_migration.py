"""Existing Codex timelines lose redundant snapshots without losing edits."""

from __future__ import annotations

import uuid
from importlib import import_module
from typing import Any

import pytest
from django.apps import apps
from django.db import connection
from django.utils import timezone

from apps.harness.models import HarnessMessage, HarnessSession
from apps.harness.repositories import HarnessPartRepository

MIGRATION = import_module(
    "apps.harness.migrations.0040_remove_codex_turn_diff_snapshots"
)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "harness_id,finished,with_file,linked_tool,call_id,removed",
    [
        ("codex", True, True, False, str(uuid.uuid4()), True),
        ("claude", True, True, False, str(uuid.uuid4()), False),
        ("opencuria", True, True, False, str(uuid.uuid4()), False),
        ("codex", False, True, False, str(uuid.uuid4()), False),
        ("codex", True, False, False, str(uuid.uuid4()), False),
        ("codex", True, True, True, str(uuid.uuid4()), False),
        ("codex", True, True, False, "native-file-call", False),
    ],
    ids=[
        "redundant-snapshot",
        "claude",
        "opencuria",
        "active-turn",
        "only-diff",
        "linked-tool",
        "unknown-call",
    ],
)
def test_cleanup_preserves_real_work_and_other_harnesses(
    harness_workspace: Any,
    monkeypatch: pytest.MonkeyPatch,
    harness_id: str,
    finished: bool,
    with_file: bool,
    linked_tool: bool,
    call_id: str,
    removed: bool,
) -> None:
    """Repair finished Codex history while leaving unrelated/uncertain rows alone."""
    session = HarnessSession.objects.create(
        workspace=harness_workspace,
        organization_id=harness_workspace.runner.organization_id,
        harness_id=harness_id,
    )
    message = HarnessMessage.objects.create(
        session=session,
        role="assistant",
        harness_id=harness_id,
        content="Changed the file.\nDone.",
        completed_at=timezone.now() if finished else None,
    )
    preserved = []
    if with_file:
        preserved.append(
            HarnessPartRepository.create(
                message_id=message.id,
                type="patch",
                state="completed",
                call_id="exec-edit",
                output="+new\n",
                meta={"path": "/workspace/file.py", "tool": "edit", "step": 1},
            )
        )
    if linked_tool:
        preserved.append(
            HarnessPartRepository.create(
                message_id=message.id,
                type="tool",
                state="completed",
                call_id=call_id,
            )
        )
    preserved.append(
        HarnessPartRepository.create(
            message_id=message.id,
            type="text",
            state="completed",
            output="Done.",
        )
    )
    snapshots = [
        HarnessPartRepository.create(
            message_id=message.id,
            type="patch",
            state="completed",
            call_id=call_id,
            output="diff --git a/file.py b/file.py\n+new\n",
            meta={"path": "", "tool": "edit", "step": 1},
        )
        for _ in range(3)
    ]
    # Exercise the bounded loop and idempotence without loading full diff bodies.
    monkeypatch.setattr(MIGRATION, "BATCH_SIZE", 2)
    for _ in range(2):
        MIGRATION.remove_turn_diff_snapshots(apps, connection.schema_editor())

    remaining = HarnessPartRepository.list_for_message(message.id)
    assert {part.id for part in remaining} == {
        part.id for part in preserved + ([] if removed else snapshots)
    }
    if removed:
        assert remaining[-1].type == "text"
        assert remaining[-1].output == "Done."
    message.refresh_from_db()
    assert message.content == "Changed the file.\nDone."
