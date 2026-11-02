// Disk usage rules are shared with the mobile app; only the colors are web-specific.
import type { UsageLevel } from '@opencuria/chat-core/workspaceDisk'

export * from '@opencuria/chat-core/workspaceDisk'

/** Tailwind classes per level: meter fill and accompanying text. */
export const USAGE_LEVEL_CLASSES: Record<UsageLevel, { fill: string; text: string }> = {
  ok: { fill: 'bg-primary', text: 'text-muted-foreground' },
  warning: { fill: 'bg-amber-500', text: 'text-amber-600 dark:text-amber-400' },
  critical: { fill: 'bg-destructive', text: 'text-destructive' },
}
