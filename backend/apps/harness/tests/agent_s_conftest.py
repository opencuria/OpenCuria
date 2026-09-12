"""Shared fakes for the isolated Agent-S3 core tests (no network/ORM)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from apps.harness.agent_s.ports import (
    CallPurpose,
    CodeExecutionPort,
    Completion,
    CompletionPort,
    OcrPort,
    Usage,
)

TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@dataclass
class ScriptedCompletion(CompletionPort):
    """completion port replaying scripted texts (optionally failing first)."""

    scripts: dict[CallPurpose, list[str]] = field(default_factory=dict)
    failures: dict[CallPurpose, int] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    usages: list[Usage] = field(default_factory=list)
    default_text: str = "agent.wait(1.0)"

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        purpose: CallPurpose,
        temperature: float,
        use_thinking: bool,
    ) -> Completion:
        # Copy messages so later local appends do not mutate the record.
        snapshot = [
            {"role": m["role"], "content": [dict(p) for p in m["content"]]}
            for m in messages
        ]
        self.calls.append(
            {
                "purpose": purpose,
                "temperature": temperature,
                "use_thinking": use_thinking,
                "messages": snapshot,
            }
        )
        remaining_failures = self.failures.get(purpose, 0)
        if remaining_failures > 0:
            self.failures[purpose] = remaining_failures - 1
            raise RuntimeError(f"injected {purpose} failure")
        queue = self.scripts.get(purpose, [])
        text = queue.pop(0) if queue else self.default_text
        usage = Usage(input_tokens=1, output_tokens=1)
        self.usages.append(usage)
        return Completion(text=text, usage=usage)

    def calls_for(self, purpose: CallPurpose) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["purpose"] == purpose]


@dataclass
class StubOcr(OcrPort):
    rows: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0

    async def read_words(self, screenshot: bytes) -> list[dict[str, Any]]:
        self.calls += 1
        return [dict(r) for r in self.rows]


@dataclass
class StubCodeExecution(CodeExecutionPort):
    results: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run_python(self, code: str) -> dict[str, Any]:
        self.calls.append({"kind": "python", "code": code})
        if self.results:
            return self.results.pop(0)
        return {"status": "ok", "output": "ok", "error": "", "return_code": 0}

    async def run_bash(self, code: str, timeout: int = 30) -> dict[str, Any]:
        self.calls.append({"kind": "bash", "code": code, "timeout": timeout})
        if self.results:
            return self.results.pop(0)
        return {"status": "ok", "output": "ok", "error": "", "returncode": 0}


@pytest.fixture
def png() -> bytes:
    return TINY_PNG


@pytest.fixture
def obs(png: bytes) -> dict[str, Any]:
    return {"screenshot": png}


@pytest.fixture
def no_sleep():
    async def _sleep(_delay: float) -> None:
        return None

    return _sleep


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)
