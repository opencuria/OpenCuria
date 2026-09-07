import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { listHarnessConversations, markHarnessSessionRead, markHarnessSessionUnread } from '@/services/harness.api'
import type { HarnessConversation } from '@/types/harness'

vi.mock('@/services/harness.api', () => ({
  listHarnessConversations: vi.fn().mockResolvedValue([]),
  markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  markHarnessSessionUnread: vi.fn().mockResolvedValue(undefined),
}))

const markReadMock = vi.mocked(markHarnessSessionRead)
const markUnreadMock = vi.mocked(markHarnessSessionUnread)
const listMock = vi.mocked(listHarnessConversations)

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

  it('exposes busy and unread conversations as activeConversations', () => {
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ session_id: 'idle', status: 'idle', unread: false }),
      makeConversation({ session_id: 'busy', status: 'busy', unread: false }),
      makeConversation({ session_id: 'unread', status: 'idle', unread: true }),
    ]
    expect(store.activeConversations.map((row) => row.session_id)).toEqual(['busy', 'unread'])
  })

  it('persists mark-read via the API', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: true })]
    await store.markAsRead('session-1')
    expect(store.conversations[0]?.unread).toBe(false)
    expect(store.conversations[0]?.manual_unread).toBe(false)
    expect(markReadMock).toHaveBeenCalledWith('session-1')
  })

  it('persists mark-unread via the API', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: false })]
    await store.markAsUnread('session-1')
    expect(store.conversations[0]?.unread).toBe(true)
    expect(store.conversations[0]?.manual_unread).toBe(true)
    expect(markUnreadMock).toHaveBeenCalledWith('session-1')
  })

  it('rolls back mark-unread when the API fails', async () => {
    markUnreadMock.mockRejectedValueOnce(new Error('offline'))
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: false })]
    await store.markAsUnread('session-1')
    expect(store.conversations[0]?.unread).toBe(false)
    expect(store.conversations[0]?.manual_unread).toBe(false)
  })

  it('does not clear manual unread when a session becomes busy', () => {
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ status: 'idle', unread: true, manual_unread: true }),
    ]
    store.updateSessionStatus('session-1', 'busy')
    expect(store.conversations[0]?.unread).toBe(true)
    expect(store.conversations[0]?.manual_unread).toBe(true)
    expect(store.conversations[0]?.status).toBe('busy')
  })

  it('does not clear manual unread on idle viewed events', () => {
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ status: 'busy', unread: true, manual_unread: true }),
    ]
    store.updateSessionStatus('session-1', 'idle', true)
    expect(store.conversations[0]?.unread).toBe(true)
    expect(store.conversations[0]?.manual_unread).toBe(true)
  })

  it('sets attention kind and merges permission plus question', () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    store.setAttention('session-1', 'permission')
    expect(store.conversations[0]?.needs_attention).toBe(true)
    expect(store.conversations[0]?.attention_kind).toBe('permission')
    store.setAttention('session-1', 'question')
    expect(store.conversations[0]?.attention_kind).toBe('both')
  })

  it('clears attention immediately then refreshes from the server', async () => {
    vi.useFakeTimers()
    listMock.mockResolvedValueOnce([])
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ needs_attention: true, attention_kind: 'permission' }),
    ]
    store.clearAttention('session-1')
    expect(store.conversations[0]?.needs_attention).toBe(false)
    expect(store.conversations[0]?.attention_kind).toBe('')
    await vi.advanceTimersByTimeAsync(300)
    expect(listMock).toHaveBeenCalled()
    vi.useRealTimers()
  })
})
