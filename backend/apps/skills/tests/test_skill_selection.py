"""Skill selection: ID normalization, visible body resolution and merging."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.db.models.sql.compiler import SQLCompiler

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


@pytest.mark.django_db
def test_filter_available_ids_drops_deleted_hidden_and_invalid_ids(skill_setup):
    personal_id = str(skill_setup["personal"].id)
    org_id = str(skill_setup["org_skill"].id)
    owner = skill_setup["owner"]
    deleted = Skill.objects.create(name="Gone", body="x", user=owner, created_by=owner)
    deleted_id = str(deleted.id)
    deleted.delete()
    available = SkillService().filter_available_ids(
        [
            personal_id,
            deleted_id,
            str(skill_setup["foreign"].id),
            str(skill_setup["other_org"].id),
            "nope",
            org_id,
            personal_id,
        ],
        user_id=skill_setup["owner"].id,
        organization_id=skill_setup["org"].id,
    )
    assert available == [personal_id, org_id]


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["resolve_bodies", "filter_available_ids"])
def test_selection_reads_only_requested_visible_rows(
    skill_setup: dict[str, Any],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
    django_assert_num_queries: Any,
) -> None:
    """Database rows and queries must depend on selection, not catalogue size."""
    Skill.objects.bulk_create(
        [
            Skill(
                name=f"Unselected {index}",
                body="Unused",
                organization=skill_setup["org"],
            )
            for index in range(500)
        ]
    )
    selected = [skill_setup["personal"], skill_setup["org_skill"]]
    ids = [str(skill.id) for skill in selected]
    rows = []
    original = SQLCompiler.results_iter

    def count_rows(compiler: SQLCompiler, *args: Any, **kwargs: Any) -> Iterator[Any]:
        for row in original(compiler, *args, **kwargs):
            if compiler.query.model is Skill:
                rows.append(row)
            yield row

    monkeypatch.setattr(SQLCompiler, "results_iter", count_rows)
    with django_assert_num_queries(1):
        result = getattr(SkillService(), operation)(
            ids,
            user_id=skill_setup["owner"].id,
            organization_id=skill_setup["org"].id,
        )
    expected = (
        [f"## {skill.name}\n{skill.body.strip()}" for skill in selected]
        if operation == "resolve_bodies"
        else ids
    )
    assert result == expected
    assert len(rows) == len(selected)


@pytest.mark.django_db
def test_resolve_preserves_duplicate_ids_and_request_order(
    skill_setup: dict[str, Any], django_assert_num_queries: Any
) -> None:
    """The query may deduplicate IDs; rendered prompt fragments must not."""
    personal_id = str(skill_setup["personal"].id)
    org_id = str(skill_setup["org_skill"].id)
    with django_assert_num_queries(1):
        assert _resolve(skill_setup, [personal_id, org_id, personal_id.upper()]) == [
            "## Personal\nPrefer pytest.",
            "## Org\nUse type hints.",
            "## Personal\nPrefer pytest.",
        ]


@pytest.mark.django_db
@pytest.mark.parametrize("ids", [[], ["", " ", "invalid"]])
def test_filter_without_valid_ids_does_not_read_catalogue(
    skill_setup: dict[str, Any], ids: list[str], django_assert_num_queries: Any
) -> None:
    """Empty or entirely invalid saved selections need no database work."""
    with django_assert_num_queries(0):
        assert (
            SkillService().filter_available_ids(
                ids,
                user_id=skill_setup["owner"].id,
                organization_id=skill_setup["org"].id,
            )
            == []
        )


@pytest.mark.django_db
def test_invalid_resolve_ids_are_rejected_before_database_reads(
    skill_setup: dict[str, Any], django_assert_num_queries: Any
) -> None:
    """Validate the entire input before reporting visibility errors."""
    with django_assert_num_queries(0):
        with pytest.raises(ValueError, match="Invalid skill_id: invalid"):
            _resolve(skill_setup, [str(skill_setup["foreign"].id), "invalid"])
        assert _resolve(skill_setup, []) == []


@pytest.mark.django_db
def test_filter_preserves_normalized_uuid_spelling(
    skill_setup: dict[str, Any],
) -> None:
    """Saved IDs deduplicate by trimmed string, retaining UUID spelling."""
    skill_id = skill_setup["personal"].id
    variants = [str(skill_id), skill_id.hex.upper(), "{" + str(skill_id) + "}"]
    assert (
        SkillService().filter_available_ids(
            [f" {variants[0]} ", *variants],
            user_id=skill_setup["owner"].id,
            organization_id=skill_setup["org"].id,
        )
        == variants
    )


@pytest.mark.django_db
def test_selection_observes_content_visibility_and_deletion_changes(
    skill_setup: dict[str, Any],
) -> None:
    """Each call uses current database authority and content without a cache."""
    skill = skill_setup["personal"]
    skill_id = str(skill.id)
    service = SkillService()
    scope = {
        "user_id": skill_setup["owner"].id,
        "organization_id": skill_setup["org"].id,
    }
    assert service.resolve_bodies([skill_id], **scope) == [
        "## Personal\nPrefer pytest."
    ]
    skill.body = "Updated guidance."
    skill.save(update_fields=["body"])
    assert service.resolve_bodies([skill_id], **scope) == [
        "## Personal\nUpdated guidance."
    ]
    # Personal ownership works across organizations; org ownership is scoped.
    other_scope = {**scope, "organization_id": skill_setup["other_org"].organization_id}
    assert service.filter_available_ids([skill_id], **other_scope) == [skill_id]
    skill.user_id = None
    skill.organization_id = other_scope["organization_id"]
    skill.save(update_fields=["user_id", "organization_id"])
    assert service.filter_available_ids([skill_id], **scope) == []
    with pytest.raises(ValueError, match=f"not accessible: {skill_id}"):
        service.resolve_bodies([skill_id], **scope)
    skill.delete()
    assert service.filter_available_ids([skill_id], **other_scope) == []


def test_merge_bodies_puts_plugin_skills_first_and_drops_duplicates():
    assert SkillService.merge_bodies(["b", "a", " "], ["p1", "a", "p1"]) == [
        "p1",
        "a",
        "b",
    ]
    assert SkillService.merge_bodies(None, None) == []
