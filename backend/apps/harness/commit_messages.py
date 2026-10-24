"""Prompt material and output cleanup for small-model commit subjects.

Pure helpers used by :meth:`HarnessService.generate_commit_message`: they
turn a runner ``working_diff`` payload plus recent commit subjects into a
bounded prompt and reduce the model reply to one plain subject line.
"""

from __future__ import annotations

import re
from typing import Any

#: Upper bound for the patch text sent to the small model.
MAX_DIFF_CHARS = 24_000
#: Per-file patch share so one huge file cannot hide all others.
MAX_FILE_DIFF_CHARS = 4_000
#: Upper bound for listed files (headers are always cheaper than patches).
MAX_FILES = 200
#: Number of recent subjects offered as a style reference.
STYLE_SUBJECTS = 10
#: Hard cap for the returned subject line.
MAX_SUBJECT_CHARS = 100

_LINE_PREFIX = {"add": "+", "del": "-"}
_LABEL_PREFIX = re.compile(r"^(commit message|commit subject|subject)\s*:\s*", re.I)
_WRAPPERS = (('"', '"'), ("'", "'"), ("“", "”"), ("„", "“"), ("`", "`"))


def changes_to_describe(diff: dict[str, Any] | None) -> tuple[str, list[dict]]:
    """Return the side to describe: staged files, else all unstaged files."""
    diff = diff or {}
    staged = [item for item in diff.get("staged") or [] if isinstance(item, dict)]
    if staged:
        return "Staged changes", staged
    unstaged = [item for item in diff.get("unstaged") or [] if isinstance(item, dict)]
    return "Changes", unstaged


def _file_path(item: dict[str, Any]) -> str:
    new_path = str(item.get("new_path") or "")
    old_path = str(item.get("old_path") or "")
    if old_path and new_path and old_path != new_path:
        return f"{old_path} -> {new_path}"
    return new_path or old_path


def _file_header(item: dict[str, Any]) -> str:
    status = str(item.get("status") or "M")
    header = f"{status} {_file_path(item)}"
    if item.get("binary"):
        return f"{header} (binary)"
    additions = int(item.get("additions") or 0)
    deletions = int(item.get("deletions") or 0)
    return f"{header} (+{additions} -{deletions})"


def _file_patch(item: dict[str, Any]) -> str:
    lines: list[str] = []
    for hunk in item.get("diff") or []:
        if not isinstance(hunk, dict):
            continue
        lines.append(str(hunk.get("header") or "@@"))
        for line in hunk.get("lines") or []:
            if not isinstance(line, dict):
                continue
            prefix = _LINE_PREFIX.get(str(line.get("type")), " ")
            lines.append(f"{prefix}{line.get('content', '')}")
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[diff truncated]"


def build_commit_message_prompt(
    diff: dict[str, Any] | None, recent_subjects: list[str] | None = None
) -> str:
    """Build the bounded user prompt, or ``""`` when nothing changed."""
    label, files = changes_to_describe(diff)
    if not files:
        return ""
    sections: list[str] = []
    subjects = [
        subject.strip()
        for subject in (recent_subjects or [])
        if isinstance(subject, str) and subject.strip()
    ][:STYLE_SUBJECTS]
    if subjects:
        sections.append(
            "Recent commit subjects:\n"
            + "\n".join(f"- {subject}" for subject in subjects)
        )
    listed = files[:MAX_FILES]
    file_lines = [_file_header(item) for item in listed]
    if len(files) > len(listed):
        file_lines.append(f"... and {len(files) - len(listed)} more files")
    sections.append(f"{label} ({len(files)} files):\n" + "\n".join(file_lines))
    budget = MAX_DIFF_CHARS
    patches: list[str] = []
    for item in listed:
        patch = _file_patch(item)
        if not patch:
            continue
        if budget <= 0:
            patches.append("[remaining diffs omitted]")
            break
        clipped = _clip(patch, min(MAX_FILE_DIFF_CHARS, budget))
        budget -= len(clipped)
        patches.append(f"--- {_file_path(item)}\n{clipped}")
    if patches:
        sections.append("Diff:\n" + "\n\n".join(patches))
    return "\n\n".join(sections)


def _unwrap(subject: str) -> str:
    """Drop one wrapping quote/backtick pair, keeping inner code spans."""
    for left, right in _WRAPPERS:
        inner = subject[1:-1]
        if (
            len(subject) > 1
            and subject.startswith(left)
            and subject.endswith(right)
            and left not in inner
            and right not in inner
        ):
            return inner.strip()
    return subject


def normalize_commit_subject(text: str) -> str:
    """Reduce model output to one plain subject line."""
    for line in (text or "").splitlines():
        subject = line.strip()
        if not subject or subject.startswith("```"):
            continue
        subject = _LABEL_PREFIX.sub("", subject.replace("**", ""))
        subject = " ".join(_unwrap(subject.strip()).split())
        if len(subject) > MAX_SUBJECT_CHARS:
            subject = subject[:MAX_SUBJECT_CHARS].rsplit(" ", 1)[0].rstrip(",;:- ")
        if subject:
            return subject
    return ""
