import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { markHarnessSessionRead } from '@/services/harness.api'
import type { HarnessConversation } from '@/types/harness'

vi.mock('@/services/harness.api', () => ({
  listHarnessConversations: vi.fn().mockResolvedValue([]),
  markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
}))

const markReadMock = vi.mocked(markHarnessSessionRead)

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 'session-1',
    workspace_id: 'ws-1',
    workspace_name: 'Workspace One',
    title: 'Fix tests',
    status: 'busy',
    mode: 'build',
    agent_name: 'build',
    model: 'acme/think',
    reasoning_effort: 'high',
    unread: false,
    updated_at: '2026-03-29T10:00:00.000Z',
    ...overrides,
  }
}

describe('harnessConversations store unread', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('marks idle as unread when the session is not being viewed', () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    store.updateSessionStatus('session-1', 'idle', false)
    expect(store.conversations[0]?.unread).toBe(true)
    expect(store.conversations[0]?.status).toBe('idle')
  })

  it('keeps idle viewed sessions read', () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    store.updateSessionStatus('session-1', 'idle', true)
    expect(store.conversations[0]?.unread).toBe(false)
  })

  it('clears unread while a session is busy', () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: true })]
    store.updateSessionStatus('session-1', 'busy')
    expect(store.conversations[0]?.unread).toBe(false)
    expect(store.conversations[0]?.status).toBe('busy')
  })

  it('persists mark-read via the API', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: true })]
    await store.markAsRead('session-1')
    expect(store.conversations[0]?.unread).toBe(false)
    expect(markReadMock).toHaveBeenCalledWith('session-1')
  })
})
