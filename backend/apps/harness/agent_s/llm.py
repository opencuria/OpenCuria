"""Async LLM call helpers (retry semantics mirroring Agent-S).

Portions adapted from simular-ai/Agent-S (https://github.com/simular-ai/Agent-S,
commit 3aa272d, Apache License 2.0). See THIRD_PARTY_NOTICES.md at the
repository root. The surrounding OpenCuria project remains AGPL-3.0-only.

Reference (``gui_agents/s3/utils/common_utils.py``):

- ``call_llm_safe``: up to 3 attempts; ``assert response is not None``; the
  ``time.sleep(1.0)`` sits *after* the ``try``/``except`` (outside the
  ``except`` block), so it runs after every failed attempt — including the
  third/final failure — but ``break`` on success skips it (no sleep after
  success); returns ``""`` when ``response`` is ``None`` after retries.
- ``call_llm_formatted``: snapshots ``generator.messages`` (copy) on entry;
  up to 3 format attempts; after a failure appends a *local-only* assistant
  message (bad response) + user message (``FORMATTING_FEEDBACK_PROMPT`` with
  ``FORMATTING_FEEDBACK`` replaced by ``"- "``-joined feedback); the final
  response is returned even when still invalid; ``sleep(1.0)`` after every
  failed format attempt (including the third), none after success.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from .ports import CallPurpose, Completion, CompletionPort, Usage

MAX_LLM_ATTEMPTS = 3
MAX_FORMAT_ATTEMPTS = 3

FormatChecker = Callable[[str], Any]
SyncFormatChecker = Callable[[str], tuple[bool, str]]
SleepFn = Callable[[float], Awaitable[None]]

UsageRecorder = Callable[[CallPurpose, Usage], None]


async def _default_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def build_formatting_feedback(feedback_msgs: Sequence[str]) -> str:
    """Join feedback as ``"- a\\n- b"`` (Agent-S ``delimiter`` quirk)."""
    delimiter = "\n- "
    return f"- {delimiter.join(feedback_msgs)}"


def render_formatting_prompt(feedback_msgs: Sequence[str], template: str) -> str:
    """Fill ``FORMATTING_FEEDBACK`` in the feedback template."""
    return template.replace(
        "FORMATTING_FEEDBACK", build_formatting_feedback(feedback_msgs)
    )


async def call_llm_safe(
    completion: CompletionPort,
    messages: list[dict[str, Any]],
    *,
    purpose: CallPurpose,
    temperature: float | None = None,
    use_thinking: bool = False,
    record_usage: UsageRecorder | None = None,
    sleep: SleepFn | None = None,
) -> tuple[str, list[Usage]]:
    """Call the completion port with Agent-S retry semantics.

    Returns ``(text, usages)``; ``usages`` holds one entry per *successful*
    ``CompletionPort`` response only — failed attempts raise before any
    ``Usage`` exists, so exceptions contribute no usage entry.
    """
    sleep_fn = sleep or _default_sleep
    attempt = 0
    response: str | None = ""
    usages: list[Usage] = []
    while attempt < MAX_LLM_ATTEMPTS:
        try:
            result: Completion = await completion.complete(
                messages,
                purpose=purpose,
                temperature=temperature,
                use_thinking=use_thinking,
            )
            response = result.text
            usages.append(result.usage)
            if record_usage is not None:
                record_usage(purpose, result.usage)
            assert response is not None, "Response from agent should not be None"
            break
        except Exception:
            attempt += 1
            if attempt == MAX_LLM_ATTEMPTS:
                pass
        await sleep_fn(1.0)
    return (response if response is not None else ""), usages


async def call_llm_formatted(
    completion: CompletionPort,
    messages: list[dict[str, Any]],
    format_checkers: Sequence[FormatChecker],
    *,
    purpose: CallPurpose,
    temperature: float | None = None,
    use_thinking: bool = False,
    formatting_template: str,
    record_usage: UsageRecorder | None = None,
    sleep: SleepFn | None = None,
) -> tuple[str, list[Usage]]:
    """Call the LLM until all format checkers pass (Agent-S semantics).

    ``messages`` is copied on entry and retried against the *local* copy;
    feedback messages are appended to that copy only.
    """
    sleep_fn = sleep or _default_sleep
    attempt = 0
    response = ""
    working = [dict(m) for m in messages]
    usages: list[Usage] = []
    while attempt < MAX_FORMAT_ATTEMPTS:
        text, call_usages = await call_llm_safe(
            completion,
            working,
            purpose=purpose,
            temperature=temperature,
            use_thinking=use_thinking,
            record_usage=record_usage,
            sleep=sleep_fn,
        )
        response = text
        usages.extend(call_usages)
        feedback_msgs: list[str] = []
        for checker in format_checkers:
            outcome = checker(response)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            success, feedback = outcome
            if not success:
                feedback_msgs.append(feedback)
        if not feedback_msgs:
            break
        working.append(
            {"role": "assistant", "content": [{"type": "text", "text": response}]}
        )
        working.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": render_formatting_prompt(
                            feedback_msgs, formatting_template
                        ),
                    }
                ],
            }
        )
        attempt += 1
        if attempt == MAX_FORMAT_ATTEMPTS:
            pass
        await sleep_fn(1.0)
    return response, usages
