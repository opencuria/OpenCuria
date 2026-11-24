"""Standalone skills move into plugins and keep their IDs (migration 0017)."""

from __future__ import annotations

import importlib
import uuid

import pytest
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection

from apps.organizations.models import Organization
from apps.plugins.models import OrgPluginActivation, Plugin, PluginSkill

migration = importlib.import_module(
    "apps.plugins.migrations.0017_personal_plugins_and_skills"
)


def _create_legacy_table(rows: list[tuple]) -> None:
    """Recreate the dropped ``skills_skill`` table with *rows*."""
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE TABLE skills_skill ("
            "id char(32) NOT NULL PRIMARY KEY, "
            "name varchar(255) NOT NULL, "
            "body text NOT NULL, "
            "user_id integer NULL, "
            "organization_id char(32) NULL, "
            "created_by_id integer NULL)"
        )
        cursor.executemany(
            "INSERT INTO skills_skill "
            "(id, name, body, user_id, organization_id, created_by_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )


def _migrate() -> None:
    # Not entered: SQLite refuses an entered schema editor inside the test
    # transaction, and the migration only needs ``connection``/``execute``.
    migration.migrate_skills(apps, connection.schema_editor(atomic=False))


@pytest.mark.django_db
def test_migrate_skills_moves_skills_into_plugins_and_keeps_ids():
    user = get_user_model().objects.create_user(email="legacy@example.test")
    org = Organization.objects.create(name="Legacy", slug="legacy-org")
    # An existing organization plugin already uses the default slug.
    Plugin.objects.create(
        name="Organization skills", slug="organization-skills", organization=org
    )
    org_skill, personal_skill, plain_skill = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    _create_legacy_table(
        [
            (
                org_skill.hex,
                "Release",
                "# Release\n\nTag and ship.",
                None,
                org.id.hex,
                user.id,
            ),
            (
                personal_skill.hex,
                "Python style",
                "---\nname: python-style\ndescription: 'Use for Python code.'\n"
                "---\n\nUse type hints.",
                user.id,
                None,
                user.id,
            ),
            (plain_skill.hex, "Tests", "Prefer pytest.", user.id, None, user.id),
        ]
    )

    _migrate()

    assert "skills_skill" not in connection.introspection.table_names()
    shared = Plugin.objects.get(organization=org, slug="organization-skills-2")
    assert shared.name == "Organization skills"
    assert shared.created_by_id == user.id
    assert OrgPluginActivation.objects.filter(organization=org, plugin=shared).exists()
    release = PluginSkill.objects.get(id=org_skill)
    assert (release.plugin_id, release.slug) == (shared.id, "release")
    assert release.description == "Release"
    assert release.body == "# Release\n\nTag and ship."

    personal = Plugin.objects.get(owner=user)
    assert (personal.name, personal.slug, personal.scope) == (
        "My skills",
        "my-skills",
        "personal",
    )
    assert not OrgPluginActivation.objects.filter(plugin=personal).exists()
    style = PluginSkill.objects.get(id=personal_skill)
    assert style.plugin_id == personal.id
    assert style.description == "Use for Python code."
    assert style.body == "Use type hints."
    assert PluginSkill.objects.get(id=plain_skill).description == "Prefer pytest."
    assert list(
        personal.plugin_skills.order_by("position").values_list("name", flat=True)
    ) == ["Python style", "Tests"]


@pytest.mark.django_db
def test_migrate_skills_without_legacy_table_is_a_noop():
    before = Plugin.objects.count()

    _migrate()

    assert Plugin.objects.count() == before


@pytest.mark.django_db
def test_migrate_skills_reads_folded_frontmatter_descriptions():
    user = get_user_model().objects.create_user(email="folded@example.test")
    skill_id = uuid.uuid4()
    _create_legacy_table(
        [
            (
                skill_id.hex,
                "Deploy",
                "---\nname: deploy\ndescription: >\n  Use when deploying\n"
                "  the app.\n---\n\nRun deploy.",
                user.id,
                None,
                user.id,
            )
        ]
    )

    _migrate()

    skill = PluginSkill.objects.get(id=skill_id)
    assert skill.body == "Run deploy."
    assert skill.description == "Use when deploying the app."
