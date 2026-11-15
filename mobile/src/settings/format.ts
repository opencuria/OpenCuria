/** Display helpers shared by the settings pages. */

const DAY_MS = 86_400_000;

/** "1 skill", "2 skills"; `plural` defaults to the singular plus "s". */
export function count(
  value: number,
  singular: string,
  plural?: string,
): string {
  return `${value} ${value === 1 ? singular : (plural ?? `${singular}s`)}`;
}

/** A timestamp as a local calendar date, e.g. "Nov 4, 2026"; "—" when missing. */
export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    }).format(date);
  } catch {
    return date.toDateString();
  }
}

/**
 * How long ago a timestamp was, e.g. "Just now", "5m ago", "3h ago",
 * "2d ago"; older than a month shows the date. `fallback` when missing.
 */
export function formatRelative(
  iso: string | null | undefined,
  fallback = "Never",
  now = Date.now(),
): string {
  if (!iso) return fallback;
  const time = new Date(iso).getTime();
  if (Number.isNaN(time)) return fallback;
  const seconds = Math.max(0, Math.floor((now - time) / 1000));
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  const days = Math.floor((now - time) / DAY_MS);
  return days < 30 ? `${days}d ago` : formatDate(iso);
}
