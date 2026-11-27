"""Plugin skill selection by name and the orientation tool."""

from __future__ import annotations

import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.accounts.models import User
from apps.harness.harness_service import HarnessService
from apps.harness.models import HarnessSession
from apps.mcp_app import resolve
from apps.mcp_app.errors import ToolError
from apps.organizations.models import Membership, MembershipRole

from .conftest import fails, ok

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def runs(monkeypatch) -> list:
    """Record started runs instead of executing an agent."""
    started: list = []

    async def start_run(self, target, prompt, **kwargs):
        started.append(kwargs["skill_ids"])

    monkeypatch.setattr(HarnessService, "start_run", start_run)
    return started


async def _personal_plugin(ctx, name: str, *skills: str) -> dict:
    return await ok(
        ctx,
        "plugin_create",
        name=name,
        skills=[{"name": skill, "body": f"{skill} body."} for skill in skills],
        enable=False,
    )


def _make_member() -> User:
    return User.objects.create_user(email=f"s-{uuid.uuid4().hex[:8]}@x.com")


async def test_chat_skills_resolve_personal_plugin_skills_by_name_or_slug(world, runs):
    ctx = world.ctx()
    plugin = await _personal_plugin(ctx, "Style", "Python style")
    skill_id = plugin["skills"][0]["skill_id"]

    by_name = await ok(
        ctx, "chat_start", workspace="billing-api", prompt="A", skills=["python STYLE"]
    )
    by_slug = await ok(
        ctx, "chat_start", workspace="billing-api", prompt="B", skills=["python-style"]
    )

    assert by_name["chat"]["skills"] == ["Python style"]
    assert by_slug["chat"]["skills"] == ["Python style"]
    assert runs == [[skill_id], [skill_id]]


async def test_unknown_and_ambiguous_skill_names_explain_how_to_recover(world, runs):
    ctx = world.ctx()
    await _personal_plugin(ctx, "Backend", "Review")
    await _personal_plugin(ctx, "Frontend", "review", "Release checklist")

    ambiguous = await fails(
        ctx, "chat_start", workspace="billing-api", prompt="A", skills=["REVIEW"]
    )
    unknown = await fails(
        ctx, "chat_start", workspace="billing-api", prompt="A", skills=["Releas"]
    )

    assert "2 skills match 'REVIEW'" in ambiguous
    assert "Pass the skill ID" in ambiguous
    assert "Did you mean: 'Release checklist'" in unknown
    assert "plugin_list" in unknown
    assert runs == []
    assert await sync_to_async(HarnessSession.objects.count)() == 0


async def test_other_users_and_unreleased_skills_are_not_selectable(world, runs):
    owner_plugin = await _personal_plugin(world.ctx(), "Private", "Secret sauce")
    admin = world.ctx(role="admin")
    shared = await ok(
        admin,
        "plugin_create",
        name="Team",
        scope="organization",
        skills=[{"name": "Team rules", "body": "Be kind."}],
        enable=False,
    )

    def make_stranger() -> User:
        stranger = _make_member()
        Membership.objects.create(
            user=stranger, organization=world.org, role=MembershipRole.MEMBER
        )
        return stranger

    stranger = world.ctx(user=await sync_to_async(make_stranger)())
    owner_skill_id = owner_plugin["skills"][0]["skill_id"]
    with pytest.raises(ToolError, match="No skill") as hidden:
        await sync_to_async(resolve.skill_ids)(stranger, [owner_skill_id])
    assert "Secret sauce" not in str(hidden.value)
    assert "No plugin 'Private'" in await fails(
        stranger, "plugin_get", plugin="Private"
    )

    # Organization plugin skills are selectable only while released.
    await ok(admin, "plugin_set_active", plugin="Team", active=False, organization=True)
    withdrawn = await fails(
        world.ctx(),
        "chat_start",
        workspace="billing-api",
        prompt="A",
        skills=["Team rules"],
    )
    assert "No skill 'Team rules' found" in withdrawn

    await ok(admin, "plugin_set_active", plugin="Team", active=True, organization=True)
    started = await ok(
        world.ctx(),
        "chat_start",
        workspace="billing-api",
        prompt="A",
        skills=["Team rules"],
    )
    assert started["chat"]["skills"] == ["Team rules"]
    assert runs == [[shared["skills"][0]["skill_id"]]]


async def test_context_reports_workspace_scope_and_counts(world):
    data = await ok(world.ctx(), "opencuria_context")

    assert data["organization"]["name"] == world.org.name
    assert data["organization"]["role"] == "member"
    assert data["current_workspace_id"] == str(world.workspace.id)
    assert data["counts"] == {
        "workspaces": 1,
        "workspaces_running": 1,
        "chats": 0,
        "chats_running": 0,
        "chats_waiting_for_input": 0,
        "scheduled_tasks": 0,
        "skills": 0,
    }
    assert {engine["engine"] for engine in data["engines"]} == {
        "native",
        "claude",
        "codex",
    }


async def test_context_counts_usable_plugin_skills(world):
    await _personal_plugin(world.ctx(), "Mine", "One", "Two")
    admin = world.ctx(role="admin")
    await ok(
        admin,
        "plugin_create",
        name="Shared",
        scope="organization",
        skills=[{"name": "Three", "body": "Three body."}],
        enable=False,
    )
    assert (await ok(world.ctx(), "opencuria_context"))["counts"]["skills"] == 3

    await ok(
        admin, "plugin_set_active", plugin="Shared", active=False, organization=True
    )
    assert (await ok(world.ctx(), "opencuria_context"))["counts"]["skills"] == 2
