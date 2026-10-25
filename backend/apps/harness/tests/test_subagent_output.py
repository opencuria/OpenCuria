"""Persistent subagent results remain recoverable through normal workspace tools."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

import pytest

from apps.harness.access.file_types import FileContent
from apps.harness.permissions.evaluator import PermissionEvaluator
from apps.harness.providers.base import ChatOptions, Delta, LLMMessage, ToolSchema
from apps.harness.runner import HarnessRunner
from apps.harness.tests.conftest import FakeAccessor
from apps.harness.tests.test_harness_service import (
    FakeProvider,
    _db_create_session,
    _service,
    _tool_step,
)
from apps.harness.tests.test_subagent_lifecycle import (
    assistant_for,
    children,
    finish,
    start,
    task_parts,
)
from apps.harness.tools.spill import SPILL_DIR

pytestmark = pytest.mark.django_db(transaction=True)
CONCLUSION = "The conclusion beyond the preview is now available."
CHILD_OUTPUT = "\n".join(f"Finding {i}: supporting evidence." for i in range(400))
CHILD_OUTPUT += f"\n{CONCLUSION}"


class SpillReadingProvider(FakeProvider):
    """Delegate once, then read the omitted conclusion from the returned file."""

    def __init__(self) -> None:
        super().__init__([])
        self.path = ""

    async def chat_stream(
        self,
        model: str,
        messages: list[LLMMessage],
        tools: list[ToolSchema],
        opts: ChatOptions | None = None,
    ) -> AsyncIterator[Delta]:
        """Use the spill path exactly as a parent model would."""
        prompt = next(m.content for m in reversed(messages) if m.role == "user")
        if prompt == "child":
            yield Delta(text=CHILD_OUTPUT)
            return
        if prompt != "parent":
            yield Delta(text="summary")
            return
        outputs = [m for m in messages if m.role == "tool"]
        if not outputs:
            step = _tool_step(
                "task",
                {"description": "research", "prompt": "child", "agent": "explore"},
                call_id="delegate",
            )
        elif len(outputs) == 1:
            match = re.search(r"Full output: (\S+\.log)\.", outputs[0].content)
            if match is None:
                yield Delta(text="Missing full output path")
                return
            self.path = match.group(1)
            step = _tool_step(
                "read",
                {"path": self.path, "offset": 400, "limit": 1},
                call_id="recover",
            )
        else:
            yield Delta(text=outputs[-1].content)
            return
        for delta in step:
            yield delta


@pytest.mark.parametrize("mode", ["build", "plan"])
@pytest.mark.parametrize("read_allowed", [True, False])
async def test_persistent_parent_can_recover_output_subject_to_read_permissions(
    harness_workspace, mode: str, read_allowed: bool
) -> None:
    """A real parent/child run persists the preview and gates recovery normally."""
    provider = SpillReadingProvider()
    service, _, _ = _service(provider=provider)

    class ReadTrackingAccessor(FakeAccessor):
        """Record actual reads to distinguish execution from permission denial."""

        def __init__(self) -> None:
            super().__init__(str(harness_workspace.id))
            self.reads: list[str] = []

        async def read_file(
            self, path: str, max_size: int | None = None
        ) -> FileContent:
            if path.startswith(f"{SPILL_DIR}/"):
                self.reads.append(path)
            return await super().read_file(path, max_size)

    accessor = ReadTrackingAccessor()

    async def workspace_accessor(workspace_id: str) -> FakeAccessor:
        assert workspace_id == str(harness_workspace.id)
        return accessor

    service._accessor_factory = workspace_accessor
    service.permissions.evaluator = PermissionEvaluator(
        global_rules={"*": "allow", "read": "allow" if read_allowed else "deny"}
    )
    service._runner_factory = lambda **kwargs: HarnessRunner(
        evaluator=service.permissions.evaluator, **kwargs
    )
    parent = await _db_create_session(harness_workspace)
    parent.mode = mode
    parent.save(update_fields=["mode"])
    assistant, task = await start(service, parent)
    await finish(task)

    assistant.refresh_from_db()
    assert assistant.finish == "stop", assistant.error
    child = children(parent)[0]
    assert child.agent_name == "explore"
    assert child.mode == mode
    assert assistant_for(child).content == CHILD_OUTPUT
    assert accessor.written == {provider.path: CHILD_OUTPUT.encode("utf-8")}
    delegate = next(p for p in task_parts(parent) if p.call_id == "delegate")
    assert delegate.state == "completed"
    assert f'<task id="{child.id}" state="completed">' in delegate.output
    assert f"Full output: {provider.path}." in delegate.output
    assert CONCLUSION not in delegate.output
    recovery = next(p for p in task_parts(parent) if p.call_id == "recover")
    if read_allowed:
        assert recovery.state == "completed"
        assert CONCLUSION in assistant.content
        assert accessor.reads == [provider.path]
    else:
        assert recovery.state == "error"
        assert "denied" in assistant.content.lower()
        assert CONCLUSION not in assistant.content
        assert not accessor.reads

    history = await service.history.build(parent)
    assert any(
        m.role == "tool" and f"Full output: {provider.path}." in m.content
        for m in history
    )
