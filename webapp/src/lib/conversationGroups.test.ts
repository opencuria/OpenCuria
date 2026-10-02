import { describe, expect, it } from 'vitest'

import { WorkspaceOperation, WorkspaceStatus } from '@/types'
import type { Workspace } from '@/types'
import type { HarnessConversation } from '@/types/harness'
import {
  conversationTitle,
  extractActionRequired,
  extractActiveConversations,
  formatTimeAgo,
  groupConversationsByWorkspace,
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
    last_message_at: new Date(NOW).toISOString(),
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
    plugin_ids: [],
    credentials_present: false,
    ...overrides,
  }
}

describe('conversationTitle', () => {
  it('returns the trimmed title or a fallback', () => {
    expect(conversationTitle(conversation({ title: '  Hello  ' }))).toBe('Hello')
    expect(conversationTitle(conversation({ title: '' }))).toBe('New chat')
    expect(conversationTitle(conversation({ title: '   ' }))).toBe('New chat')
  })
})

describe('formatTimeAgo', () => {
  it('uses compact labels', () => {
    expect(formatTimeAgo(new Date(NOW - 30 * 1000).toISOString(), NOW)).toBe('now')
    expect(formatTimeAgo(new Date(NOW - 5 * 60 * 1000).toISOString(), NOW)).toBe('5m')
    expect(formatTimeAgo(new Date(NOW - 2 * 60 * 60 * 1000).toISOString(), NOW)).toBe('2h')
    expect(formatTimeAgo(new Date(NOW - 3 * 24 * 60 * 60 * 1000).toISOString(), NOW)).toBe('3d')
  })
})

describe('extractActiveConversations', () => {
  it('puts busy sessions ahead of unread and caps at 5', () => {
    const conversations = [
      conversation({
        session_id: 'unread-old',
        unread: true,
        last_message_at: new Date(NOW - 60_000).toISOString(),
      }),
      conversation({
        session_id: 'busy-new',
        status: 'busy',
        last_message_at: new Date(NOW - 10_000).toISOString(),
      }),
      conversation({
        session_id: 'busy-old',
        status: 'busy',
        last_message_at: new Date(NOW - 20_000).toISOString(),
      }),
      conversation({
        session_id: 'unread-new',
        unread: true,
        last_message_at: new Date(NOW).toISOString(),
      }),
      conversation({ session_id: 'idle', unread: false, status: 'idle' }),
      conversation({
        session_id: 'unread-3',
        unread: true,
        last_message_at: new Date(NOW - 30_000).toISOString(),
      }),
      conversation({
        session_id: 'unread-4',
        unread: true,
        last_message_at: new Date(NOW - 40_000).toISOString(),
      }),
      conversation({
        session_id: 'unread-5',
        unread: true,
        last_message_at: new Date(NOW - 50_000).toISOString(),
      }),
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

  it('excludes chats that need attention', () => {
    const active = extractActiveConversations([
      conversation({ session_id: 'busy', status: 'busy' }),
      conversation({
        session_id: 'gate',
        status: 'busy',
        needs_attention: true,
        attention_kind: 'permission',
      }),
      conversation({ session_id: 'unread', unread: true }),
    ])
    expect(active.map((row) => row.session_id)).toEqual(['busy', 'unread'])
  })
})

describe('extractActionRequired', () => {
  it('returns attention chats newest first', () => {
    const rows = extractActionRequired([
      conversation({ session_id: 'idle' }),
      conversation({
        session_id: 'old-gate',
        needs_attention: true,
        attention_kind: 'question',
        last_message_at: new Date(NOW - 20_000).toISOString(),
      }),
      conversation({
        session_id: 'new-gate',
        needs_attention: true,
        attention_kind: 'permission',
        last_message_at: new Date(NOW).toISOString(),
      }),
    ])
    expect(rows.map((row) => row.session_id)).toEqual(['new-gate', 'old-gate'])
  })
})

describe('groupConversationsByWorkspace', () => {
  it('groups duplicate names by ID with deterministic workspace ID ties', () => {
    const workspaces = [workspace({ id: 'b' }), workspace({ id: 'a' })]
    const groups = groupConversationsByWorkspace(workspaces, [
      conversation({ session_id: 'b-chat', workspace_id: 'b' }),
      conversation({ session_id: 'a-chat', workspace_id: 'a' }),
      conversation({ session_id: 'a-chat-2', workspace_id: 'a' }),
    ])
    expect(
      groups.map((group) => [
        group.workspaceId,
        group.name,
        group.conversations.map((row) => row.session_id),
      ]),
    ).toEqual([
      ['a', 'Alpha', ['a-chat', 'a-chat-2']],
      ['b', 'Alpha', ['b-chat']],
    ])
    expect(groups[0]?.workspace).toBe(workspaces[1])
  })

  it('sorts live first, then numeric and case-insensitive alphabetical names in both partitions', () => {
    const workspaces = [
      workspace({ id: 'online-z', name: 'Zebra' }),
      workspace({ id: 'offline-10', name: 'alpha 10', runner_online: false }),
      workspace({ id: 'online-10', name: 'alpha 10' }),
      workspace({ id: 'offline-z', name: 'Zebra', runner_online: false }),
      workspace({ id: 'online-2', name: 'Alpha 2' }),
      workspace({ id: 'offline-2', name: 'Alpha 2', runner_online: false }),
    ]
    const groups = groupConversationsByWorkspace(workspaces, [])
    expect(groups.map((group) => group.workspaceId)).toEqual([
      'online-2',
      'online-10',
      'online-z',
      'offline-2',
      'offline-10',
      'offline-z',
    ])
    expect(groupConversationsByWorkspace([...workspaces].reverse(), [])).toEqual(groups)
  })

  it('includes only RUNNING workspaces, even when their runner is offline', () => {
    const groups = groupConversationsByWorkspace(
      [
        workspace({ id: 'running-online' }),
        workspace({ id: 'stopped-online', status: WorkspaceStatus.STOPPED }),
        workspace({ id: 'running-offline', runner_online: false }),
      ],
      [conversation({ workspace_id: 'stopped-online' })],
    )
    expect(groups.map((group) => [group.workspaceId, group.online])).toEqual([
      ['running-online', true],
      ['running-offline', false],
    ])
  })

  it('ignores activity, updated timestamps, busy and unread, sorting chats by last message then session ID', () => {
    const workspaces = [
      workspace({ id: 'z', name: 'Zebra', last_activity_at: new Date(NOW + 60_000).toISOString() }),
      workspace({ id: 'a', name: 'Alpha', last_activity_at: new Date(NOW - 60_000).toISOString() }),
    ]
    const conversations = ['z', 'a'].flatMap((workspace_id) => [
      conversation({
        workspace_id,
        session_id: `${workspace_id}-old`,
        status: 'busy',
        unread: true,
        updated_at: new Date(NOW + 60_000).toISOString(),
        last_message_at: new Date(NOW - 60_000).toISOString(),
      }),
      conversation({ workspace_id, session_id: `${workspace_id}-tie-b` }),
      conversation({
        workspace_id,
        session_id: `${workspace_id}-new`,
        updated_at: new Date(NOW - 60_000).toISOString(),
        last_message_at: new Date(NOW + 1).toISOString(),
      }),
      conversation({ workspace_id, session_id: `${workspace_id}-tie-a` }),
    ])
    const groups = groupConversationsByWorkspace(workspaces, conversations)
    expect(
      groups.map((group) => [group.workspaceId, group.conversations.map((row) => row.session_id)]),
    ).toEqual([
      ['a', ['a-new', 'a-tie-a', 'a-tie-b', 'a-old']],
      ['z', ['z-new', 'z-tie-a', 'z-tie-b', 'z-old']],
    ])
    expect(groupConversationsByWorkspace(workspaces, [...conversations].reverse())).toEqual(groups)
  })

  it('does not mutate input arrays or workspace and chat objects', () => {
    const workspaces = [workspace({ id: 'z', name: 'Zebra' }), workspace({ id: 'a' })]
    const conversations = [
      conversation({
        session_id: 'old',
        workspace_id: 'a',
        last_message_at: new Date(NOW - 1).toISOString(),
      }),
      conversation({ session_id: 'new', workspace_id: 'a' }),
    ]
    const before = structuredClone({ workspaces, conversations })
    workspaces.forEach(Object.freeze)
    conversations.forEach(Object.freeze)
    Object.freeze(workspaces)
    Object.freeze(conversations)
    const groups = groupConversationsByWorkspace(workspaces, conversations)
    expect({ workspaces, conversations }).toEqual(before)
    expect(groups[0]?.conversations).not.toBe(conversations)
    expect(groups[0]?.conversations[0]).toBe(conversations[1])
  })

  it('prefers current workspace names, then chat names, then a short workspace ID', () => {
    const groups = groupConversationsByWorkspace(
      [
        workspace({ id: 'current', name: '  Renamed  ' }),
        workspace({ id: 'blank', name: '   ' }),
        workspace({ id: '12345678-abcd', name: '   ' }),
        workspace({ id: '87654321-abcd', name: '' }),
      ],
      [
        conversation({ workspace_id: 'current', workspace_name: 'Old name' }),
        conversation({ workspace_id: 'blank', workspace_name: '  Chat name  ' }),
        conversation({ workspace_id: 'missing', workspace_name: '  Historical name  ' }),
        conversation({ workspace_id: '12345678-abcd', workspace_name: '   ' }),
      ],
    )
    expect(Object.fromEntries(groups.map((group) => [group.workspaceId, group.name]))).toEqual({
      current: 'Renamed',
      blank: 'Chat name',
      '12345678-abcd': 'Workspace 12345678',
      '87654321-abcd': 'Workspace 87654321',
    })
  })

  const nonRunningStatuses = Object.values(WorkspaceStatus).filter(
    (status) => status !== WorkspaceStatus.RUNNING,
  )

  it.each(nonRunningStatuses)(
    'excludes %s workspaces with chats, even during active operations',
    (status) => {
      const workspaces = [
        workspace({ id: 'running' }),
        workspace({ id: `${status}-history`, status }),
        ...Object.values(WorkspaceOperation).flatMap((active_operation) => [
          workspace({ id: `${status}-${active_operation}-history`, status, active_operation }),
          workspace({ id: `${status}-${active_operation}-empty`, status, active_operation }),
        ]),
      ]
      const conversations = workspaces
        .filter(({ id }) => !id.endsWith('-empty'))
        .map(({ id }) =>
          conversation({
            workspace_id: id,
            session_id: `${id}-chat`,
            status: 'busy',
            unread: true,
          }),
        )
      conversations.push(conversation({ workspace_id: 'missing' }))

      const groups = groupConversationsByWorkspace(workspaces, conversations)

      expect(groups.map((group) => group.workspaceId)).toEqual(['running'])
      expect(groups[0]?.conversations).toEqual([conversations[0]])
    },
  )

  it('shows empty running workspaces, including literal running status with an offline runner', () => {
    const groups = groupConversationsByWorkspace(
      [
        workspace({ id: 'online' }),
        workspace({
          id: 'runner-offline',
          status: 'running' as WorkspaceStatus,
          runner_online: false,
        }),
        workspace({
          id: 'starting',
          status: WorkspaceStatus.STOPPED,
          active_operation: WorkspaceOperation.STARTING,
        }),
        workspace({ id: 'creating', status: WorkspaceStatus.CREATING }),
      ],
      [],
    )
    expect(groups.map((group) => [group.workspaceId, group.online])).toEqual([
      ['online', true],
      ['runner-offline', false],
    ])
    expect(groups.every((group) => group.conversations.length === 0)).toBe(true)
  })

  it('omits unknown history while the workspace list loads until a running workspace arrives', () => {
    const conversations = [conversation({ workspace_name: 'Historical name' })]
    const before = structuredClone(conversations)

    expect(groupConversationsByWorkspace([], conversations)).toEqual([])
    expect(
      groupConversationsByWorkspace(
        [workspace({ status: WorkspaceStatus.CREATING })],
        conversations,
      ),
    ).toEqual([])
    const groups = groupConversationsByWorkspace(
      [workspace({ name: 'Current name' })],
      conversations,
    )

    expect(groups.map((group) => [group.workspaceId, group.name])).toEqual([
      ['ws-1', 'Current name'],
    ])
    expect(groups[0]?.conversations).toEqual(conversations)
    expect(groups[0]?.conversations[0]).toBe(conversations[0])
    expect(conversations).toEqual(before)
  })

  it('does not apply a global workspace or conversation cap', () => {
    const workspaces = Array.from({ length: 12 }, (_, index) => workspace({ id: `ws-${index}` }))
    const conversations = workspaces.flatMap(({ id }) =>
      Array.from({ length: 8 }, (_, index) =>
        conversation({ workspace_id: id, session_id: `${id}-chat-${index}` }),
      ),
    )
    const groups = groupConversationsByWorkspace(workspaces, conversations)
    expect(groups).toHaveLength(12)
    expect(groups.every((group) => group.conversations.length === 8)).toBe(true)
  })

  it('removes stopped groups and restores their chats on start without changing raw inputs', () => {
    const alpha = workspace({ id: 'a', name: 'Alpha' })
    const zebra = workspace({ id: 'z', name: 'Zebra' })
    const conversations = [
      conversation({
        session_id: 'a-old',
        workspace_id: 'a',
        last_message_at: new Date(NOW - 1).toISOString(),
      }),
      conversation({ session_id: 'z-chat', workspace_id: 'z' }),
      conversation({ session_id: 'a-new', workspace_id: 'a' }),
    ]
    const before = structuredClone(conversations)
    conversations.forEach(Object.freeze)
    Object.freeze(conversations)
    const initial = groupConversationsByWorkspace([alpha, zebra], conversations)
    const stopped = groupConversationsByWorkspace(
      [
        {
          ...alpha,
          status: WorkspaceStatus.STOPPED,
          active_operation: WorkspaceOperation.STARTING,
        },
        zebra,
      ],
      conversations,
    )
    expect(stopped.map((group) => group.workspaceId)).toEqual(['z'])

    const restarted = groupConversationsByWorkspace(
      [alpha, { ...zebra, runner_online: false }],
      conversations,
    )
    expect(restarted.map((group) => [group.workspaceId, group.online])).toEqual([
      ['a', true],
      ['z', false],
    ])
    expect(restarted[0]?.conversations.map((row) => row.session_id)).toEqual(['a-new', 'a-old'])
    expect(restarted[0]?.conversations[0]).toBe(conversations[2])
    expect(restarted[0]?.conversations).toEqual(initial[0]?.conversations)
    expect(initial.map((group) => [group.workspaceId, group.online])).toEqual([
      ['a', true],
      ['z', true],
    ])
    expect(conversations).toEqual(before)
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
