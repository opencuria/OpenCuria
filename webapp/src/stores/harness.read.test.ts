import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { markHarnessSessionRead } from '@/services/harness.api'
import type { HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
  }
})

const markReadMock = vi.mocked(markHarnessSessionRead)

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
  return {
    id: 'session-1',
    workspace_id: 'ws-1',
    parent_id: null,
    title: 'Chat',
    mode: 'build',
    agent_name: 'build',
    model: 'acme/think',
    reasoning_effort: 'high',
    status: 'busy',
    unread: false,
    cost: 0,
    tokens: {},
    ...overrides,
  }
}

describe('harness store read tracking', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('marks an idle session read when it becomes the viewing session', async () => {
    const store = useHarnessStore()
    const conversations = useHarnessConversationStore()
    store.sessions = [makeSession({ status: 'idle', unread: true })]
    conversations.conversations = [
      {
        session_id: 'session-1',
        workspace_id: 'ws-1',
        workspace_name: 'Workspace One',
        title: 'Chat',
        status: 'idle',
        mode: 'build',
        agent_name: 'build',
        model: 'acme/think',
        reasoning_effort: 'high',
        unread: true,
        updated_at: '2026-03-29T10:00:00.000Z',
      },
    ]

    store.setViewingSession('session-1')
    await vi.waitFor(() => expect(markReadMock).toHaveBeenCalledWith('session-1'))
    expect(store.sessions[0]?.unread).toBe(false)
    expect(conversations.conversations[0]?.unread).toBe(false)
  })

  it('marks read when an idle event arrives for the viewing session', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy', unread: false })]
    store.setViewingSession('session-1')
    store.handleSessionStatus('session-1', 'idle')
    await vi.waitFor(() => expect(markReadMock).toHaveBeenCalledWith('session-1'))
    expect(store.sessions[0]?.unread).toBe(false)
  })

  it('marks unread when an idle event arrives for a session that is not viewed', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy', unread: false })]
    store.setViewingSession(null)
    store.handleSessionStatus('session-1', 'idle')
    expect(store.sessions[0]?.unread).toBe(true)
    expect(markReadMock).not.toHaveBeenCalled()
  })
})
