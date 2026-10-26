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

  it('records the resolved Codex model and effort after creating a chat with defaults', async () => {
    createMock.mockResolvedValue(
      makeSession({
        harness_id: 'codex',
        model: 'chatgpt/gpt-6.1-sol',
        reasoning_effort: 'high',
      }),
    )
    const store = useHarnessStore()
    await store.createSession('ws-1', 'hello', 'build', '', [], '', 'codex')
    expect(saveMock).toHaveBeenCalledWith('chatgpt/gpt-6.1-sol', 'high')
  })

  it('remembers Codex effort on follow-ups even when no overrides are sent', async () => {
    sendMock.mockResolvedValue(
      makeSession({
        harness_id: 'codex',
        model: 'chatgpt/gpt-6.1-sol',
        reasoning_effort: 'ultra',
      }),
    )
    const store = useHarnessStore()
    await store.sendMessage('session-1', 'follow up', {})
    expect(saveMock).toHaveBeenCalledWith('chatgpt/gpt-6.1-sol', 'ultra')
  })

  it('persists Claude selection and does not write its alias into native model recents', async () => {
    createMock.mockResolvedValue(makeSession({ harness_id: 'claude', model: 'sonnet' }))
    const store = useHarnessStore()
    const session = await store.createSession(
      'ws-1',
      'hello',
      'build',
      'sonnet',
      [],
      'high',
      'claude',
    )
    expect(session?.harness_id).toBe('claude')
    expect(createMock).toHaveBeenCalledWith('ws-1', {
      prompt: 'hello',
      mode: 'build',
      model: 'sonnet',
      agent_name: 'build',
      skill_ids: [],
      reasoning_effort: 'high',
      harness_id: 'claude',
    })
    expect(saveMock).not.toHaveBeenCalled()
  })

  it('does not write Claude model ids into native recents on follow-up sends', async () => {
    sendMock.mockResolvedValue(makeSession({ harness_id: 'claude', model: 'sonnet' }))
    const store = useHarnessStore()
    await store.sendMessage('session-1', 'follow up', { model: 'sonnet' })
    expect(saveMock).not.toHaveBeenCalled()
  })

  it('records nothing when createSession fails', async () => {
    createMock.mockRejectedValue(new Error('down'))
    const store = useHarnessStore()
    const session = await store.createSession('ws-1', 'hello', 'build', 'acme/think')
    expect(session).toBeNull()
    expect(saveMock).not.toHaveBeenCalled()
  })
})
