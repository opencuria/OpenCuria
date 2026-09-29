"""Focused tests for compact timeline display projections."""

from __future__ import annotations

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
        "line 0", "line 1", "line 2", "line 3"
    ]
    assert display["additions"] == 12
    assert display["deletions"] == 0


def test_question_projection_preserves_bounded_question_answer_and_ask_user():
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

    legacy = build_part_display(
        part_type="tool",
        input_data={"tool": "ask_user", "arguments": {"question": "Q"}},
        output="A" * 900,
    )
    assert legacy["question_rows"][0]["question"] == "Q"
    assert len(legacy["question_rows"][0]["answer"]) == 500
