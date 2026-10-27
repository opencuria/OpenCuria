"""Characterize retention decisions, recovery isolation and atomic retirement."""

import uuid
from unittest.mock import Mock

import pytest
from django.db import connection

from apps.runners.deletion_repository import DeletionRepository
from apps.runners.image_lines import ImageLineRepository
from apps.runners.models import CapturedImage, ImageDeletionRequest, ImageInstance
from apps.runners.retention_repository import RetentionRepository
from apps.runners.services.image_retention import ImageRetentionService
from apps.runners.tests.test_image_versions import make_line, qemu_workspace
from common.exceptions import ConflictError, NotFoundError

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("keep", [0, 1, 4])
def test_retention_configuration_keeps_at_least_one_version(runner, keep: int) -> None:
    """Missing organizations and zero configuration retain the existing floor."""
    organization = runner.organization
    organization.image_versions_to_keep = keep
    organization.save(update_fields=["image_versions_to_keep"])
    assert ImageRetentionService.keep_for(organization.id) == max(keep, 1)
    assert ImageRetentionService.keep_for(uuid.uuid4()) == 1


def test_apply_skips_pending_pins_and_reserved_versions(runner, user) -> None:
    """Both future workspace bases and existing deletion owners block retirement."""
    captured, (reserved, pinned, expired, latest) = make_line(
        runner, user, ("ready",) * 4
    )
    organization = runner.organization
    organization.image_versions_to_keep = 1
    organization.save(update_fields=["image_versions_to_keep"])
    workspace = qemu_workspace(runner, user, latest)
    workspace.pending_base_image_instance = pinned
    workspace.save(update_fields=["pending_base_image_instance"])
    ImageDeletionRequest.objects.create(
        organization=organization,
        target_type="image",
        target_id=reserved.id,
        previous={f"image:{reserved.id}": "ready"},
    )
    assert ImageRetentionService.apply(
        ImageLineRepository.line_of(expired), organization.id
    ) == [str(expired.id)]
    assert ImageInstance.objects.get(pk=reserved.id).status == "ready"
    assert ImageInstance.objects.get(pk=pinned.id).status == "ready"
    assert CapturedImage.objects.get(pk=captured.id).status == "active"


@pytest.mark.parametrize("error", [ConflictError("busy"), NotFoundError("Image")])
def test_apply_continues_after_expected_deletion_rejection(
    runner, user, monkeypatch, error: Exception
) -> None:
    """A rejected candidate must not stop later versions of the same line."""
    _, images = make_line(runner, user, ("ready",) * 4)
    request = Mock(side_effect=[error, {}])
    monkeypatch.setattr(DeletionRepository, "request", request)
    requested = ImageRetentionService.apply(
        ImageLineRepository.line_of(images[0]), runner.organization_id
    )
    assert request.call_count == 2
    assert requested == [str(request.call_args.args[3])]
    assert request.call_args.args[:3] == (runner.organization_id, None, "image")
    assert request.call_args.kwargs == {"mode": "deferred"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["apply", "heal"])
def test_unexpected_failure_rolls_back_the_whole_line(
    runner, user, monkeypatch, operation: str
) -> None:
    """Selection and every retirement share the runner lock and transaction."""
    captured, images = make_line(runner, user, ("ready",) * 4)
    original = DeletionRepository.request
    calls = []

    def request(*args, **kwargs) -> dict:
        assert connection.in_atomic_block
        calls.append(args[3])
        if len(calls) == 2:
            raise RuntimeError("unexpected failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(DeletionRepository, "request", request)
    with pytest.raises(RuntimeError, match="unexpected failure"):
        if operation == "apply":
            ImageRetentionService.apply(
                ImageLineRepository.line_of(images[0]), runner.organization_id
            )
        else:
            ImageRetentionService.heal(captured)
    assert len(calls) == 2
    assert not ImageDeletionRequest.objects.exists()
    assert set(ImageInstance.objects.values_list("status", flat=True)) == {"ready"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["apply", "heal"])
def test_selection_and_retirement_follow_the_runner_lock(
    runner, user, monkeypatch, operation: str
) -> None:
    """Moving orchestration must not move candidate reads outside serialization."""
    from apps.runners import retention_repository

    captured, images = make_line(runner, user, ("ready",) * 4)
    events = []
    original_lock = retention_repository.lock_runner
    repository, method = (
        (ImageLineRepository, "ready_ids")
        if operation == "apply"
        else (RetentionRepository, "recoverable_versions")
    )
    original_read = getattr(repository, method)

    def lock(runner_id: uuid.UUID) -> object:
        assert runner_id == runner.id and connection.in_atomic_block
        events.append("lock")
        return original_lock(runner_id)

    def read(*args) -> list:
        assert connection.in_atomic_block
        events.append("read")
        return original_read(*args)

    def request(*args, **kwargs) -> dict:
        assert connection.in_atomic_block
        events.append("request")
        return {}

    monkeypatch.setattr(retention_repository, "lock_runner", lock)
    monkeypatch.setattr(repository, method, read)
    monkeypatch.setattr(DeletionRepository, "request", request)
    if operation == "apply":
        ImageRetentionService.apply(
            ImageLineRepository.line_of(images[0]), runner.organization_id
        )
        count = 2
    else:
        ImageRetentionService.heal(captured)
        count = 4
    assert events == ["lock", "read", *(["request"] * count)]
    assert not connection.in_atomic_block


def test_heal_skips_owned_lines_and_ineligible_or_reserved_versions(
    runner, user
) -> None:
    """Only orphaned versions are submitted while a captured line retires."""
    captured, images = make_line(
        runner, user, ("ready", "failed", "deleted", "deleting", "capturing")
    )
    captured.status = "pending_deletion"
    captured.save(update_fields=["status"])
    owner = ImageDeletionRequest.objects.create(
        organization=runner.organization,
        target_type="captured_image",
        target_id=captured.id,
    )
    assert ImageRetentionService.heal(captured) == []
    owner.phase = "completed"
    owner.save(update_fields=["phase"])
    ImageDeletionRequest.objects.create(
        organization=runner.organization,
        target_type="image",
        target_id=images[0].id,
        previous={f"image:{images[0].id}": "ready"},
    )
    assert ImageRetentionService.heal(captured) == [str(images[1].id)]


def test_tick_isolates_line_failures_and_still_finalizes(
    runner, user, monkeypatch
) -> None:
    """Apply/heal errors do not prevent other lines or finalization progressing."""
    make_line(runner, user)
    make_line(runner, user)
    pending, _ = make_line(runner, user)
    empty, _ = make_line(runner, user, ("deleted",))
    CapturedImage.objects.filter(pk__in=[pending.id, empty.id]).update(
        status="pending_deletion"
    )
    apply = Mock(side_effect=[RuntimeError("apply failed"), ["expired"]])
    heal = Mock(side_effect=[RuntimeError("heal failed"), ["orphaned"]])
    monkeypatch.setattr(ImageRetentionService, "apply", apply)
    monkeypatch.setattr(ImageRetentionService, "heal", heal)
    assert ImageRetentionService.tick() == ["expired", "orphaned"]
    assert apply.call_count == heal.call_count == 2
    empty.refresh_from_db()
    assert empty.status == "deleted"
