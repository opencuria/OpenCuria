/** Last observed filesystem usage inside a workspace (`/workspace`). */
export interface WorkspaceDiskUsageInput {
  used_bytes: number;
  free_bytes: number;
  total_bytes: number;
  observed_at: string;
}

/** Share in use from which a resource counts as almost exhausted. */
export const USAGE_WARNING_PERCENT = 90;
/** Share in use from which writes or allocations are about to fail. */
export const USAGE_CRITICAL_PERCENT = 97;
/** Running workspaces report every minute; older values are labelled with their age. */
const STALE_AFTER_MS = 5 * 60 * 1000;

export type UsageLevel = "ok" | "warning" | "critical";

export interface DiskUsageView {
  /** Usable space in use, floored to whole percent (like `df`'s Use%). */
  percent: number;
  level: UsageLevel;
  usedBytes: number;
  freeBytes: number;
  /** Usable capacity (used + free); excludes filesystem reserve and overhead. */
  capacityBytes: number;
}

export function usageLevel(percent: number): UsageLevel {
  if (percent >= USAGE_CRITICAL_PERCENT) return "critical";
  if (percent >= USAGE_WARNING_PERCENT) return "warning";
  return "ok";
}

/** Share of usable space in use (0–100); reserved blocks count as unavailable. */
export function diskUsagePercent(usage: WorkspaceDiskUsageInput): number {
  const capacity = usage.used_bytes + usage.free_bytes;
  if (capacity <= 0) return 0;
  return Math.min(100, Math.max(0, (usage.used_bytes / capacity) * 100));
}

export function diskLevel(usage: WorkspaceDiskUsageInput): UsageLevel {
  return usageLevel(diskUsagePercent(usage));
}

export function describeDiskUsage(
  usage: WorkspaceDiskUsageInput,
): DiskUsageView {
  return {
    percent: Math.floor(diskUsagePercent(usage)),
    level: diskLevel(usage),
    usedBytes: usage.used_bytes,
    freeBytes: usage.free_bytes,
    capacityBytes: usage.used_bytes + usage.free_bytes,
  };
}

/** Whether the observation is too old to pass for the current state. */
export function isDiskUsageStale(
  usage: WorkspaceDiskUsageInput,
  now = Date.now(),
): boolean {
  return now - new Date(usage.observed_at).getTime() > STALE_AFTER_MS;
}

/** Binary-unit size such as "12.4 GiB", as used for storage throughout the UI. */
export function formatStorageBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 4);
  return `${(bytes / 1024 ** unit).toFixed(1)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}
