/**
 * Save-on-send: successful create/send records model+effort usage
 * (fire-and-forget); failures record nothing.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { createHarnessSession, saveRecentModel, sendHarnessMessage } from '@/services/harness.api'
import type { HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    createHarnessSession: vi.fn(),
    sendHarnessMessage: vi.fn(),
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
    saveRecentModel: vi.fn(),
  }
})

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }),
}))

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

const createMock = vi.mocked(createHarnessSession)
const sendMock = vi.mocked(sendHarnessMessage)
const saveMock = vi.mocked(saveRecentModel)
saveMock.mockResolvedValue({ model: 'x', effort: '', last_used_at: '' })

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

describe('harness store recent-models save-on-send', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('records usage after createSession succeeds', async () => {
    createMock.mockResolvedValue(makeSession())
    const store = useHarnessStore()
    const session = await store.createSession('ws-1', 'hello', 'build', 'acme/think', [], 'high')
    expect(session?.id).toBe('session-1')
    expect(saveMock).toHaveBeenCalledWith('acme/think', 'high')
  })

  it('records usage after sendMessage succeeds', async () => {
    sendMock.mockResolvedValue(makeSession())
    const store = useHarnessStore()
    await store.sendMessage('session-1', 'follow up', {
      model: 'acme/other',
      reasoningEffort: 'low',
    })
    expect(saveMock).toHaveBeenCalledWith('acme/other', 'low')
  })

  it('records nothing when createSession fails', async () => {
    createMock.mockRejectedValue(new Error('down'))
    const store = useHarnessStore()
    const session = await store.createSession('ws-1', 'hello', 'build', 'acme/think')
    expect(session).toBeNull()
    expect(saveMock).not.toHaveBeenCalled()
  })
})
