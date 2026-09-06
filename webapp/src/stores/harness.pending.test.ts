import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { listHarnessParts } from '@/services/harness.api'
import type {
  HarnessPermissionRequest,
  HarnessQuestionRequest,
  HarnessSession,
} from '@/types/harness'

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessParts: vi.fn().mockResolvedValue({ session: {}, messages: [] }),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

const listPartsMock = vi.mocked(listHarnessParts)

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
    status: 'busy',
    unread: false,
    cost: 0,
    tokens: {},
    ...overrides,
  }
}

function makePermission(
  overrides: Partial<HarnessPermissionRequest> = {},
): HarnessPermissionRequest {
  return {
    request_id: 'perm-1',
    session_id: 'session-1',
    workspace_id: 'ws-1',
    tool: 'bash',
    pattern: 'reboot',
    title: '$ reboot',
    status: 'pending',
    ...overrides,
  }
}

function makeQuestion(
  overrides: Partial<HarnessQuestionRequest> = {},
): HarnessQuestionRequest {
  return {
    request_id: 'q-1',
    session_id: 'session-1',
    workspace_id: 'ws-1',
    questions: [{ question: 'Which color?' }],
    status: 'pending',
    ...overrides,
  }
}

describe('harness store pending gate hydration', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    listPartsMock.mockResolvedValue({ session: makeSession(), messages: [] })
  })

  it('hydrates pending permissions and questions from fetchParts', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-1')
    listPartsMock.mockResolvedValueOnce({
      session: makeSession(),
      messages: [],
      permissions: [makePermission()],
      questions: [makeQuestion()],
    })

    await store.fetchParts('session-1', false)

    expect(store.activePermissionRequests.map((row) => row.request_id)).toEqual(['perm-1'])
    expect(store.activeQuestionRequests.map((row) => row.request_id)).toEqual(['q-1'])
  })

  it('replaces stale pending gates for the fetched session only', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    store.setActiveSession('session-1')
    store.handlePermissionRequired(makePermission({ request_id: 'stale-perm' }))
    store.handleQuestionRequired(makeQuestion({ request_id: 'stale-q' }))
    store.handlePermissionRequired(
      makePermission({ request_id: 'other-perm', session_id: 'session-2' }),
    )
    listPartsMock.mockResolvedValueOnce({
      session: makeSession(),
      messages: [],
      permissions: [makePermission()],
      questions: [makeQuestion()],
    })

    await store.fetchParts('session-1', false)

    expect(store.pendingPermissions['stale-perm']).toBeUndefined()
    expect(store.pendingQuestions['stale-q']).toBeUndefined()
    expect(store.pendingPermissions['perm-1']?.title).toBe('$ reboot')
    expect(store.pendingQuestions['q-1']?.questions[0]?.question).toBe('Which color?')
    expect(store.pendingPermissions['other-perm']?.session_id).toBe('session-2')
  })
})
