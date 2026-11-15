"""Stateless QEMU sizing policy shared by settings and capacity admission.

Lifecycle services resolve requests here; capacity admission applies the same
per-workspace bounds before checking aggregate reservations under runner locks.
Resolution also applies the guest OS minimums of the workspace image.
This module performs no persistence, dispatch or lifecycle orchestration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.exceptions import ConflictError

from ..guest_os import WINDOWS, guest_minimums, normalize_guest_os

if TYPE_CHECKING:
    from ..models import Runner


def validate_runner_qemu_limits(runner: Runner) -> None:
    """Validate min/max/default and total limits for a runner's QEMU config."""
    if runner.qemu_min_vcpus > runner.qemu_max_vcpus:
        raise ConflictError("Runner vCPU minimum cannot exceed maximum")
    if not (
        runner.qemu_min_vcpus <= runner.qemu_default_vcpus <= runner.qemu_max_vcpus
    ):
        raise ConflictError("Runner default vCPU must be within min/max range")

    if runner.qemu_min_memory_mb > runner.qemu_max_memory_mb:
        raise ConflictError("Runner RAM minimum cannot exceed maximum")
    if not (
        runner.qemu_min_memory_mb
        <= runner.qemu_default_memory_mb
        <= runner.qemu_max_memory_mb
    ):
        raise ConflictError("Runner default RAM must be within min/max range")

    if runner.qemu_min_disk_size_gb > runner.qemu_max_disk_size_gb:
        raise ConflictError("Runner disk minimum cannot exceed maximum")
    if not (
        runner.qemu_min_disk_size_gb
        <= runner.qemu_default_disk_size_gb
        <= runner.qemu_max_disk_size_gb
    ):
        raise ConflictError("Runner default disk must be within min/max range")

    if (
        runner.qemu_max_active_vcpus is not None
        and runner.qemu_max_active_vcpus < runner.qemu_default_vcpus
    ):
        raise ConflictError(
            "Runner total active vCPU limit cannot be smaller than default vCPU"
        )
    if (
        runner.qemu_max_active_memory_mb is not None
        and runner.qemu_max_active_memory_mb < runner.qemu_default_memory_mb
    ):
        raise ConflictError(
            "Runner total active RAM limit cannot be smaller than default RAM"
        )
    if (
        runner.qemu_max_active_disk_size_gb is not None
        and runner.qemu_max_active_disk_size_gb < runner.qemu_default_disk_size_gb
    ):
        raise ConflictError(
            "Runner total active disk limit cannot be smaller than default disk"
        )


#: Resource dimensions as (runner/guest field suffix, label, unit).
_DIMENSIONS = (
    ("vcpus", "vCPU", ""),
    ("memory_mb", "RAM", " MiB"),
    ("disk_size_gb", "disk", " GiB"),
)


def _guest_label(guest_os: str | None) -> str:
    return "Windows" if normalize_guest_os(guest_os) == WINDOWS else "Linux"


def ensure_runner_fits_guest(runner: Runner, guest_os: str | None) -> None:
    """Reject runners whose per-workspace maximums cannot host the guest OS."""
    minimums = guest_minimums(guest_os)
    short = [
        f"{label} {getattr(minimums, name)}{unit}"
        for name, label, unit in _DIMENSIONS
        if getattr(runner, f"qemu_max_{name}") < getattr(minimums, name)
    ]
    if short:
        raise ConflictError(
            f"This runner's limits are too small for {_guest_label(guest_os)} "
            f"workspaces, which need at least {', '.join(short)}",
            code="runner_limits_below_guest_minimum",
        )


def resolve_qemu_resources(
    *,
    runner: Runner,
    qemu_vcpus: int | None,
    qemu_memory_mb: int | None,
    qemu_disk_size_gb: int | None,
    current: tuple[int | None, int | None, int | None] | None = None,
    guest_os: str | None = None,
) -> tuple[int, int, int]:
    """Resolve effective QEMU resources and validate against runner limits.

    Explicit values win; others keep ``current`` (else the runner default),
    raised to the guest OS minimum. Explicit values below that minimum are
    rejected, as are runners whose maximums cannot host the guest at all.
    """
    ensure_runner_fits_guest(runner, guest_os)
    minimums = guest_minimums(guest_os)
    requested = (qemu_vcpus, qemu_memory_mb, qemu_disk_size_gb)
    resolved = []
    for index, (name, label, unit) in enumerate(_DIMENSIONS):
        minimum = getattr(minimums, name)
        value = requested[index]
        if value is None:
            fallback = current[index] if current else None
            if fallback is None:
                fallback = getattr(runner, f"qemu_default_{name}")
            value = max(fallback, minimum)
        elif value < minimum and minimum > getattr(runner, f"qemu_min_{name}"):
            raise ConflictError(
                f"{_guest_label(guest_os)} workspaces need at least "
                f"{minimum}{unit} {label}",
                code="resources_below_guest_minimum",
            )
        resolved.append(value)
    resolved_vcpus, resolved_memory_mb, resolved_disk_size_gb = resolved

    if not (runner.qemu_min_vcpus <= resolved_vcpus <= runner.qemu_max_vcpus):
        raise ConflictError(
            f"vCPU value must be between {runner.qemu_min_vcpus} "
            f"and {runner.qemu_max_vcpus}"
        )
    if not (
        runner.qemu_min_memory_mb <= resolved_memory_mb <= runner.qemu_max_memory_mb
    ):
        raise ConflictError(
            f"RAM value must be between {runner.qemu_min_memory_mb} "
            f"and {runner.qemu_max_memory_mb} MiB"
        )
    if not (
        runner.qemu_min_disk_size_gb
        <= resolved_disk_size_gb
        <= runner.qemu_max_disk_size_gb
    ):
        raise ConflictError(
            f"Disk value must be between {runner.qemu_min_disk_size_gb} "
            f"and {runner.qemu_max_disk_size_gb} GiB"
        )
    return resolved_vcpus, resolved_memory_mb, resolved_disk_size_gb
