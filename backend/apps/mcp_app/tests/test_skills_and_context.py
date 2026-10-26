"""Skill management, name resolution and the orientation tool."""

from __future__ import annotations

import uuid

import pytest
from asgiref.sync import sync_to_async

from apps.accounts.models import User
from apps.organizations.models import Membership, MembershipRole

from .conftest import fails, ok

pytestmark = pytest.mark.django_db(transaction=True)


async def test_skill_lifecycle_by_name(world):
    ctx = world.ctx()

    created = await ok(
        ctx, "skill_create", name="Python style", body="Use type hints everywhere."
    )
    assert created["scope"] == "personal"

    listed = await ok(ctx, "skill_list", query="type hints")
    assert [s["name"] for s in listed["skills"]] == ["Python style"]
    assert listed["skills"][0]["preview"] == "Use type hints everywhere."

    fetched = await ok(ctx, "skill_get", skill="python STYLE")
    assert fetched["body"] == "Use type hints everywhere."

    updated = await ok(ctx, "skill_update", skill=created["skill_id"], body="Use mypy.")
    assert updated["body"] == "Use mypy."

    deleted = await ok(ctx, "skill_delete", skill="Python style")
    assert deleted["deleted"] is True
    assert (await ok(ctx, "skill_list"))["total"] == 0


async def test_organization_skills_require_admin(world):
    member_error = await fails(
        world.ctx(),
        "skill_create",
        name="Shared",
        body="Body",
        scope="organization",
    )
    assert "Only organization admins" in member_error

    created = await ok(
        world.ctx(role="admin"),
        "skill_create",
        name="Shared",
        body="Body",
        scope="organization",
    )
    assert created["scope"] == "organization"
    # Members see organization skills but cannot change them.
    assert (await ok(world.ctx(), "skill_get", skill="Shared"))["scope"] == (
        "organization"
    )
    error = await fails(world.ctx(), "skill_update", skill="Shared", name="Mine")
    assert "Not allowed" in error


async def test_unknown_and_ambiguous_names_explain_how_to_recover(world):
    ctx = world.ctx()
    await ok(ctx, "skill_create", name="Review", body="A")
    await ok(ctx, "skill_create", name="review", body="B")
    await ok(ctx, "skill_create", name="Release checklist", body="C")

    ambiguous = await fails(ctx, "skill_get", skill="REVIEW")
    assert "2 skills match 'REVIEW'" in ambiguous
    assert "Pass the skill ID" in ambiguous

    unknown = await fails(ctx, "skill_get", skill="Releas")
    assert "Did you mean: 'Release checklist'" in unknown
    assert "skill_list" in unknown


async def test_other_users_skills_are_invisible(world):
    owner_skill = await ok(world.ctx(), "skill_create", name="Private", body="X")

    def make_stranger():
        stranger = User.objects.create_user(email=f"s-{uuid.uuid4().hex[:8]}@x.com")
        Membership.objects.create(
            user=stranger, organization=world.org, role=MembershipRole.MEMBER
        )
        return stranger

    stranger = await sync_to_async(make_stranger)()
    error = await fails(
        world.ctx(user=stranger), "skill_delete", skill=owner_skill["skill_id"]
    )
    assert "No skill" in error


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
