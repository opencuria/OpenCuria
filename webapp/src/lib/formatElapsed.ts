/**
 * Compact elapsed-time labels for finished harness turns (`5m 11s`).
 */

/** Format a millisecond duration as `11s`, `5m 11s`, or `1h 5m 11s`. */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

/** Elapsed ms between ISO timestamps, or null when either side is missing. */
export function elapsedMs(
  createdAt?: string,
  completedAt?: string | null,
): number | null {
  if (!createdAt || !completedAt) return null
  const start = Date.parse(createdAt)
  const end = Date.parse(completedAt)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null
  return Math.max(0, end - start)
}
