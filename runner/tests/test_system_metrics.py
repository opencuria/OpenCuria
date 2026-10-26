"""Host/VM sampling behavior without a QEMU runtime or control transport."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

import pytest
from src.config import RunnerSettings
from src.services.system_metrics import SystemMetricsCollector


@pytest.fixture
def collector() -> SystemMetricsCollector:
    """Provide a fresh CPU baseline without initializing a control transport."""
    return SystemMetricsCollector(RunnerSettings())


@pytest.mark.parametrize(
    ("samples", "times", "expected"),
    [
        ([(0, 2), (1_000_000_000, 2)], [10, 11], [0.0, 50.0]),
        ([(0, 4), (1_000_000_000, 1)], [10, 11], [0.0, 25.0]),
        ([(0, 0), (1_000_000_000, 0)], [10, 11], [0.0, 100.0]),
        ([(0, 2), (10_000_000_000, 2)], [10, 11], [0.0, 100.0]),
        ([(1_000_000_000, 2), (0, 2)], [10, 11], [0.0, 0.0]),
        ([(0, 2), (1_000_000_000, 2)], [10, 10], [0.0, 0.0]),
        ([(0, 3), (1_000_000_000, 3)], [10, 11], [0.0, 33.3]),
    ],
)
def test_vm_cpu_uses_elapsed_capacity_and_bounds_samples(
    collector: SystemMetricsCollector,
    monkeypatch: pytest.MonkeyPatch,
    samples: list[tuple[int, int]],
    times: list[int],
    expected: list[float],
) -> None:
    """Preserve first-sample, vCPU-change, reset and rounding behavior."""
    monkeypatch.setattr(
        "src.services.system_metrics.time.monotonic", Mock(side_effect=times)
    )
    actual = [
        collector._build_vm_metrics_payload(
            {"ws": {"cpu_time_ns": cpu, "vcpu_count": vcpus}}
        )["ws"]["cpu_usage_percent"]
        for cpu, vcpus in samples
    ]
    assert actual == expected


def test_disappearing_vm_discards_its_previous_cpu_sample(
    collector: SystemMetricsCollector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace returning after absence starts with a fresh CPU baseline."""
    monkeypatch.setattr(
        "src.services.system_metrics.time.monotonic", Mock(side_effect=[1, 2])
    )
    collector._build_vm_metrics_payload({"ws": {"cpu_time_ns": 1_000_000_000}})
    assert collector._build_vm_metrics_payload({}) == {}
    returned = collector._build_vm_metrics_payload(
        {"ws": {"cpu_time_ns": 100_000_000_000}}
    )
    assert returned == {
        "ws": {
            "cpu_usage_percent": 0.0,
            "ram_used_bytes": 0,
            "ram_total_bytes": 0,
            "disk_used_bytes": 0,
            "disk_total_bytes": 0,
        }
    }


class SystemMetricsVmPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.collector = SystemMetricsCollector(RunnerSettings())

    def test_forwards_guest_disk_counters_when_reported(self) -> None:
        payload = self.collector._build_vm_metrics_payload(
            {
                "ws-1": {
                    "cpu_time_ns": 0,
                    "vcpu_count": 2,
                    "disk_used_bytes": 3,
                    "disk_total_bytes": 4,
                    "guest_disk_used_bytes": 10,
                    "guest_disk_free_bytes": 20,
                    "guest_disk_total_bytes": 31,
                }
            }
        )

        assert payload["ws-1"]["guest_disk_used_bytes"] == 10
        assert payload["ws-1"]["guest_disk_free_bytes"] == 20
        assert payload["ws-1"]["guest_disk_total_bytes"] == 31
        assert payload["ws-1"]["disk_used_bytes"] == 3

    def test_omits_guest_disk_counters_when_guest_did_not_answer(self) -> None:
        payload = self.collector._build_vm_metrics_payload(
            {"ws-1": {"disk_used_bytes": 3, "disk_total_bytes": 0}}
        )

        assert "guest_disk_used_bytes" not in payload["ws-1"]
        assert payload["ws-1"]["disk_used_bytes"] == 3


class SystemMetricsPathTests(unittest.TestCase):
    def test_storage_root_defaults_to_var_lib_opencuria(self) -> None:
        settings = RunnerSettings(
            qemu_image_cache_dir="/var/lib/opencuria/images",
            qemu_disk_dir="/var/lib/opencuria/disks",
            qemu_snapshot_dir="/var/lib/opencuria/snapshots",
        )
        collector = SystemMetricsCollector(settings)
        self.assertEqual(
            collector._storage_root_path().as_posix(), "/var/lib/opencuria"
        )

    def test_storage_root_respects_custom_common_base(self) -> None:
        settings = RunnerSettings(
            qemu_image_cache_dir="/mnt/kern-store/images",
            qemu_disk_dir="/mnt/kern-store/disks",
            qemu_snapshot_dir="/mnt/kern-store/snapshots",
        )
        collector = SystemMetricsCollector(settings)
        self.assertEqual(collector._storage_root_path().as_posix(), "/mnt/kern-store")

    def test_resolve_disk_usage_path_falls_back_to_existing_parent(self) -> None:
        settings = RunnerSettings(
            qemu_image_cache_dir="/tmp/runner-metrics-test/images",
            qemu_disk_dir="/tmp/runner-metrics-test/disks",
            qemu_snapshot_dir="/tmp/runner-metrics-test/snapshots",
        )
        collector = SystemMetricsCollector(settings)
        self.assertEqual(collector._resolve_disk_usage_path(), "/tmp")
