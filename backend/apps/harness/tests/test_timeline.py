"""Focused tests for compact timeline display projections."""

from __future__ import annotations

import re
from typing import Any

import pytest

from apps.harness import timeline
from apps.harness.timeline import build_part_display


def test_patch_projection_matches_collapsed_file_diff_window():
    diff = """--- a/f.py
+++ b/f.py
@@ -1,8 +1,8 @@
 keep-1
 keep-2
-old
+new
 keep-3
 keep-4
 keep-5
"""
    display = build_part_display(
        part_type="patch", output=diff, meta_data={"path": "f.py"}
    )
    assert display["preview"] == [
        {"type": "context", "oldNo": 2, "newNo": 2, "content": "keep-2"},
        {"type": "del", "oldNo": 3, "newNo": None, "content": "old"},
        {"type": "add", "oldNo": None, "newNo": 3, "content": "new"},
        {"type": "context", "oldNo": 4, "newNo": 4, "content": "keep-3"},
    ]
    assert display["additions"] == display["deletions"] == 1


def test_patch_preview_is_bounded_and_handles_hunks_and_added_file():
    output = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1,12 @@\n" + "".join(
        f"+line {i}\n" for i in range(12)
    )
    display = build_part_display(part_type="patch", output=output)
    assert len(display["preview"]) == 4
    assert [line["content"] for line in display["preview"]] == [
        "line 0",
        "line 1",
        "line 2",
        "line 3",
    ]
    assert display["additions"] == 12
    assert display["deletions"] == 0


@pytest.mark.parametrize(
    "kinds,indices",
    [
        ("", []),
        ("c", [0]),
        ("ccac", [0, 1, 2, 3]),
        ("cccccc", [0, 1, 2, 3]),
        ("accccc", [0, 1]),
        ("dacccc", [0, 1, 2]),
        ("ccacccc", [1, 2, 3]),
        ("ccdcccc", [1, 2, 3]),
        ("ccdacccc", [1, 2, 3, 4]),
        ("ccaddccc", [1, 2, 3, 4]),
        ("addccc", [0, 1, 2, 3]),
        ("aaddcc", [0, 1, 2, 3]),
        ("ccaccccdcc", [1, 2, 3, 4]),
        ("ccccca", [4, 5]),
        ("cccccd", [4, 5]),
        ("ccccad", [3, 4, 5]),
    ],
)
def test_patch_preview_window_contract(kinds: str, indices: list[int]) -> None:
    """Keep short diffs, leading context and the single-change trailing window."""
    prefixes = {"c": " ", "a": "+", "d": "-"}
    output = "@@ -10,20 +30,20 @@\n" + "".join(
        f"{prefixes[kind]}line-{index}\n" for index, kind in enumerate(kinds)
    )
    old_no, new_no = 10, 30
    lines = []
    for index, kind in enumerate(kinds):
        lines.append(
            {
                "type": {"c": "context", "a": "add", "d": "del"}[kind],
                "oldNo": None if kind == "a" else old_no,
                "newNo": None if kind == "d" else new_no,
                "content": f"line-{index}",
            }
        )
        old_no += kind != "a"
        new_no += kind != "d"
    display = build_part_display(part_type="patch", output=output)
    assert display["preview"] == [lines[index] for index in indices]
    assert display["additions"] == kinds.count("a")
    assert display["deletions"] == kinds.count("d")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_patch_preview_preserves_hunks_headers_and_content_bounds(newline: str) -> None:
    """Skip metadata, retain Unicode/blank context and number each hunk correctly."""
    output = newline.join(
        [
            "diff --git a/f b/f",
            "--- a/f",
            "+++ b/f",
            "@@ -7 +9 @@ function",
            " ",
            "-" + "ä" * 600,
            "+new\tvalue",
            "\\ No newline at end of file",
            "@@ -100,2 +200,2 @@",
            " context",
            "-old",
            "+new",
            "",
        ]
    )
    display = build_part_display(part_type="patch", output=output)
    assert display["preview"] == [
        {"type": "context", "oldNo": 7, "newNo": 9, "content": ""},
        {"type": "del", "oldNo": 8, "newNo": None, "content": "ä" * 500},
        {"type": "add", "oldNo": None, "newNo": 10, "content": "new\tvalue"},
        {"type": "context", "oldNo": 100, "newNo": 200, "content": "context"},
    ]
    assert display["additions"] == display["deletions"] == 2


def test_patch_without_hunk_or_terminal_newline() -> None:
    """Unframed changes still start numbering at one; malformed hunks are ignored."""
    display = build_part_display(
        part_type="patch", output="unknown\n@@ malformed\n---not-header\n+++value\r"
    )
    assert display["preview"] == [
        {"type": "del", "oldNo": 1, "newNo": None, "content": "--not-header"},
        {"type": "add", "oldNo": None, "newNo": 1, "content": "++value"},
    ]
    assert display["additions"] == display["deletions"] == 1


def test_patch_projection_scans_each_hunk_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counting a large suffix must not reparse it to build the tiny preview."""
    parsed_hunks = []
    original = timeline._HUNK_RE

    class HunkPattern:
        def match(self, raw: str) -> re.Match[str] | None:
            match = original.match(raw)
            if match:
                parsed_hunks.append(raw)
            return match

    monkeypatch.setattr(timeline, "_HUNK_RE", HunkPattern())
    display = build_part_display(
        part_type="patch",
        output="@@ -0,0 +1,10000 @@\n" + "+new\n" * 10000,
    )
    assert display["additions"] == 10000
    assert len(display["preview"]) == 4
    assert parsed_hunks == ["@@ -0,0 +1,10000 @@"]


def test_question_projection_preserves_question_and_answer():
    question = build_part_display(
        part_type="tool",
        input_data={
            "tool": "question",
            "arguments": {
                "questions": [
                    {
                        "header": "Preference",
                        "question": "Which option?",
                        "options": [{"label": "One", "description": "First"}],
                        "multiple": True,
                    }
                ]
            },
        },
        output='{"answers":["Chosen"]}',
    )
    assert question["question_rows"] == [
        {
            "header": "Preference",
            "question": "Which option?",
            "options": [{"label": "One", "description": "First"}],
            "multiple": True,
            "answer": "Chosen",
        }
    ]
    meta_question = build_part_display(
        part_type="tool",
        input_data={"tool": "question", "arguments": {}},
        meta_data={
            "questions": [{"header": "Meta", "question": "Meta question?"}],
            "answers": ["Meta answer"],
        },
    )
    assert meta_question["question_rows"][0]["header"] == "Meta"
    assert meta_question["question_rows"][0]["answer"] == "Meta answer"


@pytest.mark.django_db(transaction=True)
async def test_patch_event_persists_full_diff_before_emitting_preview(
    harness_workspace: Any,
) -> None:
    """Live events and saved timelines share the exact bounded patch projection."""
    from asgiref.sync import sync_to_async

    from apps.harness.repositories import (
        HarnessMessageRepository,
        HarnessPartRepository,
        HarnessSessionRepository,
    )
    from apps.harness.run_timeline import RunTimeline

    session = await sync_to_async(HarnessSessionRepository.create)(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
    )
    message = await sync_to_async(HarnessMessageRepository.create)(
        session_id=session.id, role="assistant"
    )
    output = "@@ -0,0 +1,1000 @@\n" + "+new\n" * 1000
    emitted = []

    async def emit(event: str, data: dict[str, Any], workspace_id: str) -> None:
        part = await sync_to_async(HarnessPartRepository.get_by_id)(data["part_id"])
        assert part is not None and part.output == output
        assert part.state == "completed"
        assert data["delta"]["display"] == part.display
        assert timeline.timeline_part_payload(part)["display"] == part.display
        emitted.append((event, data, workspace_id))

    run = RunTimeline(
        messages=HarnessMessageRepository,
        parts=HarnessPartRepository,
        run_context=lambda _: {},
        emit=emit,
    )
    await run.record(
        session,
        message,
        {
            "type": "patch",
            "path": "new.txt",
            "unified_diff": output,
            "step": 1,
            "tool": "write",
            "call_id": "write-1",
            "title": "New file",
        },
    )
    assert len(emitted) == 1
    event, data, workspace_id = emitted[0]
    assert event == "harness.part_updated"
    assert workspace_id == str(harness_workspace.id)
    assert data["message_id"] == str(message.id)
    assert data["delta"] == {
        "patch": "new.txt",
        "display": {
            "path": "new.txt",
            "step": 1,
            "tool": "write",
            "additions": 1000,
            "deletions": 0,
            "preview": [
                {"type": "add", "oldNo": None, "newNo": index, "content": "new"}
                for index in range(1, 5)
            ],
        },
    }


@pytest.mark.django_db
def test_patch_updates_and_forks_rebuild_the_same_projection(
    harness_workspace: Any,
) -> None:
    """Every repository write uses the shared projector and keeps full details."""
    from apps.harness.repositories import (
        HarnessMessageRepository,
        HarnessPartRepository,
        HarnessSessionRepository,
    )

    messages = []
    for _ in range(2):
        session = HarnessSessionRepository.create(
            workspace_id=harness_workspace.id,
            organization_id=harness_workspace.runner.organization_id,
        )
        messages.append(
            HarnessMessageRepository.create(session_id=session.id, role="assistant")
        )
    part = HarnessPartRepository.create(
        message_id=messages[0].id,
        type="patch",
        state="running",
        output="+old",
        meta={"path": "a.txt", "old_content": "private", "new_content": "private"},
    )
    updated = "@@ -1,5 +1,5 @@\n same\n-old\n+new\n same\n same\n same\n"
    HarnessPartRepository.mark_state(part, "completed", output=updated)
    part.refresh_from_db()
    expected = build_part_display(
        part_type="patch", output=updated, meta_data={"path": "a.txt"}
    )
    assert part.display == expected
    assert part.meta == {"path": "a.txt"}
    HarnessPartRepository.copy_for_messages({messages[0].id: messages[1].id})
    (copied,) = HarnessPartRepository.list_for_message(messages[1].id)
    assert copied.id != part.id
    assert copied.output == updated and copied.display == expected
    payload = timeline.timeline_part_payload(copied)
    assert payload["display"] == expected
    assert payload["output"] == "" and payload["detail_loaded"] is False
