import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import {
  getHarnessPart,
  listHarnessParts,
  listHarnessSessions,
  listHarnessTodos,
} from '@/services/harness.api'
import type { HarnessMessage, HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    getHarnessPart: vi.fn(),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

const partsMock = vi.mocked(listHarnessParts)
const partDetailMock = vi.mocked(getHarnessPart)
const todosMock = vi.mocked(listHarnessTodos)
const sessionsMock = vi.mocked(listHarnessSessions)

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
  return {
    id: 'session-1',
    workspace_id: 'ws-1',
    parent_id: null,
    title: 'Chat',
    mode: 'build',
    agent_name: 'build',
    model: 'm',
    status: 'idle',
    cost: 0,
    tokens: {},
    ...overrides,
  }
}

function makeMessage(sessionId: string, content: string): HarnessMessage {
  return {
    id: `message-${sessionId}`,
    session_id: sessionId,
    role: 'user',
    content,
    parts: [],
  }
}

describe('harness transcript memory policy', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    partsMock.mockImplementation(async (sessionId) => ({
      session: makeSession({ id: sessionId }),
      messages: [makeMessage(sessionId, `transcript for ${sessionId}`)],
    }))
    partDetailMock.mockReset()
    todosMock.mockResolvedValue([])
    sessionsMock.mockResolvedValue([])
  })

  it('retains four recent inactive transcripts and refetches the least-recently-used eviction', async () => {
    const store = useHarnessStore()
    store.sessions = Array.from({ length: 6 }, (_, index) =>
      makeSession({ id: `session-${index + 1}` }),
    )

    for (let index = 1; index <= 4; index += 1) {
      await store.fetchParts(`session-${index}`, false)
    }

    expect(partsMock).toHaveBeenCalledTimes(4)
    expect(store.sessions.map((session) => session.id)).toHaveLength(6)

    // Fetching a cached transcript still preserves original behavior: issue a
    // server request rather than returning from the local cache.
    await store.fetchParts('session-1', false)
    expect(partsMock).toHaveBeenCalledTimes(5)

    // Touch session-1, then add a fifth inactive transcript. Session-2 is
    // least-recently-used and is evicted; its next fetch reloads it.
    await store.fetchParts('session-5', false)
    expect(store.messagesBySession['session-1']).toBeDefined()
    expect(store.messagesBySession['session-2']).toBeUndefined()
    expect(store.messagesBySession['session-3']).toBeDefined()

    await store.fetchParts('session-2', false)
    expect(partsMock).toHaveBeenCalledTimes(7)
    expect(store.messagesBySession['session-2']?.[0]?.content).toBe('transcript for session-2')
  })

  it('preserves ancestor and descendant transcripts for the active subtask UI', async () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'root' }),
      makeSession({ id: 'child', parent_id: 'root' }),
      ...Array.from({ length: 5 }, (_, index) => makeSession({ id: `inactive-${index + 1}` })),
    ]
    store.setActiveSession('child')

    for (const id of [
      'root',
      'child',
      'inactive-1',
      'inactive-2',
      'inactive-3',
      'inactive-4',
      'inactive-5',
    ]) {
      await store.fetchParts(id, false)
    }

    expect(store.messagesBySession.root).toBeDefined()
    expect(store.messagesBySession.child).toBeDefined()
    expect(store.messagesBySession['inactive-1']).toBeUndefined()
  })

  it('coalesces concurrent fetches while always reconciling from the server', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    let finishFetch!: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void
    partsMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishFetch = resolve
        }),
    )

    const firstFetch = store.fetchParts('session-1', false)
    const concurrentFetch = store.fetchParts('session-1', false)
    expect(partsMock).toHaveBeenCalledTimes(1)

    finishFetch({
      session: makeSession(),
      messages: [makeMessage('session-1', 'server snapshot')],
    })
    await Promise.all([firstFetch, concurrentFetch])
    expect(store.messagesBySession['session-1']?.[0]?.content).toBe('server snapshot')

    await store.fetchParts('session-1', false)
    expect(partsMock).toHaveBeenCalledTimes(2)
  })

  it('enforces the estimated payload budget for inactive transcripts', async () => {
    const store = useHarnessStore()
    const oversizedContent = 'x'.repeat(8.5 * 1024 * 1024)
    store.sessions = [makeSession({ id: 'large' })]
    partsMock.mockResolvedValueOnce({
      session: makeSession({ id: 'large' }),
      messages: [makeMessage('large', oversizedContent)],
    })

    await store.fetchParts('large', false)

    expect(store.messagesBySession.large).toBeUndefined()
    expect(store.sessions[0]?.id).toBe('large')
  })

  it('preserves active, viewing, busy, and detail-pinned sessions outside the inactive budget', async () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'active' }),
      makeSession({ id: 'viewed' }),
      makeSession({ id: 'busy', status: 'busy' }),
      makeSession({ id: 'pinned' }),
      ...Array.from({ length: 5 }, (_, index) => makeSession({ id: `inactive-${index + 1}` })),
    ]
    store.setActiveSession('active')
    store.setViewingSession('viewed')
    store.pinPartDetail('pinned', 'part-1', true)

    for (const id of [
      'pinned',
      'active',
      'viewed',
      'busy',
      'inactive-1',
      'inactive-2',
      'inactive-3',
      'inactive-4',
      'inactive-5',
    ]) {
      await store.fetchParts(id, false)
    }

    expect(store.messagesBySession.active).toBeDefined()
    expect(store.messagesBySession.viewed).toBeDefined()
    expect(store.messagesBySession.busy).toBeDefined()
    expect(store.messagesBySession.pinned).toBeDefined()
    expect(store.messagesBySession['inactive-1']).toBeUndefined()
    expect(store.messagesBySession['inactive-5']).toBeDefined()
  })

  it('clears associated detail errors and ignores a lazy detail response after eviction', async () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'session-1' }),
      ...Array.from({ length: 5 }, (_, index) => makeSession({ id: `session-${index + 2}` })),
    ]
    await store.fetchParts('session-1', false)
    store.messagesBySession['session-1'] = [
      {
        ...makeMessage('session-1', ''),
        role: 'assistant',
        parts: [
          {
            id: 'part-1',
            session_id: 'session-1',
            type: 'tool',
            state: 'completed',
            title: 'Read',
            output: '',
            detail_loaded: false,
          },
        ],
      },
    ]
    store.setActiveSession('session-1')
    partDetailMock.mockRejectedValueOnce(new Error('offline'))
    await expect(store.fetchPartDetail('session-1', 'part-1')).rejects.toThrow('offline')
    expect(store.detailErrors['session-1:part-1']).toBe('offline')
    store.setActiveSession('session-2')

    for (const id of ['session-2', 'session-3', 'session-4', 'session-5', 'session-6']) {
      await store.fetchParts(id, false)
    }

    expect(store.messagesBySession['session-1']).toBeUndefined()
    expect(store.detailErrors['session-1:part-1']).toBeUndefined()
  })

  it('protects a session while a lazy detail request is in flight', async () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'session-1' }),
      ...Array.from({ length: 5 }, (_, index) => makeSession({ id: `session-${index + 2}` })),
    ]
    await store.fetchParts('session-1', false)
    store.messagesBySession['session-1'] = [
      {
        ...makeMessage('session-1', ''),
        role: 'assistant',
        parts: [
          {
            id: 'part-1',
            session_id: 'session-1',
            type: 'tool',
            state: 'completed',
            title: 'Read',
            output: '',
            detail_loaded: false,
          },
        ],
      },
    ]
    store.setActiveSession('session-1')
    let resolve!: (value: Awaited<ReturnType<typeof getHarnessPart>>) => void
    partDetailMock.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const detailRequest = store.fetchPartDetail('session-1', 'part-1')
    store.setActiveSession(null)

    for (const id of ['session-2', 'session-3', 'session-4', 'session-5', 'session-6']) {
      await store.fetchParts(id, false)
    }
    expect(store.messagesBySession['session-1']).toBeDefined()

    resolve({ id: 'part-1', output: 'stale' } as Awaited<ReturnType<typeof getHarnessPart>>)
    await detailRequest
    expect(store.messagesBySession['session-1']).toBeUndefined()
  })

  it('invalidates pending transcript responses on reset', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    let resolve!: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void
    partsMock.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )

    const fetch = store.fetchParts('session-1', false)
    store.reset()
    resolve({ session: makeSession(), messages: [makeMessage('session-1', 'stale')] })
    await fetch

    expect(store.messagesBySession['session-1']).toBeUndefined()
    expect(store.sessions).toEqual([])
  })

  it('clears workspace transcript caches and invalidates old workspace requests', async () => {
    const store = useHarnessStore()
    await store.fetchSessions('ws-1')
    store.sessions = [makeSession()]
    store.setActiveSession('session-1')
    store.setViewingSession('session-1')
    store.messagesBySession['session-1'] = [makeMessage('session-1', 'old workspace')]
    store.todosBySession['session-1'] = []

    let resolve!: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void
    partsMock.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const oldFetch = store.fetchParts('session-1', false)
    sessionsMock.mockResolvedValueOnce([makeSession({ id: 'new-session', workspace_id: 'ws-2' })])
    await store.fetchSessions('ws-2')
    resolve({ session: makeSession(), messages: [makeMessage('session-1', 'stale old data')] })
    await oldFetch

    expect(store.activeSessionId).toBeNull()
    expect(store.viewingSessionId).toBeNull()
    expect(store.messagesBySession['session-1']).toBeUndefined()
    expect(store.todosBySession['session-1']).toBeUndefined()
    expect(store.sessions.map((session) => session.id)).toEqual(['new-session'])
  })
})
