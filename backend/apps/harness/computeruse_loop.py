"""Computer-use recording/truncation helpers (Agent-S lifecycle).

The legacy OpenComputer JSON tool loop lived here
(``ComputerUseLoopState``, 1000-grid ``tools/computeruse.py`` plumbing,
image-ledger compaction). It is fully replaced by the Agent-S harness
integration (:mod:`apps.harness.agent_s.harness`); this module only keeps
the recording-path/video-markdown/task-truncation helpers, canonically
re-exported from the Agent-S harness layer so every caller shares one
implementation and one signature (``run_id`` + explicit
``recording_path``).
"""

from __future__ import annotations

from .agent_s.harness import (
    append_video_to_output,
    default_recording_path,
    sanitize_run_id,
    truncate_task_output,
)

COMPUTER_USE_RECORD_DIR = "/workspace/.opencuria/computeruse"


def video_markdown(run_id: str) -> str:
    """Markdown embed line for the session recording."""
    return f"\n\n![Computer use]({default_recording_path(run_id)})"


__all__ = [
    "COMPUTER_USE_RECORD_DIR",
    "append_video_to_output",
    "default_recording_path",
    "sanitize_run_id",
    "truncate_task_output",
    "video_markdown",
]
