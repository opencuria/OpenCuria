"""Characterize QEMU sizing independently of lifecycle and task dispatch."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from apps.runners.services import RunnerRepositories, RunnerService
from apps.runners.services.qemu_resources import (
    resolve_qemu_resources,
    validate_runner_qemu_limits,
)
from apps.runners.services.workspace_capacity import WorkspaceCapacityService
from common.exceptions import ConflictError

Dimension = tuple[str, str, str, int, int, int]
Resources = tuple[int | None, int | None, int | None]

DIMENSIONS = [
    ("vcpus", "vCPU", "", 1, 2, 8),
    ("memory_mb", "RAM", " MiB", 1024, 4096, 16384),
    ("disk_size_gb", "disk", " GiB", 10, 40, 200),
]


@pytest.fixture
def limits() -> SimpleNamespace:
    """Distinct ranges make each dimension and fallback observable."""
    fields = {"id": "runner"}
    for name, _, _, minimum, default, maximum in DIMENSIONS:
        fields.update(
            {
                f"qemu_min_{name}": minimum,
                f"qemu_default_{name}": default,
                f"qemu_max_{name}": maximum,
                f"qemu_max_active_{name}": None,
            }
        )
    return SimpleNamespace(**fields)


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("failure", ["range", "default_low", "default_high", "total"])
def test_runner_config_errors(
    limits: SimpleNamespace, dimension: Dimension, failure: str
) -> None:
    name, label, _, minimum, default, maximum = dimension
    field, value, message = {
        "range": ("min", maximum + 1, f"Runner {label} minimum cannot exceed maximum"),
        "default_low": (
            "default",
            minimum - 1,
            f"Runner default {label} must be within min/max range",
        ),
        "default_high": (
            "default",
            maximum + 1,
            f"Runner default {label} must be within min/max range",
        ),
        "total": (
            "max_active",
            default - 1,
            f"Runner total active {label} limit cannot be smaller than default {label}",
        ),
    }[failure]
    setattr(limits, f"qemu_{field}_{name}", value)
    with pytest.raises(ConflictError) as caught:
        validate_runner_qemu_limits(limits)
    assert caught.value.message == message
    assert caught.value.code == "conflict"


def test_runner_config_validation_order(limits: SimpleNamespace) -> None:
    limits.qemu_default_disk_size_gb = 0
    limits.qemu_max_active_vcpus = 0
    with pytest.raises(ConflictError) as caught:
        validate_runner_qemu_limits(limits)
    assert caught.value.message == "Runner default disk must be within min/max range"
    limits.qemu_default_vcpus = 0
    with pytest.raises(ConflictError) as caught:
        validate_runner_qemu_limits(limits)
    assert caught.value.message == "Runner default vCPU must be within min/max range"


@pytest.mark.parametrize("at_boundary", [False, True])
def test_runner_limits_accept_unlimited_totals_and_inclusive_bounds(
    limits: SimpleNamespace, at_boundary: bool
) -> None:
    if at_boundary:
        for name, _, _, _, default, _ in DIMENSIONS:
            for field in ("min", "max", "max_active"):
                setattr(limits, f"qemu_{field}_{name}", default)
    validate_runner_qemu_limits(limits)


@pytest.mark.parametrize(
    ("current", "requested", "expected"),
    [
        (None, (None, None, None), (2, 4096, 40)),
        ((4, 8192, 80), (None, None, None), (4, 8192, 80)),
        ((4, 8192, 80), (2, None, 60), (2, 8192, 60)),
        (None, (1, 1024, 10), (1, 1024, 10)),
        (None, (8, 16384, 200), (8, 16384, 200)),
    ],
)
def test_resolution_defaults_current_and_explicit_values(
    limits: SimpleNamespace,
    current: tuple[int, int, int] | None,
    requested: Resources,
    expected: tuple[int, int, int],
) -> None:
    assert (
        resolve_qemu_resources(
            runner=limits,
            qemu_vcpus=requested[0],
            qemu_memory_mb=requested[1],
            qemu_disk_size_gb=requested[2],
            current=current,
        )
        == expected
    )


@pytest.mark.parametrize("dimension", DIMENSIONS)
@pytest.mark.parametrize("above", [False, True])
def test_preflight_and_capacity_preserve_range_errors(
    limits: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    dimension: Dimension,
    above: bool,
) -> None:
    """Disk-only admission still checks all per-VM limits before aggregate use."""
    name, label, unit, minimum, _, maximum = dimension
    requested = {f"qemu_{item[0]}": None for item in DIMENSIONS}
    requested[f"qemu_{name}"] = maximum + 1 if above else 0
    monkeypatch.setattr(
        "apps.runners.services.workspace_capacity.RunnerRepository.get_by_id",
        Mock(return_value=limits),
    )
    calls = [
        lambda: resolve_qemu_resources(runner=limits, **requested),
        *[
            lambda reserve=reserve: WorkspaceCapacityService.ensure(
                limits.id,
                **{key.removeprefix("qemu_"): val for key, val in requested.items()},
                reserve_compute=reserve,
            )
            for reserve in (True, False)
        ],
    ]
    label = "Disk" if name == "disk_size_gb" else label
    for call in calls:
        with pytest.raises(ConflictError) as caught:
            call()
        assert caught.value.message == (
            f"{label} value must be between {minimum} and {maximum}{unit}"
        )
        assert caught.value.code == "conflict"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_capacity_defaults_count_towards_aggregate_limits(
    limits: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, dimension: Dimension
) -> None:
    name, _, _, _, default, _ = dimension
    setattr(limits, f"qemu_max_active_{name}", default)
    monkeypatch.setattr(
        "apps.runners.services.workspace_capacity.RunnerRepository.get_by_id",
        Mock(return_value=limits),
    )
    existing = SimpleNamespace(
        id="existing",
        capacity_payload={},
        capacity_status=None,
        qemu_vcpus=None,
        qemu_memory_mb=None,
        qemu_disk_size_gb=None,
    )
    monkeypatch.setattr(
        "apps.runners.services.workspace_capacity."
        "WorkspaceRepository.list_reserved_qemu_by_runner",
        Mock(return_value=[existing]),
    )
    with pytest.raises(ConflictError, match="limit exceeded"):
        WorkspaceCapacityService.ensure(
            limits.id, vcpus=None, memory_mb=None, disk_size_gb=None
        )
    WorkspaceCapacityService.ensure(
        limits.id,
        vcpus=None,
        memory_mb=None,
        disk_size_gb=None,
        exclude_workspace_id=existing.id,
    )


@pytest.mark.parametrize("valid", [False, True])
def test_runner_settings_validate_before_persistence(
    limits: SimpleNamespace, valid: bool
) -> None:
    """Settings updates validate the resulting config, then persist once."""
    runners = Mock()
    runners.get_by_id.return_value = limits
    runners.update_qemu_settings.return_value = limits
    service = RunnerService(repositories=replace(RunnerRepositories(), runners=runners))
    changed = {"qemu_default_vcpus": 4 if valid else 9}
    if valid:
        assert service.update_runner_qemu_settings(limits.id, **changed) is limits
        runners.update_qemu_settings.assert_called_once_with(limits, **changed)
    else:
        with pytest.raises(ConflictError) as caught:
            service.update_runner_qemu_settings(limits.id, **changed)
        assert (
            caught.value.message == "Runner default vCPU must be within min/max range"
        )
        runners.update_qemu_settings.assert_not_called()


def test_runner_settings_noop_does_not_validate_or_write(
    limits: SimpleNamespace,
) -> None:
    """An empty update returns even an existing invalid configuration as before."""
    limits.qemu_default_vcpus = 0
    runners = Mock()
    runners.get_by_id.return_value = limits
    service = RunnerService(repositories=replace(RunnerRepositories(), runners=runners))
    assert service.update_runner_qemu_settings(limits.id) is limits
    runners.update_qemu_settings.assert_not_called()
