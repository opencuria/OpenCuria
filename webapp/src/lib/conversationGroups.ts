import { WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'

export const ACTIVE_CONVERSATION_LIMIT = 5
export const VISIBLE_CONVERSATION_LIMIT = 15
export const SIDEBAR_WORKSPACE_LIMIT = 4

export type TimeBucketKey = 'today' | 'yesterday' | 'last7days' | 'last30days' | 'older'

export interface TimeBucket {
  key: TimeBucketKey
  label: string
  conversations: HarnessConversation[]
}

const TIME_BUCKET_LABELS: Record<TimeBucketKey, string> = {
  today: 'Heute',
  yesterday: 'Gestern',
  last7days: 'Letzte 7 Tage',
  last30days: 'Letzte 30 Tage',
  older: 'Älter',
}

const HIDDEN_WORKSPACE_STATUSES = new Set<string>([
  WorkspaceStatus.REMOVED,
  WorkspaceStatus.DELETED,
  WorkspaceStatus.DELETING,
  WorkspaceStatus.PENDING_DELETION,
])

const BUCKET_ORDER: TimeBucketKey[] = [
  'today',
  'yesterday',
  'last7days',
  'last30days',
  'older',
]

/**
 * Display title for a conversation row (fallback when the session has no title yet).
 */
export function conversationTitle(conversation: HarnessConversation): string {
  return conversation.title?.trim() || 'New chat'
}

/**
 * Compact relative timestamp for sidebar rows (`jetzt`, `5m`, `2h`, `3d`).
 */
export function formatTimeAgo(isoString: string, now = Date.now()): string {
  const diff = now - new Date(isoString).getTime()
  const minutes = Math.floor(diff / 60000)
  if (minutes < 1) return 'jetzt'
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d`
  const weeks = Math.floor(days / 7)
  if (weeks < 52) return `${weeks}w`
  return `${Math.floor(days / 365)}y`
}

/**
 * Busy sessions first, then unread, newest first. Caps at `limit`.
 * Conversations that need user action are excluded (they live in Action required).
 */
export function extractActiveConversations(
  conversations: HarnessConversation[],
  limit = ACTIVE_CONVERSATION_LIMIT,
): HarnessConversation[] {
  return [...conversations]
    .filter(
      (conversation) =>
        !conversation.needs_attention &&
        (conversation.status === 'busy' || conversation.unread),
    )
    .sort((a, b) => {
      if (a.status === 'busy' && b.status !== 'busy') return -1
      if (a.status !== 'busy' && b.status === 'busy') return 1
      return new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
    })
    .slice(0, limit)
}

/**
 * Conversations waiting on a permission or question gate, newest first.
 */
export function extractActionRequired(
  conversations: HarnessConversation[],
): HarnessConversation[] {
  return [...conversations]
    .filter((conversation) => Boolean(conversation.needs_attention))
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
}

/**
 * Group conversations into calendar-day buckets relative to `now`.
 *
 * Conversations should already be sorted newest-first; this function preserves
 * relative order inside each bucket.
 */
export function groupConversationsByTime(
  conversations: HarnessConversation[],
  now = Date.now(),
): TimeBucket[] {
  const startToday = startOfLocalDay(now)
  const startYesterday = addLocalDays(startToday, -1)
  const start7 = addLocalDays(startToday, -7)
  const start30 = addLocalDays(startToday, -30)

  const buckets: Record<TimeBucketKey, HarnessConversation[]> = {
    today: [],
    yesterday: [],
    last7days: [],
    last30days: [],
    older: [],
  }

  for (const conversation of conversations) {
    const timestamp = new Date(conversation.updated_at).getTime()
    buckets[bucketForTimestamp(timestamp, startToday, startYesterday, start7, start30)].push(
      conversation,
    )
  }

  return BUCKET_ORDER.filter((key) => buckets[key].length > 0).map((key) => ({
    key,
    label: TIME_BUCKET_LABELS[key],
    conversations: buckets[key],
  }))
}

/**
 * Keep the first `limit` conversations across groups; later groups are trimmed
 * or dropped. Returns how many conversations were hidden.
 */
export function capConversationGroups(
  groups: TimeBucket[],
  limit = VISIBLE_CONVERSATION_LIMIT,
): { groups: TimeBucket[]; hiddenCount: number } {
  const total = groups.reduce((sum, group) => sum + group.conversations.length, 0)
  if (total <= limit) return { groups, hiddenCount: 0 }

  const capped: TimeBucket[] = []
  let remaining = limit
  for (const group of groups) {
    if (remaining <= 0) break
    const conversations = group.conversations.slice(0, remaining)
    capped.push({ ...group, conversations })
    remaining -= conversations.length
  }
  return { groups: capped, hiddenCount: total - limit }
}

/**
 * Workspaces shown in the compact sidebar section: live/operating ones and
 * any workspace that already has chats. Stopped empty workspaces stay on
 * `/workspaces`. Sorted live-first, then by `last_activity_at`.
 */
export function selectSidebarWorkspaces(
  workspaces: Workspace[],
  conversations: HarnessConversation[],
  limit = SIDEBAR_WORKSPACE_LIMIT,
): Workspace[] {
  const workspaceIdsWithChats = new Set(conversations.map((conversation) => conversation.workspace_id))
  return [...workspaces]
    .filter((workspace) => {
      if (HIDDEN_WORKSPACE_STATUSES.has(workspace.status)) return false
      if (workspaceIdsWithChats.has(workspace.id)) return true
      return isLiveWorkspace(workspace) || isOperatingWorkspace(workspace)
    })
    .sort((a, b) => {
      const liveDelta = Number(isLiveWorkspace(b)) - Number(isLiveWorkspace(a))
      if (liveDelta !== 0) return liveDelta
      return (
        new Date(b.last_activity_at ?? 0).getTime() -
        new Date(a.last_activity_at ?? 0).getTime()
      )
    })
    .slice(0, limit)
}

export function isLiveWorkspace(workspace: Workspace): boolean {
  return workspace.status === WorkspaceStatus.RUNNING && workspace.runner_online
}

export function isOperatingWorkspace(workspace: Workspace): boolean {
  return Boolean(workspace.active_operation) || workspace.status === WorkspaceStatus.CREATING
}

export function countableWorkspaces(workspaces: Workspace[]): Workspace[] {
  return workspaces.filter((workspace) => !HIDDEN_WORKSPACE_STATUSES.has(workspace.status))
}

function startOfLocalDay(now: number): number {
  const date = new Date(now)
  date.setHours(0, 0, 0, 0)
  return date.getTime()
}

function addLocalDays(timestamp: number, days: number): number {
  const date = new Date(timestamp)
  date.setDate(date.getDate() + days)
  return date.getTime()
}

function bucketForTimestamp(
  timestamp: number,
  startToday: number,
  startYesterday: number,
  start7: number,
  start30: number,
): TimeBucketKey {
  if (timestamp >= startToday) return 'today'
  if (timestamp >= startYesterday) return 'yesterday'
  if (timestamp >= start7) return 'last7days'
  if (timestamp >= start30) return 'last30days'
  return 'older'
}
