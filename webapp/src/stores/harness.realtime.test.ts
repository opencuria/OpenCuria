import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import {
  listHarnessParts,
  listHarnessSessions,
  sendHarnessMessage,
} from '@/services/harness.api'
import type { HarnessMessage, HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    sendHarnessMessage: vi.fn(),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }),
}))

const partsMock = vi.mocked(listHarnessParts)
const sessionsMock = vi.mocked(listHarnessSessions)
const sendMock = vi.mocked(sendHarnessMessage)

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

function makeMessage(overrides: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'msg-1',
    session_id: 'session-1',
    role: 'user',
    content: 'hello',
    parts: [],
    ...overrides,
  }
}

describe('harness store realtime reliability', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    partsMock.mockResolvedValue({ session: makeSession(), messages: [] })
    sessionsMock.mockResolvedValue([])
  })

  it('routes a fresh turn by message_id instead of the previous answer', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy' })]
    store.messagesBySession['session-1'] = [
      makeMessage({ id: 'user-1', content: 'first', position: 0 }),
      makeMessage({
        id: 'assistant-old',
        role: 'assistant',
        content: 'old answer',
        parts: [
          { id: 'part-old', session_id: 'session-1', type: 'text', state: 'running', title: '', output: 'old answer' },
        ],
        position: 1,
      }),
      makeMessage({ id: 'user-2', content: 'follow up', position: 2 }),
    ]

    store.handlePartUpdated('session-1', { text: 'new' }, { messageId: 'assistant-new' })

    expect(store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-old')?.content).toBe(
      'old answer',
    )
    expect(store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-new')?.content).toBe(
      'new',
    )
  })

  it('drops late stale events for a completed turn (latest turn untouched)', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy' })]
    store.messagesBySession['session-1'] = [
      makeMessage({ id: 'user-1', content: 'first' }),
      makeMessage({
        id: 'assistant-old',
        role: 'assistant',
        content: 'old',
        parts: [],
        completed_at: '2026-03-29T10:00:00.000Z',
      }),
      makeMessage({ id: 'user-2', content: 'second' }),
      makeMessage({ id: 'assistant-new', role: 'assistant', content: '', parts: [] }),
    ]

    store.handlePartUpdated('session-1', { text: 'stale' }, { messageId: 'assistant-old' })

    expect(store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-old')?.content).toBe('old')
    expect(store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-new')?.content).toBe('')
  })

  it('anchors the busy shell after the optimistic follow-up user', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'idle' })]
    store.messagesBySession['session-1'] = [
      makeMessage({ id: 'user-1', content: 'first' }),
      makeMessage({ id: 'assistant-1', role: 'assistant', content: 'done', completed_at: '2026-03-29T10:00:00.000Z' }),
      makeMessage({ id: 'local-user-session-1-7', content: 'follow up' }),
    ]

    store.handleSessionStatus('session-1', 'busy', {
      message_id: 'assistant-fresh',
      user_message_id: 'local-user-session-1-7',
    })

    const ids = store.messagesBySession['session-1']?.map((m) => m.id)
    expect(ids).toEqual(['user-1', 'assistant-1', 'local-user-session-1-7', 'assistant-fresh'])
    expect(store.sessions[0]?.status).toBe('busy')
  })

  it('keeps queued pending tool then flips the same part to running', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy' })]
    store.messagesBySession['session-1'] = [makeMessage({ id: 'user-1' })]

    store.handlePartUpdated(
      'session-1',
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1', state: 'pending' },
      { partId: 'part-1', messageId: 'assistant-1' },
    )
    let assistant = store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-1')
    expect(assistant?.parts).toHaveLength(1)
    expect(assistant?.parts[0]?.state).toBe('pending')

    store.handlePartUpdated(
      'session-1',
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1', state: 'running' },
      { partId: 'part-1', messageId: 'assistant-1' },
    )
    assistant = store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-1')
    expect(assistant?.parts).toHaveLength(1)
    expect(assistant?.parts[0]?.state).toBe('running')
  })

  it('routes interleaved text/tool deltas without merging across tools', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy' })]
    store.messagesBySession['session-1'] = [makeMessage({ id: 'user-1' })]

    store.handlePartUpdated('session-1', { text: 'before ' }, { partId: 'text-1', messageId: 'assistant-1' })
    store.handlePartUpdated(
      'session-1',
      { tool_started: 'read', title: 'Read', call_id: 'c1', queued: true },
      { partId: 'tool-1', messageId: 'assistant-1' },
    )
    store.handlePartUpdated(
      'session-1',
      { tool_completed: 'read', call_id: 'c1', output: 'body' },
      { partId: 'tool-1', messageId: 'assistant-1' },
    )
    store.handlePartUpdated('session-1', { text: 'after' }, { partId: 'text-2', messageId: 'assistant-1' })

    const assistant = store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-1')
    expect(assistant?.parts.map((p) => p.type)).toEqual(['text', 'tool', 'text'])
    expect(assistant?.content).toBe('before after')
  })

  it('starts a subtask card in the per-turn shell while viewing it', () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ status: 'busy' }),
      makeSession({ id: 'child-1', parent_id: 'session-1', agent_name: 'explore' }),
    ]
    store.setActiveSession('child-1')
    store.messagesBySession['session-1'] = [
      makeMessage({ id: 'user-1' }),
      makeMessage({ id: 'assistant-1', role: 'assistant', content: '', parts: [] }),
    ]

    store.handleSubtaskStarted('session-1', {
      subtask_id: 'sub-1',
      agent: 'explore',
      description: 'research',
      part_id: 'subtask-part-1',
      child_session_id: 'child-1',
      message_id: 'assistant-1',
    })

    // The parent turn owns the card even while the child session is viewed.
    const parent = store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-1')
    expect(parent?.parts.some((p) => p.id === 'subtask-part-1')).toBe(true)
    expect(store.activeSessionId).toBe('child-1')
  })

  it('preserves streamed data when a busy fetch races ahead (fetch race)', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy' })]
    store.messagesBySession['session-1'] = [
      makeMessage({ id: 'user-1', content: 'hi', position: 0 }),
      makeMessage({
        id: 'assistant-1',
        role: 'assistant',
        content: 'Hello world streamed',
        parts: [
          { id: 'text-1', session_id: 'session-1', type: 'text', state: 'running', title: '', output: 'Hello world streamed' },
        ],
        position: 1,
      }),
    ]
    partsMock.mockResolvedValueOnce({
      session: makeSession({ status: 'busy' }),
      messages: [
        makeMessage({ id: 'user-1', content: 'hi', position: 0 }),
        makeMessage({
          id: 'assistant-1',
          role: 'assistant',
          content: 'Hello',
          parts: [
            { id: 'text-1', session_id: 'session-1', type: 'text', state: 'running', title: '', output: 'Hello' },
          ],
          position: 1,
        }),
      ],
    })

    await store.fetchParts('session-1', false)

    const assistant = store.messagesBySession['session-1']?.find((m) => m.id === 'assistant-1')
    expect(assistant?.content).toBe('Hello world streamed')
  })

  it('hydrates the server user and assistant ids on follow-up (even for repeated prompts)', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'idle' })]
    store.messagesBySession['session-1'] = [makeMessage({ id: 'user-1', content: 'repeat', position: 0 })]
    sendMock.mockResolvedValueOnce(makeSession({ status: 'busy' }))
    partsMock.mockResolvedValueOnce({
      session: makeSession({ status: 'busy' }),
      messages: [
        makeMessage({ id: 'user-1', content: 'repeat', position: 0 }),
        makeMessage({ id: 'assistant-1', role: 'assistant', position: 1 }),
        makeMessage({ id: 'user-2', content: 'repeat', position: 2 }),
        makeMessage({ id: 'assistant-2', role: 'assistant', position: 3 }),
      ],
    })

    await store.sendMessage('session-1', 'repeat')

    expect(store.messagesBySession['session-1']?.map((m) => m.id)).toEqual([
      'user-1', 'assistant-1', 'user-2', 'assistant-2',
    ])
  })

  it('does not set loading on same-workspace refresh while chat exists', async () => {
    const store = useHarnessStore()
    sessionsMock.mockResolvedValue([makeSession()])
    await store.fetchSessions('ws-1')
    store.setActiveSession('session-1')
    store.messagesBySession['session-1'] = [makeMessage({ id: 'user-1' })]
    expect(store.loading).toBe(false)

    sessionsMock.mockResolvedValue([makeSession()])
    await store.fetchSessions('ws-1')

    expect(store.loading).toBe(false)
    expect(store.messagesBySession['session-1']).toHaveLength(1)
  })

  it('sorts fetched snapshots by position, not content length', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'idle' })]
    partsMock.mockResolvedValueOnce({
      session: makeSession({ status: 'idle' }),
      messages: [
        makeMessage({ id: 'b', content: 'longer content here', position: 1 }),
        makeMessage({ id: 'a', content: 'x', position: 0 }),
      ],
    })

    await store.fetchParts('session-1', false)

    expect(store.messagesBySession['session-1']?.map((m) => m.id)).toEqual(['a', 'b'])
  })
})

describe('idle snapshot reconciliation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    partsMock.mockResolvedValue({ session: makeSession(), messages: [] })
  })

  it('requests a post-idle snapshot after an older busy fetch finishes', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'busy' })]
    let finishBusy: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void = () => {}
    partsMock.mockImplementationOnce(() => new Promise((resolve) => { finishBusy = resolve }))
    const busy = store.fetchParts('session-1')
    store.handleSessionStatus('session-1', 'idle', { message_id: 'answer-id' })
    partsMock.mockResolvedValueOnce({
      session: makeSession({ status: 'idle' }),
      messages: [makeMessage({ id: 'answer-id', role: 'assistant', content: 'final', position: 1 })],
    })
    const settled = store.refreshPartsAfterIdle('session-1')
    expect(partsMock).toHaveBeenCalledTimes(1)
    finishBusy({ session: makeSession({ status: 'busy' }), messages: [] })
    await busy
    await settled
    expect(partsMock).toHaveBeenCalledTimes(2)
    expect(store.messagesBySession['session-1']?.[0]?.content).toBe('final')
  })
})
