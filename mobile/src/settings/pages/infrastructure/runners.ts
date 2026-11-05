import type {
  Runner,
  RunnerSystemMetrics,
  RunnerUpdateInput,
} from "@opencuria/client";
import type { NativeFormTone } from "../../../ui/NativeForm.types";
import { formatRelative } from "../../format";

/** "Online · Connected 5m ago", "Offline · Last seen 2h ago" or "Never connected". */
export function runnerConnection(runner: Runner): string {
  if (runner.status === "online")
    return `Online · Connected ${formatRelative(runner.connected_at, "just now").toLowerCase()}`;
  if (runner.disconnected_at)
    return `Offline · Last seen ${formatRelative(runner.disconnected_at).toLowerCase()}`;
  return "Never connected";
}

/** A used share as a whole percentage. */
export function percent(used: number, total: number): number {
  return total > 0 ? Math.round((used / total) * 100) : 0;
}

/** Warning from 70 %, danger from 90 %, as in the webapp. */
export function usageTone(value: number): NativeFormTone {
  return value >= 90 ? "danger" : value >= 70 ? "warning" : "default";
}

/** "CPU 12% · RAM 40% · Disk 55%". */
export function metricsSummary(metrics: RunnerSystemMetrics): string {
  return [
    `CPU ${Math.round(metrics.cpu_usage_percent)}%`,
    `RAM ${percent(metrics.ram_used_bytes, metrics.ram_total_bytes)}%`,
    `Disk ${percent(metrics.disk_used_bytes, metrics.disk_total_bytes)}%`,
  ].join(" · ");
}

export type QemuResource = "vcpus" | "memory" | "disk";

export interface QemuRange {
  min: number;
  default: number;
  max: number;
}

export interface QemuLimitsForm {
  ranges: Record<QemuResource, QemuRange>;
  /** Totals across active workspaces; `null` is unlimited. */
  caps: Record<QemuResource, number | null>;
}

/** Steps and bounds of each resource, as in the webapp's limit editor. */
export const QEMU_RESOURCES: Record<
  QemuResource,
  { label: string; unit: string; step: number; floor: number; ceiling: number }
> = {
  vcpus: { label: "vCPU", unit: "vCPU", step: 1, floor: 1, ceiling: 128 },
  memory: {
    label: "Memory",
    unit: "MiB",
    step: 256,
    floor: 256,
    ceiling: 262144,
  },
  disk: { label: "Storage", unit: "GiB", step: 5, floor: 5, ceiling: 4096 },
};

/** "4 vCPU", "8 GiB" (memory in MiB shown in GiB when whole), "50 GiB". */
export function formatResource(resource: QemuResource, value: number): string {
  if (resource === "vcpus") return `${value} vCPU`;
  if (resource === "memory")
    return value >= 1024 && value % 1024 === 0
      ? `${value / 1024} GiB`
      : value >= 1024
        ? `${(value / 1024).toFixed(1)} GiB`
        : `${value} MiB`;
  return `${value} GiB`;
}

/** The runner's stored limits as an editable form. */
export function qemuLimitsForm(runner: Runner): QemuLimitsForm {
  return {
    ranges: {
      vcpus: {
        min: runner.qemu_min_vcpus,
        default: runner.qemu_default_vcpus,
        max: runner.qemu_max_vcpus,
      },
      memory: {
        min: runner.qemu_min_memory_mb,
        default: runner.qemu_default_memory_mb,
        max: runner.qemu_max_memory_mb,
      },
      disk: {
        min: runner.qemu_min_disk_size_gb,
        default: runner.qemu_default_disk_size_gb,
        max: runner.qemu_max_disk_size_gb,
      },
    },
    caps: {
      vcpus: runner.qemu_max_active_vcpus ?? null,
      memory: runner.qemu_max_active_memory_mb ?? null,
      disk: runner.qemu_max_active_disk_size_gb ?? null,
    },
  };
}

/**
 * Sets one bound and keeps the range consistent: raising the minimum pushes
 * the default and maximum up, lowering the maximum pulls them down, and the
 * default stays between both.
 */
export function setQemuBound(
  range: QemuRange,
  bound: keyof QemuRange,
  value: number,
): QemuRange {
  if (bound === "min")
    return {
      min: value,
      default: Math.max(range.default, value),
      max: Math.max(range.max, value),
    };
  if (bound === "max")
    return {
      max: value,
      default: Math.min(range.default, value),
      min: Math.min(range.min, value),
    };
  return { ...range, default: Math.min(range.max, Math.max(range.min, value)) };
}

/** A cap's starting value when "Unlimited" is turned off. */
export function initialCap(range: QemuRange, resource: QemuResource): number {
  return Math.max(range.default, QEMU_RESOURCES[resource].floor);
}

/** The highest a cap stepper offers: generous, but bounded. */
export function capCeiling(
  form: QemuLimitsForm,
  resource: QemuResource,
  host: number | null,
): number {
  return Math.max(
    form.ranges[resource].max * 4,
    (host ?? 0) * 2,
    form.caps[resource] ?? 0,
    QEMU_RESOURCES[resource].floor,
  );
}

const positive = (value: number) => Number.isSafeInteger(value) && value > 0;

/** Every bound positive, min ≤ default ≤ max, and caps positive when set. */
export function qemuLimitsValid(form: QemuLimitsForm): boolean {
  return (Object.keys(form.ranges) as QemuResource[]).every((resource) => {
    const range = form.ranges[resource];
    const cap = form.caps[resource];
    return (
      positive(range.min) &&
      positive(range.default) &&
      positive(range.max) &&
      range.min <= range.default &&
      range.default <= range.max &&
      (cap === null || positive(cap))
    );
  });
}

/** All twelve fields; caps are `null` when unlimited. */
export function qemuLimitsPayload(form: QemuLimitsForm): RunnerUpdateInput {
  const { vcpus, memory, disk } = form.ranges;
  return {
    qemu_min_vcpus: vcpus.min,
    qemu_max_vcpus: vcpus.max,
    qemu_default_vcpus: vcpus.default,
    qemu_min_memory_mb: memory.min,
    qemu_max_memory_mb: memory.max,
    qemu_default_memory_mb: memory.default,
    qemu_min_disk_size_gb: disk.min,
    qemu_max_disk_size_gb: disk.max,
    qemu_default_disk_size_gb: disk.default,
    qemu_max_active_vcpus: form.caps.vcpus,
    qemu_max_active_memory_mb: form.caps.memory,
    qemu_max_active_disk_size_gb: form.caps.disk,
  };
}

/** Host capacity in the cap's unit, from the latest metrics. */
export function hostCapacity(
  resource: QemuResource,
  runner: Runner,
  metrics: RunnerSystemMetrics | null | undefined,
): number | null {
  if (resource === "vcpus") return runner.qemu_max_vcpus ?? null;
  if (!metrics) return null;
  return resource === "memory"
    ? Math.floor(metrics.ram_total_bytes / 1024 ** 2)
    : Math.floor(metrics.disk_total_bytes / 1024 ** 3);
}

/** "Needs attention", "In progress" or "History" for a runner activity. */
export const ACTIVITY_GROUPS = [
  { state: "attention", title: "Needs attention" },
  { state: "active", title: "In progress" },
  { state: "history", title: "History" },
] as const;

/** Button titles of operation dispositions, as in the webapp. */
export const DISPOSITION_TITLES = {
  reconcile: "Apply confirmed result",
  retry: "Try again",
  acknowledge_interrupted: "Close interrupted operation",
} as const;
