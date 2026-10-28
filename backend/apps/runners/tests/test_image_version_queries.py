"""Image presentation contracts and SQL work bounded by line kinds, not size."""

from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.accounts.models import APIKeyPermission
from apps.organizations.models import Membership, Organization
from apps.runners.image_lines import ImageLine, ImageLineRepository
from apps.runners.image_presenters import captured_image_to_out
from apps.runners.models import (
    CapturedImage,
    ImageBuildJob,
    ImageDefinition,
    ImageInstance,
)
from apps.runners.services.image_retention import ImageRetentionService
from apps.runners.services.image_versions import ImageVersionService
from apps.runners.tests.test_image_generations import finish, request
from apps.runners.tests.test_image_versions import make_line, qemu_workspace
from apps.runners.tests.test_image_versions_api import headers

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("line_count", [1, 10, 30])
@pytest.mark.parametrize("version_count", [1, 8])
def test_captured_list_has_constant_query_count(
    runner, user, django_assert_num_queries, line_count: int, version_count: int
) -> None:
    """Include schema mapping so deferred relation reads cannot hide an N+1."""
    for index in range(line_count):
        _, versions = make_line(
            runner, user, ("ready",) * version_count, name=f"Image {index:02}"
        )
        qemu_workspace(runner, user, versions[0])

    with django_assert_num_queries(6):
        rows = [
            captured_image_to_out(view)
            for view in ImageVersionService().list_captured_images(
                user, runner.organization_id
            )
        ]

    assert len(rows) == line_count
    assert [row.name for row in rows] == sorted(row.name for row in rows)
    for row in rows:
        assert len(row.versions) == version_count
        assert row.latest_version == version_count
        assert row.workspace_count == 1
        assert [version.version for version in row.versions] == list(
            range(version_count, 0, -1)
        )


def test_empty_captured_list_only_reads_visible_lines(
    runner, user, django_assert_num_queries
) -> None:
    """An empty result does not read versions, policies or workspace pins."""
    with django_assert_num_queries(1):
        assert (
            ImageVersionService().list_captured_images(user, runner.organization_id)
            == []
        )
    with django_assert_num_queries(0):
        assert ImageVersionService().version_extras([]) == {}


@pytest.mark.parametrize("line_status", ["active", "pending_deletion"])
def test_captured_list_and_detail_preserve_version_and_pin_rules(
    runner, user, line_status: str
) -> None:
    """Unavailable/newer attempts, retention and pending pins retain their meaning."""
    runner.organization.image_versions_to_keep = 1
    runner.organization.save(update_fields=["image_versions_to_keep"])
    line, versions = make_line(
        runner,
        user,
        ("ready", "ready", "ready", "failed", "capturing", "retired", "deleted"),
    )
    line.status = line_status
    line.save(update_fields=["status"])
    ImageInstance.objects.filter(pk=versions[2].id).update(runner_ref="")
    workspace = qemu_workspace(runner, user, versions[0])
    workspace.pending_base_image_instance = versions[1]
    workspace.save(update_fields=["pending_base_image_instance"])
    duplicate_pin = qemu_workspace(runner, user, versions[1], status="failed")
    duplicate_pin.pending_base_image_instance = versions[1]
    duplicate_pin.save(update_fields=["pending_base_image_instance"])
    for status in ("deleted", "removed"):
        qemu_workspace(runner, user, versions[1], status=status)
    empty = CapturedImage.objects.create(
        name="Empty", runner=runner, organization=runner.organization, created_by=user
    )
    service = ImageVersionService()
    rows = {
        view["line"].id: captured_image_to_out(view).model_dump()
        for view in service.list_captured_images(user, runner.organization_id)
    }
    assert (
        rows[line.id]
        == captured_image_to_out(service.captured_image(line)).model_dump()
    )
    assert rows[empty.id]["versions"] == []
    assert rows[empty.id]["latest_id"] is None
    row = rows[line.id]
    expected_latest = versions[1].id if line_status == "active" else None
    assert row["latest_id"] == expected_latest
    assert row["total_size_bytes"] == 2100  # Deleted v7 is excluded.
    assert row["workspace_count"] == 3  # A pin to each version counts separately.
    assert [v["version"] for v in row["versions"]] == [6, 5, 4, 3, 2, 1]
    facts = {v["id"]: v for v in row["versions"]}
    assert facts[versions[0].id]["retention"] == "expires_when_unused"
    assert facts[versions[1].id]["retention"] == (
        "latest" if line_status == "active" else "expires_when_unused"
    )
    assert facts[versions[2].id]["retention"] == "kept"
    assert all(facts[v.id]["retention"] is None for v in versions[3:6])
    assert len(facts[versions[1].id]["workspaces"]) == 2


@pytest.mark.parametrize(
    ("job_status", "definition_status", "selected_status", "has_selection"),
    [
        ("active", "active", "ready", True),
        ("deactivated", "active", "ready", True),
        ("active", "deactivated", "ready", True),
        ("active", "active", "failed", True),
        ("active", "active", "ready", False),
    ],
)
def test_bulk_latest_preserves_definition_selection(
    runner,
    user,
    django_assert_num_queries,
    job_status: str,
    definition_status: str,
    selected_status: str,
    has_selection: bool,
) -> None:
    """Definition latest is the published selection, even if a newer ready exists."""
    definition = ImageDefinition.objects.create(
        name="Recipe", organization=runner.organization
    )
    job, older, task = request(definition, runner, user)
    assert finish(job, older, task)
    job, newer, task = request(definition, runner, user)
    assert finish(job, newer, task)
    ImageBuildJob.objects.filter(pk=job.id).update(
        status=job_status, current_generation=older if has_selection else None
    )
    ImageDefinition.objects.filter(pk=definition.id).update(status=definition_status)
    ImageInstance.objects.filter(pk=older.id).update(status=selected_status)
    line = ImageLine("definition", job.id, definition.name, runner.id)
    missing = ImageLine("definition", uuid.uuid4(), "Missing", runner.id)
    with django_assert_num_queries(1):
        latest = ImageLineRepository.latest_many([line, missing])
    available = (job_status, definition_status, selected_status, has_selection) == (
        "active",
        "active",
        "ready",
        True,
    )
    assert latest[line] == (older if available else None)
    assert latest[missing] is None
    assert ImageLineRepository.latest(line) == latest[line]
    refs = ImageVersionService().version_refs([older, newer])
    for image in (older, newer):
        assert refs[image.id]["latest_id"] == (older.id if available else None)
        assert refs[image.id]["update_available"] is (
            available and image.id != older.id
        )
    labels = ImageRetentionService.retention_labels(line, 1)
    assert labels[newer.id] == "kept"
    if selected_status == "ready":
        assert labels[older.id] == ("latest" if available else "expires_when_unused")
    else:
        assert older.id not in labels


def test_presentation_reads_fresh_policies_status_and_pins(runner, user) -> None:
    """Reusing a service never retains stale version facts across requests."""
    line, versions = make_line(runner, user, ("ready",) * 3)
    service = ImageVersionService()
    first = service.list_captured_images(user, runner.organization_id)[0]
    assert first["latest"].id == versions[2].id
    runner.organization.image_versions_to_keep = 1
    runner.organization.save(update_fields=["image_versions_to_keep"])
    ImageInstance.objects.filter(pk=versions[2].id).update(status="failed")
    qemu_workspace(runner, user, versions[0])
    second = service.list_captured_images(user, runner.organization_id)[0]
    assert second["latest"].id == versions[1].id
    assert second["extras"][versions[0].id]["retention"] == "expires_when_unused"
    assert second["workspace_count"] == 1
    assert first["workspace_count"] == 0
    assert first["extras"][versions[2].id]["is_latest"] is True


def test_bulk_facts_group_by_identity_across_line_names(runner, user) -> None:
    """A rename between loaded records must not split a line's ready versions."""
    line, versions = make_line(runner, user, ("ready", "ready"))
    original = ImageLine("captured", line.id, line.name, runner.id)
    renamed = ImageLine("captured", line.id, "Renamed", runner.id)
    latest = ImageLineRepository.latest_many([original, renamed])
    labels = ImageRetentionService.retention_labels_many(
        {original: 1, renamed: 1}, latest
    )
    assert latest[original] == latest[renamed] == versions[1]
    assert (
        labels[original]
        == labels[renamed]
        == {versions[1].id: "latest", versions[0].id: "expires_when_unused"}
    )


@pytest.mark.parametrize("initial_versions", [(), ("ready",)])
@pytest.mark.parametrize("read", ["list", "detail"])
def test_publication_during_version_read_keeps_the_latest_reference(
    runner, user, monkeypatch, initial_versions: tuple[str, ...], read: str
) -> None:
    """Latest is read from persistence even if publication follows the row read."""
    line, _ = make_line(runner, user, initial_versions)
    load_versions = ImageLineRepository.version_rows_for_lines
    published = []

    def load_then_publish(lines: list[ImageLine]) -> list[ImageInstance]:
        rows = load_versions(lines)
        published.append(
            ImageInstance.objects.create(
                runner=runner,
                captured_image=line,
                name=line.name,
                generation=len(initial_versions) + 1,
                status="ready",
                runner_ref="published",
            )
        )
        return rows

    monkeypatch.setattr(
        ImageLineRepository, "version_rows_for_lines", load_then_publish
    )
    service = ImageVersionService()
    view = (
        service.list_captured_images(user, runner.organization_id)[0]
        if read == "list"
        else service.captured_image(line)
    )
    assert view["latest"].id == published[0].id
    assert len(view["versions"]) == len(initial_versions)


def test_captured_list_keeps_organization_owner_and_deletion_scope(
    runner, user
) -> None:
    """Batch loading never widens the visible line set or exposes hidden versions."""
    visible, _ = make_line(runner, user)
    deleted, _ = make_line(runner, user)
    CapturedImage.objects.filter(pk=deleted.id).update(status="deleted")
    other = get_user_model().objects.create_user(email="hidden-images@example.com")
    make_line(runner, other)
    foreign = Organization.objects.create(name="Foreign images", slug="foreign-images")
    make_line(runner, user)
    CapturedImage.objects.exclude(pk__in=[visible.id, deleted.id]).filter(
        created_by=user
    ).update(organization=foreign)
    rows = ImageVersionService().list_captured_images(user, runner.organization_id)
    assert [view["line"].id for view in rows] == [visible.id]


@pytest.mark.parametrize("failure", ["anonymous", "permission"])
def test_captured_list_still_requires_authorization(runner, user, failure: str) -> None:
    """The optimized service remains behind the same REST authorization gates."""
    make_line(runner, user)
    if failure == "anonymous":
        auth = {}
    else:
        permissions = [] if failure == "permission" else [APIKeyPermission.IMAGES_READ]
        auth = headers(user, runner.organization, permissions)
    Membership.objects.create(user=user, organization=runner.organization)
    response = Client().get("/api/v1/captured-images/", **auth)
    assert response.status_code == (401 if failure == "anonymous" else 403)
