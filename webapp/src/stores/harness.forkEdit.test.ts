import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { useNotificationStore } from '@/stores/notifications'
import { useHarnessConversationStore } from '@/stores/harnessConversations'
import { ApiRequestError } from '@/services/api'
import { editHarnessMessage, forkHarnessSession, listHarnessParts } from '@/services/harness.api'
import type { HarnessMessage, HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    forkHarnessSession: vi.fn(),
    editHarnessMessage: vi.fn(),
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

vi.mock('@/stores/harnessConversations', async () => {
  const actual = await vi.importActual<typeof import('@/stores/harnessConversations')>(
    '@/stores/harnessConversations',
  )
  return {
    ...actual,
    useHarnessConversationStore: () => ({
      fetchConversations: vi.fn().mockResolvedValue(undefined),
    }),
  }
})

const forkMock = vi.mocked(forkHarnessSession)
const editMock = vi.mocked(editHarnessMessage)
const partsMock = vi.mocked(listHarnessParts)

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
    status: 'idle',
    unread: false,
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
    content: 'original prompt',
    parts: [],
    ...overrides,
  }
}

describe('harness store fork/edit', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    partsMock.mockResolvedValue({ session: makeSession(), messages: [] })
  })

  it('forkSession adds the session, activates it, and returns the prefill', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.messagesBySession['session-1'] = [makeMessage()]
    const forked = makeSession({ id: 'session-fork', title: 'Chat (fork #1)' })
    forkMock.mockResolvedValueOnce(forked)

    const result = await store.forkSession('session-1', 'msg-1')

    expect(forkMock).toHaveBeenCalledWith('session-1', { message_id: 'msg-1' })
    expect(result).toEqual({ session: forked, prefill: 'original prompt' })
    expect(store.sessions.map((row) => row.id)).toContain('session-fork')
    expect(store.activeSessionId).toBe('session-fork')
    expect(partsMock).toHaveBeenCalledWith('session-fork')
    // No auto-send: sendMessage path is untouched.
    expect(partsMock).toHaveBeenCalledTimes(1)
  })

  it('forkSession without a message id forks the full session', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    const forked = makeSession({ id: 'session-fork' })
    forkMock.mockResolvedValueOnce(forked)

    const result = await store.forkSession('session-1')

    expect(forkMock).toHaveBeenCalledWith('session-1', {})
    expect(result?.prefill).toBe('')
    expect(store.activeSessionId).toBe('session-fork')
  })

  it('forkSession notifies on failure and keeps the active session', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-1')
    forkMock.mockRejectedValueOnce(new Error('offline'))
    const notifications = useNotificationStore()
    const spy = vi.spyOn(notifications, 'error')

    const result = await store.forkSession('session-1', 'msg-1')

    expect(result).toBeNull()
    expect(spy).toHaveBeenCalledWith('Fork failed', 'offline')
    expect(store.activeSessionId).toBe('session-1')
  })

  it('editMessage upserts the session and refetches parts', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ status: 'idle' })]
    const updated = makeSession({ status: 'busy' })
    editMock.mockResolvedValueOnce(updated)

    await store.editMessage('session-1', 'msg-1', 'edited prompt')

    expect(editMock).toHaveBeenCalledWith('session-1', 'msg-1', {
      prompt: 'edited prompt',
      mode: undefined,
      model: undefined,
      skill_ids: undefined,
      reasoning_effort: undefined,
    })
    expect(store.sessions[0]?.status).toBe('busy')
    expect(partsMock).toHaveBeenCalledWith('session-1')
  })

  it('editMessage forwards mode/model/effort overrides', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    editMock.mockResolvedValueOnce(makeSession())

    await store.editMessage('session-1', 'msg-1', 'edited', {
      mode: 'plan',
      model: 'acme/other',
      reasoningEffort: 'low',
    })

    expect(editMock).toHaveBeenCalledWith('session-1', 'msg-1', {
      prompt: 'edited',
      mode: 'plan',
      model: 'acme/other',
      skill_ids: undefined,
      reasoning_effort: 'low',
    })
  })

  it('editMessage maps 409 to an Agent-is-running notification', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    editMock.mockRejectedValueOnce(new ApiRequestError(409, 'active run', 'conflict'))
    const notifications = useNotificationStore()
    const spy = vi.spyOn(notifications, 'error')

    await store.editMessage('session-1', 'msg-1', 'edited')

    expect(spy).toHaveBeenCalledWith(
      'Agent is running — stop it before editing this message.',
      'active run',
    )
    expect(partsMock).not.toHaveBeenCalled()
  })

  it('editMessage maps other failures to a generic Edit-failed notification', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    editMock.mockRejectedValueOnce(new Error('offline'))
    const notifications = useNotificationStore()
    const spy = vi.spyOn(notifications, 'error')

    await store.editMessage('session-1', 'msg-1', 'edited')

    expect(spy).toHaveBeenCalledWith('Edit failed', 'offline')
  })

  it('keeps the conversations feed usable after a fork', async () => {
    const store = useHarnessStore()
    const conversations = useHarnessConversationStore()
    void conversations
    store.sessions = [makeSession()]
    forkMock.mockResolvedValueOnce(makeSession({ id: 'session-fork' }))

    await store.forkSession('session-1', 'msg-1')

    expect(forkMock).toHaveBeenCalled()
    expect(store.sessions.map((row) => row.id)).toContain('session-fork')
  })
})
