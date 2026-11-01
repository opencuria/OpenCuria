"""Stateless QEMU sizing policy shared by settings and capacity admission.

Lifecycle services resolve requests here; capacity admission applies the same
per-workspace bounds before checking aggregate reservations under runner locks.
This module performs no persistence, dispatch or lifecycle orchestration.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from common.exceptions import ConflictError

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


def resolve_qemu_resources(
    *,
    runner: Runner,
    qemu_vcpus: int | None,
    qemu_memory_mb: int | None,
    qemu_disk_size_gb: int | None,
    current: tuple[int, int, int] | None = None,
) -> tuple[int, int, int]:
    """Resolve effective QEMU resources and validate against runner limits."""
    current_vcpus = current[0] if current else runner.qemu_default_vcpus
    current_memory_mb = current[1] if current else runner.qemu_default_memory_mb
    current_disk_size_gb = current[2] if current else runner.qemu_default_disk_size_gb

    resolved_vcpus = qemu_vcpus if qemu_vcpus is not None else current_vcpus
    resolved_memory_mb = (
        qemu_memory_mb if qemu_memory_mb is not None else current_memory_mb
    )
    resolved_disk_size_gb = (
        qemu_disk_size_gb if qemu_disk_size_gb is not None else current_disk_size_gb
    )

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
