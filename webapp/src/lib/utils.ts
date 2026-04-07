import { type ClassValue, clsx } from 'clsx'
import { twMerge } from 'tailwind-merge'

/**
 * Merge Tailwind CSS classes with clsx for conditional class application.
 * This is the standard shadcn-vue utility pattern.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

/**
 * Format an ISO date string for display.
 */
export function formatDate(date: string | null | undefined): string {
  if (!date) return '—'
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(date))
}

/**
 * Format a relative time string (e.g. "3 minutes ago").
 */
export function formatRelativeTime(date: string | null | undefined): string {
  if (!date) return '—'
  const now = Date.now()
  const then = new Date(date).getTime()
  const diffSec = Math.floor((now - then) / 1000)

  if (diffSec < 0) {
    const absDiffSec = Math.abs(diffSec)
    if (absDiffSec < 60) return 'in under a minute'
    if (absDiffSec < 3600) return `in ${Math.floor(absDiffSec / 60)}m`
    if (absDiffSec < 86400) return `in ${Math.floor(absDiffSec / 3600)}h`
    return `in ${Math.floor(absDiffSec / 86400)}d`
  }
  if (diffSec < 60) return 'just now'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
  return `${Math.floor(diffSec / 86400)}d ago`
}

/**
 * Format a minute value into a compact duration label.
 */
export function formatMinutesAsDuration(minutes: number | null | undefined): string {
  if (minutes == null) return 'Disabled'
  if (minutes < 60) return `${minutes} min`
  if (minutes % 1440 === 0) return `${minutes / 1440}d`
  if (minutes % 60 === 0) return `${minutes / 60}h`

  const hours = Math.floor(minutes / 60)
  const remainingMinutes = minutes % 60
  return `${hours}h ${remainingMinutes}m`
}
