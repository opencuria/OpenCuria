"""Host and workspace resource sampling, independent of control transports."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

import psutil

from ..config import RunnerSettings

# Guest filesystem counters are available only while the guest answers probes.
_GUEST_DISK_METRIC_KEYS = (
    "guest_disk_used_bytes",
    "guest_disk_free_bytes",
    "guest_disk_total_bytes",
)

VmMetrics = dict[str, dict[str, float | int]]
VmMetricsSource = Callable[[], Awaitable[dict[str, dict[str, Any]]]]


class SystemMetrics(TypedDict):
    """One resource observation shared by all runner control transports."""

    timestamp: str
    cpu_usage_percent: float
    ram_used_bytes: int
    ram_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    vm_metrics: VmMetrics


class SystemMetricsCollector:
    """Own CPU baselines, storage probing and host/VM metric normalization.

    The caller supplies raw VM observations and decides when to sample and
    publish. Keeping the collector alive across reconnects preserves per-VM
    CPU baselines without tying sampling to a particular control transport.
    """

    def __init__(self, settings: RunnerSettings) -> None:
        self._settings = settings
        self._vm_cpu_samples: dict[str, tuple[int, float, int]] = {}
        self.disk_usage_path = self._resolve_disk_usage_path()

    def prime_cpu_sample(self) -> None:
        """Prime host CPU sampling when the caller starts its reporting loop."""
        psutil.cpu_percent(interval=None)

    async def collect(self, vm_metrics_source: VmMetricsSource) -> SystemMetrics:
        """Collect one complete observation without publishing partial results."""
        cpu = await asyncio.to_thread(psutil.cpu_percent, 1)
        ram = psutil.virtual_memory()
        disk = psutil.disk_usage(self.disk_usage_path)
        raw_vm_metrics = await vm_metrics_source()
        vm_metrics = self._build_vm_metrics_payload(raw_vm_metrics)
        return {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "cpu_usage_percent": cpu,
            "ram_used_bytes": ram.used,
            "ram_total_bytes": ram.total,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "vm_metrics": vm_metrics,
        }

    def _compute_vm_cpu_percent(
        self, workspace_id: str, cpu_time_ns: int, vcpu_count: int
    ) -> float:
        """Compute VM CPU utilisation percentage from cumulative CPU time."""
        now = time.monotonic()
        prev = self._vm_cpu_samples.get(workspace_id)
        self._vm_cpu_samples[workspace_id] = (cpu_time_ns, now, max(vcpu_count, 1))
        if prev is None:
            return 0.0

        prev_cpu_time_ns, prev_sample_ts, prev_vcpu_count = prev
        elapsed_s = now - prev_sample_ts
        if elapsed_s <= 0:
            return 0.0

        cpu_delta_ns = max(cpu_time_ns - prev_cpu_time_ns, 0)
        capacity_ns = elapsed_s * max(prev_vcpu_count, 1) * 1_000_000_000
        if capacity_ns <= 0:
            return 0.0

        cpu_percent = (cpu_delta_ns / capacity_ns) * 100
        return round(max(0.0, min(cpu_percent, 100.0)), 1)

    def _build_vm_metrics_payload(
        self, raw_vm_metrics: dict[str, dict[str, Any]]
    ) -> VmMetrics:
        """Normalize raw VM counters and drop CPU samples for absent VMs."""
        vm_metrics: VmMetrics = {}
        observed_workspace_ids = set(raw_vm_metrics.keys())

        for workspace_id, metric in raw_vm_metrics.items():
            cpu_time_ns = int(metric.get("cpu_time_ns", 0))
            vcpu_count = int(metric.get("vcpu_count", 1))
            vm_metrics[workspace_id] = {
                "cpu_usage_percent": self._compute_vm_cpu_percent(
                    workspace_id, cpu_time_ns, vcpu_count
                ),
                "ram_used_bytes": int(metric.get("ram_used_bytes", 0)),
                "ram_total_bytes": int(metric.get("ram_total_bytes", 0)),
                "disk_used_bytes": int(metric.get("disk_used_bytes", 0)),
                "disk_total_bytes": int(metric.get("disk_total_bytes", 0)),
            }
            for key in _GUEST_DISK_METRIC_KEYS:
                if key in metric:
                    vm_metrics[workspace_id][key] = int(metric[key])

        for workspace_id in list(self._vm_cpu_samples.keys()):
            if workspace_id not in observed_workspace_ids:
                self._vm_cpu_samples.pop(workspace_id, None)

        return vm_metrics

    def _storage_root_path(self) -> Path:
        """Return the shared storage root for runner-managed QEMU artifacts."""
        storage_dirs = [
            self._settings.qemu_image_cache_dir,
            self._settings.qemu_disk_dir,
            self._settings.qemu_snapshot_dir,
        ]
        expanded = [str(Path(path).expanduser()) for path in storage_dirs if path]
        if not expanded:
            return Path("/")
        return Path(os.path.commonpath(expanded))

    def _resolve_disk_usage_path(self) -> str:
        """Resolve an existing path on the filesystem used for runner storage."""
        probe = self._storage_root_path()
        while not probe.exists():
            if probe.parent == probe:
                return "/"
            probe = probe.parent
        return str(probe)
