import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { dismissHarnessNotice } from '@/services/harness.api'
import type { HarnessSession } from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    dismissHarnessNotice: vi.fn().mockResolvedValue(undefined),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
  }
})

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }),
}))

const dismissMock = vi.mocked(dismissHarnessNotice)

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

describe('harness store notice dismiss', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('dismisses optimistically and persists via API', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-1')
    store.messagesBySession['session-1'] = [
      {
        id: 'msg-1',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        finish: 'aborted',
        error: 'aborted by user',
        parts: [],
      },
    ]

    await store.dismissNotice('msg-1')

    expect(store.dismissedNoticeIds['msg-1']).toBe(true)
    expect(store.messagesBySession['session-1']?.[0]?.notice_dismissed_at).toBeTruthy()
    expect(dismissMock).toHaveBeenCalledWith('session-1', 'msg-1')
  })

  it('rolls back the optimistic dismiss when the API fails', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-1')
    store.messagesBySession['session-1'] = [
      {
        id: 'msg-1',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        finish: 'error',
        error: 'boom',
        parts: [],
      },
    ]
    dismissMock.mockRejectedValueOnce(new Error('offline'))

    await store.dismissNotice('msg-1')

    expect(store.dismissedNoticeIds['msg-1']).toBeUndefined()
    expect(
      store.messagesBySession['session-1']?.[0]?.notice_dismissed_at,
    ).toBeNull()
  })
})
