"""Tests for ``RunTimeline`` and ``ToolCallParts``.

Characterization tests pin the part lookups used while persisting runner
events; the boundary test keeps run-event persistence below the
``HarnessService`` orchestration that composes it.
"""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path
from typing import Any

import pytest

from apps.harness.models import HarnessMessage, HarnessPart, HarnessSession
from apps.harness.repositories import (
    HarnessMessageRepository,
    HarnessPartRepository,
    HarnessSessionRepository,
)
from apps.harness.run_timeline import RunTimeline
from apps.harness.tool_call_parts import ToolCallParts

BACKEND_ROOT = Path(__file__).resolve().parents[3]
HARNESS_ROOT = BACKEND_ROOT / "apps" / "harness"
SERVICE_MODULE = "apps.harness.harness_service"


def _imports(path: Path) -> list[tuple[str, str]]:
    """Return ``(module, name)`` pairs, including function-local imports."""
    package = ".".join(path.parent.relative_to(BACKEND_ROOT).parts)
    imported: list[tuple[str, str]] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imported.extend((alias.name, "") for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = resolve_name("." * node.level + (node.module or ""), package)
            imported.extend((module, alias.name) for alias in node.names)
    return imported


def test_run_timeline_stays_below_harness_service() -> None:
    """Timeline persistence never reaches back into its orchestrating service."""
    violations = sorted(
        f"{name}.py -> {module}"
        for name in ("run_timeline", "tool_call_parts")
        for module, _ in _imports(HARNESS_ROOT / f"{name}.py")
        if module == SERVICE_MODULE or module.startswith(f"{SERVICE_MODULE}.")
    )
    assert violations == [], "\n".join(violations)


def test_no_module_imports_harness_service_privates() -> None:
    """Shared behavior gets a public home instead of a private service import."""
    violations = sorted(
        f"{path.relative_to(HARNESS_ROOT.parent)} -> {name}"
        for path in HARNESS_ROOT.parent.rglob("*.py")
        if "tests" not in path.parts and "migrations" not in path.parts
        for module, name in _imports(path)
        if module == SERVICE_MODULE and name.startswith("_")
    )
    assert violations == [], "\n".join(violations)


def _session(harness_workspace) -> HarnessSession:  # type: ignore[no-untyped-def]
    return HarnessSessionRepository.create(
        workspace_id=harness_workspace.id,
        organization_id=harness_workspace.runner.organization_id,
        title="timeline",
        agent_name="build",
        mode="build",
        model="fake-model",
    )


def _assistant(session: HarnessSession) -> HarnessMessage:
    return HarnessMessageRepository.create(
        session_id=session.id, role="assistant", content=""
    )


def _tool_calls(runs: dict[str, dict[str, Any]]) -> ToolCallParts:
    return ToolCallParts(
        parts=HarnessPartRepository, run_context=lambda key: runs.get(key, {})
    )


def _timeline() -> RunTimeline:
    async def _emit(event: str, data: dict[str, Any], workspace_id: str) -> None:
        return None

    return RunTimeline(
        messages=HarnessMessageRepository,
        parts=HarnessPartRepository,
        run_context=lambda key: {},
        emit=_emit,
    )


@pytest.mark.django_db(transaction=True)
async def test_tool_part_lookup_prefers_run_context_then_call_id(
    harness_workspace,
) -> None:
    """Run-context ids win; otherwise the call id is matched in the message."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    indexed = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", call_id="other"
    )
    by_call = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", call_id="call-1"
    )
    tool_calls = _tool_calls({})

    found = await tool_calls._lookup_tool_part(
        {"tool_parts": {"call-1": str(indexed.id)}}, assistant, "call-1"
    )
    assert found is not None and found.id == indexed.id
    found = await tool_calls._lookup_tool_part({}, assistant, "call-1")
    assert found is not None and found.id == by_call.id
    assert await tool_calls._lookup_tool_part({}, assistant, "missing") is None


@pytest.mark.django_db(transaction=True)
async def test_rebind_provisional_tool_part_picks_oldest_pending_match(
    harness_workspace,
) -> None:
    """A real call id adopts the oldest pending part of the same step + tool."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="running",
        input={"tool": "bash"},
        meta={"step": 1},
    )
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="pending",
        input={"tool": "read"},
        meta={"step": 1},
    )
    oldest = HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="pending",
        input={"tool": "bash"},
        meta={"step": 1},
    )
    HarnessPartRepository.create(
        message_id=assistant.id,
        type="tool",
        state="pending",
        input={"tool": "bash"},
        meta={"step": 1},
    )
    tool_calls = _tool_calls({})

    match = await tool_calls._rebind_provisional_tool_part(
        {}, assistant, step=1, tool="bash", call_id="real"
    )
    assert match is not None and match.id == oldest.id
    assert (
        await tool_calls._rebind_provisional_tool_part(
            {}, assistant, step=2, tool="bash", call_id="real"
        )
        is None
    )


@pytest.mark.django_db(transaction=True)
async def test_tool_finish_adopts_queued_part_call_id(
    harness_workspace,
) -> None:
    """A completion carrying ``queued_id`` rebinds the queued row's call id."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    queued = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", state="pending", call_id="tmp-1"
    )
    runs: dict[str, dict[str, Any]] = {str(session.id): {"tool_parts": {}}}

    part = await _tool_calls(runs).record_finished(
        assistant,
        {"call_id": "real-1", "queued_id": "tmp-1", "output": "ok"},
        state="completed",
    )

    queued.refresh_from_db()
    assert part.id == queued.id
    assert queued.call_id == "real-1"
    assert queued.state == "completed"
    assert queued.output == "ok"
    assert runs[str(session.id)]["tool_parts"]["real-1"] == str(queued.id)
    assert HarnessPart.objects.filter(message_id=assistant.id).count() == 1


@pytest.mark.django_db(transaction=True)
async def test_settle_open_stream_parts_only_closes_text_and_reasoning(
    harness_workspace,
) -> None:
    """Leftover streams complete; open tools and finished parts are untouched."""
    session = _session(harness_workspace)
    assistant = _assistant(session)
    text = HarnessPartRepository.create(
        message_id=assistant.id, type="text", state="running"
    )
    reasoning = HarnessPartRepository.create(
        message_id=assistant.id, type="reasoning", state="pending"
    )
    tool = HarnessPartRepository.create(
        message_id=assistant.id, type="tool", state="running"
    )
    errored = HarnessPartRepository.create(
        message_id=assistant.id, type="text", state="error"
    )

    await _timeline().settle_open_stream_parts(assistant)

    states = {
        part.id: part.state
        for part in HarnessPart.objects.filter(message_id=assistant.id)
    }
    assert states == {
        text.id: "completed",
        reasoning.id: "completed",
        tool.id: "running",
        errored.id: "error",
    }
