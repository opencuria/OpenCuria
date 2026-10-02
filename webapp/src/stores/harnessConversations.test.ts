import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
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
    last_message_at: '2026-03-29T10:00:00.000Z',
    ...overrides,
  }
}

describe('harnessConversations store unread', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    vi.clearAllMocks()
    localStorage.removeItem('kern_active_org_id')
  })

  afterEach(() => {
    localStorage.removeItem('kern_active_org_id')
    vi.clearAllTimers()
    vi.useRealTimers()
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

  it('does not persist duplicate reads when already read and coalesces an in-flight mark-read', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: false })]
    await store.markAsRead('session-1')
    expect(markReadMock).not.toHaveBeenCalled()

    store.conversations[0]!.unread = true
    let resolve!: () => void
    markReadMock.mockImplementationOnce(() => new Promise<void>((r) => { resolve = r }))
    const first = store.markAsRead('session-1')
    const second = store.markAsRead('session-1')
    expect(markReadMock).toHaveBeenCalledTimes(1)
    resolve()
    await Promise.all([first, second])
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
  })

  it('sorts fetched conversations by last_message_at', async () => {
    listMock.mockResolvedValueOnce([
      makeConversation({
        session_id: 'older',
        last_message_at: '2026-03-29T09:00:00.000Z',
      }),
      makeConversation({
        session_id: 'newer',
        last_message_at: '2026-03-29T11:00:00.000Z',
      }),
    ])
    const store = useHarnessConversationStore()
    await store.fetchConversations()
    expect(store.conversations.map((row) => row.session_id)).toEqual(['newer', 'older'])
  })

  it('does not manufacture timestamps or reorder conversations on status transitions', () => {
    vi.setSystemTime(new Date('2026-03-29T12:00:00.000Z'))
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ session_id: 'recent', status: 'idle', last_message_at: '2026-03-29T11:00:00.000Z' }),
      makeConversation({ status: 'idle' }),
    ]
    const originalOrder = store.conversations
    store.updateSessionStatus('session-1', 'busy')
    store.updateSessionStatus('session-1', 'idle')
    expect(store.conversations).toBe(originalOrder)
    expect(store.conversations.map((row) => row.session_id)).toEqual(['recent', 'session-1'])
    expect(store.conversations.map((row) => row.last_message_at)).toEqual([
      '2026-03-29T11:00:00.000Z',
      '2026-03-29T10:00:00.000Z',
    ])
    expect(store.conversations[1]?.status).toBe('idle')
    expect(store.conversations[1]?.unread).toBe(true)
    expect(listMock).not.toHaveBeenCalled()
  })

  it('coalesces status changes and applies authoritative timestamps and order after refresh', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ session_id: 'recent', status: 'idle', last_message_at: '2026-03-29T11:00:00.000Z' }),
      makeConversation({ status: 'idle' }),
    ]
    listMock.mockResolvedValueOnce([
      makeConversation({ session_id: 'recent', status: 'busy', last_message_at: '2026-03-29T11:00:00.000Z' }),
      makeConversation({ status: 'idle', unread: true, last_message_at: '2026-03-29T11:30:00.000Z' }),
    ])
    store.updateSessionStatus('session-1', 'busy')
    await vi.advanceTimersByTimeAsync(200)
    store.updateSessionStatus('recent', 'busy')
    store.updateSessionStatus('session-1', 'idle')
    await vi.advanceTimersByTimeAsync(299)
    expect(listMock).not.toHaveBeenCalled()
    expect(store.conversations.map((row) => row.session_id)).toEqual(['recent', 'session-1'])
    await vi.advanceTimersByTimeAsync(1)
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(store.conversations.map((row) => row.session_id)).toEqual(['session-1', 'recent'])
    expect(store.conversations[0]?.last_message_at).toBe('2026-03-29T11:30:00.000Z')
  })

  it('does not refresh repeated known statuses but still updates unread immediately', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: true })]
    store.updateSessionStatus('session-1', 'idle', true)
    expect(store.conversations[0]?.unread).toBe(false)
    store.updateSessionStatus('session-1', 'idle', false)
    expect(store.conversations[0]?.unread).toBe(true)
    await vi.advanceTimersByTimeAsync(1000)
    expect(listMock).not.toHaveBeenCalled()
    expect(store.conversations[0]?.last_message_at).toBe('2026-03-29T10:00:00.000Z')
  })

  it('does not refresh for unknown or child session statuses', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    store.updateSessionStatus('new-session', 'busy')
    store.updateSessionStatus('new-session', 'idle')
    store.updateSessionStatus('child-session', 'busy')
    await vi.advanceTimersByTimeAsync(1000)
    expect(listMock).not.toHaveBeenCalled()
    expect(store.conversations).toEqual([makeConversation()])
  })

  it('waits for an older fetch and coalesces invalidations into one fresh fetch', async () => {
    let resolve!: (rows: HarnessConversation[]) => void
    listMock.mockImplementationOnce(() => new Promise((r) => { resolve = r }))
    const authoritative = makeConversation({ status: 'idle', last_message_at: '2026-03-29T11:30:00.000Z' })
    listMock.mockResolvedValueOnce([authoritative])
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    const originalFetch = store.fetchConversations()
    const duplicateFetch = store.fetchConversations()
    store.updateSessionStatus('session-1', 'idle')
    await vi.advanceTimersByTimeAsync(300)
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(store.conversations[0]?.last_message_at).toBe('2026-03-29T10:00:00.000Z')
    store.updateSessionStatus('session-1', 'busy')
    store.updateSessionStatus('session-1', 'idle')
    await vi.advanceTimersByTimeAsync(300)
    expect(listMock).toHaveBeenCalledTimes(1)
    resolve([makeConversation()])
    await Promise.all([originalFetch, duplicateFetch])
    await vi.advanceTimersByTimeAsync(0)
    expect(listMock).toHaveBeenCalledTimes(2)
    expect(store.conversations).toEqual([authoritative])
    await vi.advanceTimersByTimeAsync(1000)
    expect(listMock).toHaveBeenCalledTimes(2)
  })

  it('refreshes after an older in-flight request rejects', async () => {
    let reject!: (error: Error) => void
    listMock.mockImplementationOnce(() => new Promise((_, r) => { reject = r }))
    const authoritative = makeConversation({ status: 'idle', last_message_at: '2026-03-29T11:30:00.000Z' })
    listMock.mockResolvedValueOnce([authoritative])
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    const originalFetch = store.fetchConversations()
    store.updateSessionStatus('session-1', 'idle')
    await vi.advanceTimersByTimeAsync(300)
    reject(new Error('offline'))
    await originalFetch
    await vi.advanceTimersByTimeAsync(0)
    expect(listMock).toHaveBeenCalledTimes(2)
    expect(store.conversations).toEqual([authoritative])
    expect(store.error).toBeNull()
  })

  it('does not follow up a waiting refresh after the organization changes', async () => {
    localStorage.setItem('kern_active_org_id', 'org-a')
    let resolve!: (rows: HarnessConversation[]) => void
    listMock.mockImplementationOnce(() => new Promise((r) => { resolve = r }))
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    const originalFetch = store.fetchConversations()
    store.updateSessionStatus('session-1', 'idle')
    await vi.advanceTimersByTimeAsync(300)
    localStorage.setItem('kern_active_org_id', 'org-b')
    resolve([makeConversation()])
    await originalFetch
    await vi.advanceTimersByTimeAsync(1000)
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(store.conversations[0]?.status).toBe('idle')
    expect(store.conversations[0]?.last_message_at).toBe('2026-03-29T10:00:00.000Z')
  })

  it('does not follow up a waiting refresh after switching organizations away and back', async () => {
    localStorage.setItem('kern_active_org_id', 'org-a')
    let resolve!: (rows: HarnessConversation[]) => void
    listMock.mockImplementationOnce(() => new Promise((r) => { resolve = r }))
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation()]
    const originalFetch = store.fetchConversations()
    store.updateSessionStatus('session-1', 'idle')
    await vi.advanceTimersByTimeAsync(300)
    localStorage.setItem('kern_active_org_id', 'org-b')
    listMock.mockResolvedValueOnce([])
    await store.fetchConversations()
    localStorage.setItem('kern_active_org_id', 'org-a')
    const current = makeConversation({ status: 'idle', last_message_at: '2026-03-29T12:00:00.000Z' })
    listMock.mockResolvedValueOnce([current])
    await store.fetchConversations()
    resolve([makeConversation()])
    await originalFetch
    await vi.advanceTimersByTimeAsync(1000)
    expect(listMock).toHaveBeenCalledTimes(3)
    expect(store.conversations).toEqual([current])
  })

  it('keeps local status, unread state, timestamps and order when refresh fails', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [
      makeConversation({ session_id: 'recent', last_message_at: '2026-03-29T11:00:00.000Z' }),
      makeConversation({ manual_unread: true, unread: true }),
    ]
    listMock.mockRejectedValueOnce(new Error('offline'))
    store.updateSessionStatus('session-1', 'idle', true)
    const localState = store.conversations.map((row) => ({ ...row }))
    await vi.advanceTimersByTimeAsync(300)
    expect(listMock).toHaveBeenCalledTimes(1)
    expect(store.conversations).toEqual(localState)
    expect(store.conversations[1]?.status).toBe('idle')
    expect(store.conversations[1]?.manual_unread).toBe(true)
    expect(store.conversations[1]?.unread).toBe(true)
    expect(store.conversations[1]?.last_message_at).toBe('2026-03-29T10:00:00.000Z')
    expect(store.error).toBe('offline')
    expect(store.loading).toBe(false)
  })

  it('does not bump last_message_at when marking read', async () => {
    const store = useHarnessConversationStore()
    store.conversations = [makeConversation({ status: 'idle', unread: true })]
    await store.markAsRead('session-1')
    expect(store.conversations[0]?.last_message_at).toBe('2026-03-29T10:00:00.000Z')
  })
})
