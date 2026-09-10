"""Tests for AgentConfig runtime (service validation + strategy + subagent routing)."""
from __future__ import annotations

import uuid

import pytest

from apps.harness.agents.effort_strategy import resolve_strategy_effort


def test_resolve_strategy_effort() -> None:
    assert resolve_strategy_effort(["low", "med", "high"], "lowest") == "low"
    assert resolve_strategy_effort(["low", "med", "high"], "medium") == "med"
    assert resolve_strategy_effort(["low", "med", "high"], "highest") == "high"
    assert resolve_strategy_effort(["low"], "inherit") == ""
    assert resolve_strategy_effort([], "lowest") == ""


@pytest.mark.django_db(transaction=True)
def test_agent_config_service_validation(harness_workspace) -> None:
    from apps.harness.services import AgentConfigService

    org_id = harness_workspace.runner.organization_id
    svc = AgentConfigService()
    # primary forces fixed
    rows = svc.save_configs(
        org_id,
        [{"agent": "build", "model": "m-build", "effort": "high",
          "inherit_model": True, "effort_strategy": "inherit"}],
    )
    build = next(r for r in rows if r["agent"] == "build")
    assert build["inherit_model"] is False
    assert build["effort_strategy"] == "fixed"
    assert build["model"] == "m-build"
    # subagent inherit clears model/effort
    rows = svc.save_configs(
        org_id,
        [{"agent": "general", "model": "m-x", "effort": "high",
          "inherit_model": True, "effort_strategy": "fixed"}],
    )
    general = next(r for r in rows if r["agent"] == "general")
    assert general["inherit_model"] is True
    assert general["model"] == ""
    assert general["effort"] == ""
    assert general["effort_strategy"] == "inherit"
    # unknown agent rejected
    with pytest.raises(ValueError, match="Unknown agent"):
        svc.save_configs(org_id, [{"agent": "nope", "model": "m"}])


@pytest.mark.django_db(transaction=True)
async def test_run_subagent_inherit_uses_parent_model(harness_workspace, monkeypatch) -> None:
    """Inherit-mode subagent reuses parent model; lowest strategy resolves catalog."""
    from apps.harness.harness_service import HarnessService
    from apps.harness.permissions.evaluator import PermissionEvaluator
    from apps.harness.permissions.service import PermissionService
    from apps.harness.providers.base import ProviderAdapter, Delta, ChatOptions, LLMMessage, ToolSchema, Usage
    from apps.harness.repositories import HarnessSessionRepository
    from apps.harness.services import AgentConfigService, ProviderConfigService
    from apps.harness.tools.subagents import TaskArgs
    from collections.abc import AsyncIterator

    class FakeProvider(ProviderAdapter):
        name = "fake"

        async def chat_stream(self, model, messages, tools, opts=None):  # type: ignore[no-untyped-def]
            yield Delta(text="child output", usage=Usage(1, 1, 2))

    org_id = harness_workspace.runner.organization_id
    ProviderConfigService().save_config(
        organization_id=org_id, api_key="k", base_url="https://example.com",
        default_model="big-model", small_model="small-model",
    )
    AgentConfigService().save_configs(org_id, [
        {"agent": "build", "model": "big-model", "effort": "high",
         "inherit_model": False, "effort_strategy": "fixed"},
        {"agent": "general", "inherit_model": True, "effort_strategy": "inherit"},
        {"agent": "explore", "inherit_model": True, "effort_strategy": "lowest"},
    ])
    # fake catalog for explore lowest strategy
    from apps.harness.providers.models_catalog import ProviderModel
    monkeypatch.setattr(
        ProviderConfigService, "list_models",
        lambda self, oid: [ProviderModel(id="big-model", name="B",
                                         reasoning_efforts=("low", "high"),
                                         default_effort="high",
                                         supports_tools=True,
                                         context_length=1000,
                                         max_output_tokens=100)],
    )

    async def _emit(event: str, data: dict) -> None:
        return None

    service = HarnessService(
        permissions=PermissionService(evaluator=PermissionEvaluator(global_rules={"*": "allow"})),
        emit=_emit,
        provider_factory=lambda _org: FakeProvider(),
    )
    parent = HarnessSessionRepository.create(
        workspace_id=harness_workspace.id, organization_id=org_id,
        title="parent", agent_name="build", mode="build",
        model="big-model", reasoning_effort="high",
    )
    parent_assistant = service.messages.create(session_id=parent.id, role="assistant", content="")
    service._runs[str(parent.id)] = {"session_id": str(parent.id),
                                     "message_id": str(parent_assistant.id),
                                     "tool_parts": {}, "subtask_parts": {}}
    ctx = type("Ctx", (), {"session_id": str(parent.id),
                           "workspace_id": str(harness_workspace.id),
                           "model": "big-model", "depth": 0, "max_depth": 1})()
    # inherit strategy: effort = parent effort
    args = TaskArgs(description="r", prompt="look", subagent_type="general")
    result = await service._run_subagent_tool(
        parent=parent, args=args, ctx=ctx, subtask_id="s1", organization_id=org_id)
    assert "child output" in result.output
    from apps.harness.models import HarnessSession
    child = HarnessSession.objects.filter(parent_id=parent.id).order_by("created_at").last()
    assert child is not None and child.model == "big-model"
    assert child.reasoning_effort == "high"
    # lowest strategy on explore: effort = first catalog effort
    args2 = TaskArgs(description="r", prompt="look", subagent_type="explore")
    await service._run_subagent_tool(
        parent=parent, args=args2, ctx=ctx, subtask_id="s2", organization_id=org_id)
    child2 = HarnessSession.objects.filter(parent_id=parent.id).order_by("created_at").last()
    assert child2 is not None and child2.model == "big-model"
    assert child2.reasoning_effort == "low"
    # model_override wins
    args3 = TaskArgs(description="r", prompt="look", subagent_type="general",
                     model_override="override-model")
    await service._run_subagent_tool(
        parent=parent, args=args3, ctx=ctx, subtask_id="s3", organization_id=org_id)
    child3 = HarnessSession.objects.filter(parent_id=parent.id).order_by("created_at").last()
    assert child3 is not None and child3.model == "override-model"
