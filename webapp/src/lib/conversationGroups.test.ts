import { describe, expect, it } from 'vitest'

import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'
import {
  capConversationGroups,
  conversationTitle,
  extractActiveConversations,
  formatTimeAgo,
  groupConversationsByTime,
  selectSidebarWorkspaces,
} from './conversationGroups'

const NOW = new Date('2026-03-15T15:00:00').getTime()

function conversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 's-1',
    workspace_id: 'ws-1',
    workspace_name: 'Alpha',
    title: 'First chat',
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    model: '',
    unread: false,
    updated_at: new Date(NOW).toISOString(),
    ...overrides,
  }
}

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return {
    id: 'ws-1',
    runner_id: 'r-1',
    status: WorkspaceStatus.RUNNING,
    active_operation: null,
    name: 'Alpha',
    runtime_type: 'docker',
    qemu_vcpus: null,
    qemu_memory_mb: null,
    qemu_disk_size_gb: null,
    desktop_width: 1920,
    desktop_height: 1080,
    created_by_id: 1,
    last_activity_at: new Date(NOW).toISOString(),
    auto_stop_timeout_minutes: null,
    auto_stop_at: null,
    delete_requested_at: null,
    delete_started_at: null,
    delete_confirmed_at: null,
    delete_last_error: '',
    delete_attempt_count: 0,
    created_at: new Date(NOW).toISOString(),
    updated_at: new Date(NOW).toISOString(),
    has_active_session: false,
    runner_online: true,
    credential_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

function atHour(isoDate: string, hour: number): string {
  const date = new Date(`${isoDate}T00:00:00`)
  date.setHours(hour, 0, 0, 0)
  return date.toISOString()
}

describe('conversationTitle', () => {
  it('returns the trimmed title or a fallback', () => {
    expect(conversationTitle(conversation({ title: '  Hello  ' }))).toBe('Hello')
    expect(conversationTitle(conversation({ title: '' }))).toBe('New chat')
    expect(conversationTitle(conversation({ title: '   ' }))).toBe('New chat')
  })
})

describe('formatTimeAgo', () => {
  it('uses compact German-style labels', () => {
    expect(formatTimeAgo(new Date(NOW - 30 * 1000).toISOString(), NOW)).toBe('jetzt')
    expect(formatTimeAgo(new Date(NOW - 5 * 60 * 1000).toISOString(), NOW)).toBe('5m')
    expect(formatTimeAgo(new Date(NOW - 2 * 60 * 60 * 1000).toISOString(), NOW)).toBe('2h')
    expect(formatTimeAgo(new Date(NOW - 3 * 24 * 60 * 60 * 1000).toISOString(), NOW)).toBe('3d')
  })
})

describe('extractActiveConversations', () => {
  it('puts busy sessions ahead of unread and caps at 5', () => {
    const conversations = [
      conversation({ session_id: 'unread-old', unread: true, updated_at: new Date(NOW - 60_000).toISOString() }),
      conversation({ session_id: 'busy-new', status: 'busy', updated_at: new Date(NOW - 10_000).toISOString() }),
      conversation({ session_id: 'busy-old', status: 'busy', updated_at: new Date(NOW - 20_000).toISOString() }),
      conversation({ session_id: 'unread-new', unread: true, updated_at: new Date(NOW).toISOString() }),
      conversation({ session_id: 'idle', unread: false, status: 'idle' }),
      conversation({ session_id: 'unread-3', unread: true, updated_at: new Date(NOW - 30_000).toISOString() }),
      conversation({ session_id: 'unread-4', unread: true, updated_at: new Date(NOW - 40_000).toISOString() }),
      conversation({ session_id: 'unread-5', unread: true, updated_at: new Date(NOW - 50_000).toISOString() }),
    ]

    const active = extractActiveConversations(conversations, 5)

    expect(active.map((row) => row.session_id)).toEqual([
      'busy-new',
      'busy-old',
      'unread-new',
      'unread-3',
      'unread-4',
    ])
  })
})

describe('groupConversationsByTime', () => {
  it('buckets around local midnight, 7 days, and 30 days', () => {
    const groups = groupConversationsByTime(
      [
        conversation({ session_id: 'today', updated_at: atHour('2026-03-15', 10) }),
        conversation({ session_id: 'yesterday', updated_at: atHour('2026-03-14', 22) }),
        conversation({ session_id: 'week', updated_at: atHour('2026-03-10', 12) }),
        conversation({ session_id: 'month', updated_at: atHour('2026-03-01', 12) }),
        conversation({ session_id: 'older', updated_at: atHour('2026-01-01', 12) }),
      ],
      NOW,
    )

    expect(groups.map((group) => [group.key, group.conversations.map((row) => row.session_id)])).toEqual([
      ['today', ['today']],
      ['yesterday', ['yesterday']],
      ['last7days', ['week']],
      ['last30days', ['month']],
      ['older', ['older']],
    ])
  })

  it('omits empty buckets and keeps newest-first order inside a bucket', () => {
    const groups = groupConversationsByTime(
      [
        conversation({ session_id: 'newer', updated_at: atHour('2026-03-15', 14) }),
        conversation({ session_id: 'older-today', updated_at: atHour('2026-03-15', 8) }),
      ],
      NOW,
    )

    expect(groups).toHaveLength(1)
    expect(groups[0]?.key).toBe('today')
    expect(groups[0]?.label).toBe('Heute')
    expect(groups[0]?.conversations.map((row) => row.session_id)).toEqual(['newer', 'older-today'])
  })
})

describe('capConversationGroups', () => {
  it('trims later groups and reports the hidden count', () => {
    const groups = groupConversationsByTime(
      [
        conversation({ session_id: 't1', updated_at: atHour('2026-03-15', 14) }),
        conversation({ session_id: 't2', updated_at: atHour('2026-03-15', 13) }),
        conversation({ session_id: 'y1', updated_at: atHour('2026-03-14', 12) }),
        conversation({ session_id: 'y2', updated_at: atHour('2026-03-14', 11) }),
      ],
      NOW,
    )

    const capped = capConversationGroups(groups, 3)

    expect(capped.hiddenCount).toBe(1)
    expect(capped.groups.map((group) => group.conversations.map((row) => row.session_id))).toEqual([
      ['t1', 't2'],
      ['y1'],
    ])
  })
})

describe('selectSidebarWorkspaces', () => {
  it('hides stopped workspaces without chats and prefers live ones', () => {
    const selected = selectSidebarWorkspaces(
      [
        workspace({
          id: 'stopped-empty',
          name: 'Idle',
          status: WorkspaceStatus.STOPPED,
          runner_online: false,
          last_activity_at: new Date(NOW).toISOString(),
        }),
        workspace({
          id: 'live',
          name: 'Live',
          last_activity_at: new Date(NOW - 60_000).toISOString(),
        }),
        workspace({
          id: 'stopped-with-chat',
          name: 'Archive',
          status: WorkspaceStatus.STOPPED,
          runner_online: false,
          last_activity_at: new Date(NOW - 10_000).toISOString(),
        }),
        workspace({
          id: 'creating',
          name: 'Booting',
          status: WorkspaceStatus.CREATING,
          runner_online: false,
          active_operation: WorkspaceOperation.CREATING,
          last_activity_at: new Date(NOW - 5_000).toISOString(),
        }),
      ],
      [conversation({ workspace_id: 'stopped-with-chat' })],
      4,
    )

    expect(selected.map((row) => row.id)).toEqual(['live', 'creating', 'stopped-with-chat'])
  })

  it('caps at the requested limit', () => {
    const selected = selectSidebarWorkspaces(
      [
        workspace({ id: 'a', last_activity_at: new Date(NOW).toISOString() }),
        workspace({ id: 'b', last_activity_at: new Date(NOW - 1).toISOString() }),
        workspace({ id: 'c', last_activity_at: new Date(NOW - 2).toISOString() }),
      ],
      [],
      2,
    )

    expect(selected.map((row) => row.id)).toEqual(['a', 'b'])
  })
})
