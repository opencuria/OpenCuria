import type { WorkspaceDiskUsage } from '@/types'

/** Share in use from which a resource counts as almost exhausted. */
export const USAGE_WARNING_PERCENT = 90
/** Share in use from which writes or allocations are about to fail. */
export const USAGE_CRITICAL_PERCENT = 97
/** Running workspaces report every minute; older values are labelled with their age. */
const STALE_AFTER_MS = 5 * 60 * 1000

export type UsageLevel = 'ok' | 'warning' | 'critical'

export interface DiskUsageView {
  /** Usable space in use, floored to whole percent (like `df`'s Use%). */
  percent: number
  level: UsageLevel
  usedBytes: number
  freeBytes: number
  /** Usable capacity (used + free); excludes filesystem reserve and overhead. */
  capacityBytes: number
}

/** Tailwind classes per level: meter fill and accompanying text. */
export const USAGE_LEVEL_CLASSES: Record<UsageLevel, { fill: string; text: string }> = {
  ok: { fill: 'bg-primary', text: 'text-muted-foreground' },
  warning: { fill: 'bg-amber-500', text: 'text-amber-600 dark:text-amber-400' },
  critical: { fill: 'bg-destructive', text: 'text-destructive' },
}

export function usageLevel(percent: number): UsageLevel {
  if (percent >= USAGE_CRITICAL_PERCENT) return 'critical'
  if (percent >= USAGE_WARNING_PERCENT) return 'warning'
  return 'ok'
}

/** Share of usable space in use (0–100); reserved blocks count as unavailable. */
export function diskUsagePercent(usage: WorkspaceDiskUsage): number {
  const capacity = usage.used_bytes + usage.free_bytes
  if (capacity <= 0) return 0
  return Math.min(100, Math.max(0, (usage.used_bytes / capacity) * 100))
}

export function diskLevel(usage: WorkspaceDiskUsage): UsageLevel {
  return usageLevel(diskUsagePercent(usage))
}

export function describeDiskUsage(usage: WorkspaceDiskUsage): DiskUsageView {
  return {
    percent: Math.floor(diskUsagePercent(usage)),
    level: diskLevel(usage),
    usedBytes: usage.used_bytes,
    freeBytes: usage.free_bytes,
    capacityBytes: usage.used_bytes + usage.free_bytes,
  }
}

/** Whether the observation is too old to pass for the current state. */
export function isDiskUsageStale(usage: WorkspaceDiskUsage, now = Date.now()): boolean {
  return now - new Date(usage.observed_at).getTime() > STALE_AFTER_MS
}
