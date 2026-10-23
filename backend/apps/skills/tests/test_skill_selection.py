"""Skill selection: ID normalization, visible body resolution and merging."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model

from apps.organizations.models import Organization
from apps.skills.models import Skill
from apps.skills.services import SkillService


@pytest.fixture
def skill_setup(db):
    """An org with an org skill, a personal skill and a stranger's skill."""
    user_model = get_user_model()
    org = Organization.objects.create(
        name="Skills", slug=f"skills-{uuid.uuid4().hex[:10]}"
    )
    other_org = Organization.objects.create(
        name="Other", slug=f"other-{uuid.uuid4().hex[:10]}"
    )
    owner = user_model.objects.create_user(
        email=f"owner-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    stranger = user_model.objects.create_user(
        email=f"stranger-{uuid.uuid4().hex[:6]}@example.com", password="secret"
    )
    return {
        "org": org,
        "owner": owner,
        "org_skill": Skill.objects.create(
            name="Org", body="  Use type hints.\n", organization=org, created_by=owner
        ),
        "personal": Skill.objects.create(
            name="Personal", body="Prefer pytest.", user=owner, created_by=owner
        ),
        "foreign": Skill.objects.create(
            name="Foreign", body="secret", user=stranger, created_by=stranger
        ),
        "other_org": Skill.objects.create(
            name="Other org", body="hidden", organization=other_org, created_by=owner
        ),
    }


def _resolve(setup, skill_ids):
    return SkillService().resolve_bodies(
        skill_ids, user_id=setup["owner"].id, organization_id=setup["org"].id
    )


def test_normalize_ids_trims_dedupes_and_drops_blanks():
    assert SkillService.normalize_ids([" a ", "b", "a", "", "  ", 3]) == ["a", "b", "3"]
    assert SkillService.normalize_ids(None) == []


@pytest.mark.django_db
def test_resolve_bodies_renders_visible_skills_in_request_order(skill_setup):
    bodies = _resolve(
        skill_setup,
        [str(skill_setup["personal"].id), f" {skill_setup['org_skill'].id} "],
    )
    assert bodies == ["## Personal\nPrefer pytest.", "## Org\nUse type hints."]
    assert _resolve(skill_setup, []) == []


@pytest.mark.django_db
@pytest.mark.parametrize("hidden", ["foreign", "other_org"])
def test_resolve_bodies_rejects_skills_outside_user_scope(skill_setup, hidden):
    skill_id = skill_setup[hidden].id
    with pytest.raises(ValueError, match=f"not accessible: {skill_id}"):
        _resolve(skill_setup, [str(skill_id)])


@pytest.mark.django_db
def test_resolve_bodies_rejects_invalid_and_unknown_ids(skill_setup):
    with pytest.raises(ValueError, match="Invalid skill_id: nope"):
        _resolve(skill_setup, ["nope"])
    unknown = uuid.uuid4()
    with pytest.raises(ValueError, match=f"not accessible: {unknown}"):
        _resolve(skill_setup, [str(unknown)])


def test_merge_bodies_puts_plugin_skills_first_and_drops_duplicates():
    assert SkillService.merge_bodies(["b", "a", " "], ["p1", "a", "p1"]) == [
        "p1",
        "a",
        "b",
    ]
    assert SkillService.merge_bodies(None, None) == []
