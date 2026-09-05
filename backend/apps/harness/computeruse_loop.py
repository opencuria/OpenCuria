"""Computer-use agent loop helpers (OpenComputer message compaction + recording)."""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from .providers.base import LLMMessage

COMPUTER_USE_RECORD_DIR = "/workspace/.opencuria/computeruse"
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

INITIAL_DESKTOP_TEXT = (
    "This is the automatically captured desktop at the start of the current "
    "request. Use it as the current normalized 1000x1000 coordinate frame."
)
FRESH_DESKTOP_TEXT = (
    "Here is the freshest desktop image returned by the latest tool. It is "
    "the current normalized 1000x1000 coordinate frame."
)
DESKTOP_FRAME_MARKER = "normalized 1000x1000 coordinate frame"


def sanitize_run_id(session_id: str) -> str:
    """Sanitize a session id for runner ``record_*`` actions.

    Mirrors runner ``_sanitize_run_id`` but replaces disallowed characters
    (for example slashes in in-process test session ids) with hyphens.
    """
    raw = (session_id or "").strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw)
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    if not cleaned or not re.match(r"^[A-Za-z0-9]", cleaned):
        cleaned = f"run-{cleaned}" if cleaned else f"run-{uuid.uuid4().hex[:12]}"
    cleaned = cleaned.strip("-")
    if not _RUN_ID_RE.match(cleaned):
        cleaned = f"run-{uuid.uuid4().hex[:12]}"
    return cleaned


def default_recording_path(run_id: str) -> str:
    """Return the default workspace path for a computer-use session recording."""
    return f"{COMPUTER_USE_RECORD_DIR}/{run_id}/session.mp4"


def video_markdown(run_id: str) -> str:
    """Markdown embed line for the session recording."""
    return f"\n\n![Computer use]({default_recording_path(run_id)})"


def desktop_image_message(text: str, jpeg: bytes) -> LLMMessage:
    """Build a multimodal user message with a JPEG data URL."""
    encoded = base64.b64encode(jpeg).decode("ascii")
    return LLMMessage(
        role="user",
        content=[
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            },
        ],
    )


def is_desktop_screenshot_message(message: LLMMessage) -> bool:
    """Return True when *message* is a harness-injected desktop frame."""
    if message.role != "user" or not isinstance(message.content, list):
        return False
    for part in message.content:
        if part.get("type") != "text":
            continue
        text = str(part.get("text", ""))
        if DESKTOP_FRAME_MARKER in text:
            return True
    return False


def count_image_url_parts(messages: list[LLMMessage]) -> int:
    """Count ``image_url`` parts across all messages."""
    total = 0
    for message in messages:
        content = message.content
        if not isinstance(content, list):
            continue
        for part in content:
            if part.get("type") == "image_url":
                total += 1
    return total


def append_video_to_output(output: str, run_id: str) -> str:
    """Append the recording markdown embed when it is not already present."""
    marker = f"![Computer use]({default_recording_path(run_id)})"
    if marker in output:
        return output
    return output.rstrip() + video_markdown(run_id)


def truncate_task_output(
    output: str,
    run_id: str,
    max_chars: int,
) -> tuple[str, bool]:
    """Truncate *output* for parent task cards while preserving the video line."""
    full = append_video_to_output(output, run_id)
    suffix = video_markdown(run_id)
    if full.endswith(suffix):
        text = full[: -len(suffix)].rstrip()
    else:
        path = default_recording_path(run_id)
        marker = f"![Computer use]({path})"
        idx = full.rfind(marker)
        if idx == -1:
            return full, False
        text = full[:idx].rstrip()
        suffix = full[idx:]

    if len(text) + len(suffix) <= max_chars:
        return text + suffix, False

    notice_reserve = 40
    avail = max(0, max_chars - len(suffix) - notice_reserve)
    truncated = (
        text[:avail] + f"\n…[truncated {len(text)} chars total]" + suffix
    )
    return truncated, True


@dataclass
class ComputerUseLoopState:
    """In-memory message layout for the computer-use provider loop."""

    base_messages: list[LLMMessage] = field(default_factory=list)
    ledger_lines: list[str] = field(default_factory=list)
    round_messages: list[LLMMessage] = field(default_factory=list)
    screenshot_message: LLMMessage | None = None

    def build_provider_messages(self) -> list[LLMMessage]:
        """Assemble the provider-facing message list for the next step."""
        messages = list(self.base_messages)
        if self.ledger_lines:
            messages.append(
                LLMMessage(
                    role="user",
                    content=(
                        "Completed action ledger (older screenshots discarded):\n"
                        + "\n".join(self.ledger_lines)
                    ),
                )
            )
        messages.extend(self.round_messages)
        if self.screenshot_message is not None:
            messages.append(self.screenshot_message)
        return messages

    def flush_round_to_ledger(self) -> None:
        """Move the completed assistant/tool round into the text ledger."""
        for message in self.round_messages:
            if message.role != "assistant" or not message.tool_calls:
                continue
            for call in message.tool_calls:
                name = str(call.get("name", "") or "tool")
                raw_args = str(call.get("arguments", "") or "")
                self.ledger_lines.append(f"{name}: {raw_args}")
        self.round_messages.clear()

    def set_screenshot(self, text: str, jpeg: bytes) -> None:
        """Replace the latest desktop frame user message."""
        self.screenshot_message = desktop_image_message(text, jpeg)
