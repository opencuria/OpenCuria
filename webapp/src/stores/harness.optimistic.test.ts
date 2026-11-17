import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { createHarnessSession, listHarnessParts, sendHarnessMessage } from '@/services/harness.api'
import { messageRenderKey } from '@opencuria/chat-core/optimisticPrompts'
import type { HarnessMessage, HarnessSession } from '@opencuria/chat-core/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    createHarnessSession: vi.fn(),
    listHarnessParts: vi.fn(),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    sendHarnessMessage: vi.fn(),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

const notifyError = vi.fn()
vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: notifyError, success: vi.fn(), info: vi.fn() }),
}))

const createMock = vi.mocked(createHarnessSession)
const partsMock = vi.mocked(listHarnessParts)
const sendMock = vi.mocked(sendHarnessMessage)

function session(overrides: Partial<HarnessSession> = {}): HarnessSession {
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

function row(id: string, role: 'user' | 'assistant', content: string, position: number) {
  return { id, session_id: 'session-1', role, content, parts: [], position } as HarnessMessage
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

describe('harness store optimistic prompts', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('shows a sent prompt at once and keeps its row when the server copy arrives', async () => {
    const store = useHarnessStore()
    store.sessions = [session()]
    store.messagesBySession['session-1'] = [row('u1', 'user', 'hi', 0)]
    const accepted = deferred<HarnessSession>()
    sendMock.mockReturnValueOnce(accepted.promise)
    partsMock.mockResolvedValueOnce({
      session: session({ status: 'busy' }),
      messages: [
        row('u1', 'user', 'hi', 0),
        row('u2', 'user', 'next step', 1),
        row('a2', 'assistant', '', 2),
      ],
    })

    const sending = store.sendMessage('session-1', 'next step', { skillIds: ['skill-1'] })

    const shown = store.messagesBySession['session-1']!
    expect(shown.map((message) => message.content)).toEqual(['hi', 'next step'])
    const prompt = shown[1]!
    expect(prompt.id).toMatch(/^local-user-/)
    expect(prompt.skill_ids).toEqual(['skill-1'])

    accepted.resolve(session({ status: 'busy' }))
    await expect(sending).resolves.toBe(true)

    const rows = store.messagesBySession['session-1']!
    expect(rows.map((message) => message.id)).toEqual(['u1', 'u2', 'a2'])
    expect(messageRenderKey(rows[1]!)).toBe(prompt.id)
  })

  it('takes a rejected prompt back out of the transcript', async () => {
    const store = useHarnessStore()
    store.sessions = [session()]
    store.messagesBySession['session-1'] = [row('u1', 'user', 'hi', 0)]
    sendMock.mockRejectedValueOnce(new Error('Session is busy'))

    await expect(store.sendMessage('session-1', 'next step')).resolves.toBe(false)

    expect(store.messagesBySession['session-1']?.map((message) => message.id)).toEqual(['u1'])
    expect(notifyError).toHaveBeenCalledWith('Prompt failed', 'Session is busy')
    expect(partsMock).not.toHaveBeenCalled()
  })

  it("shows a new chat's prompt while the chat is created and moves it into the chat", async () => {
    const store = useHarnessStore()
    const created = deferred<HarnessSession>()
    createMock.mockReturnValueOnce(created.promise)
    partsMock.mockResolvedValueOnce({
      session: session({ status: 'busy' }),
      messages: [row('u1', 'user', 'build it', 0), row('a1', 'assistant', '', 1)],
    })

    const creating = store.createSession('ws-1', 'build it', 'build', 'm')

    const pending = store.pendingPrompts['ws-1']!
    expect(pending.content).toBe('build it')
    expect(pending.id).toMatch(/^local-user-/)

    created.resolve(session({ status: 'busy' }))
    await expect(creating).resolves.toMatchObject({ id: 'session-1' })

    expect(store.pendingPrompts['ws-1']).toBeUndefined()
    const rows = store.messagesBySession['session-1']!
    expect(rows.map((message) => message.id)).toEqual(['u1', 'a1'])
    expect(messageRenderKey(rows[0]!)).toBe(pending.id)
  })

  it('clears the pending prompt of a chat that could not be created', async () => {
    const store = useHarnessStore()
    createMock.mockRejectedValueOnce(new Error('No provider'))

    await expect(store.createSession('ws-1', 'build it', 'build', 'm')).resolves.toBeNull()

    expect(store.pendingPrompts['ws-1']).toBeUndefined()
    expect(store.activeSessionId).toBeNull()
  })
})
