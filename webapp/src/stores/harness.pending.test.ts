import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { useAuthStore } from '@/stores/auth'
import { listHarnessParts, resolveHarnessQuestion } from '@/services/harness.api'
import {
  loadQuestionInputCache,
  questionInputCacheKey,
  saveQuestionInputCache,
} from '@/lib/questionInputCache'
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
    resolveHarnessQuestion: vi.fn().mockResolvedValue({ request_id: 'q-1', status: 'answered' }),
  }
})

const listPartsMock = vi.mocked(listHarnessParts)
const resolveQuestionMock = vi.mocked(resolveHarnessQuestion)
const draft = { answers: ['blue'], custom: ['manual'] }
const user = { id: 17, email: 'user@example.com', first_name: 'Test', last_name: 'User' }

function questionKey(overrides: Partial<HarnessQuestionRequest> = {}): string {
  return questionInputCacheKey(
    { userId: user.id, organizationId: 'org-1' },
    { ...makeQuestion(), ...overrides },
  )
}

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

function makeQuestion(overrides: Partial<HarnessQuestionRequest> = {}): HarnessQuestionRequest {
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
    window.sessionStorage.clear()
    vi.clearAllMocks()
    listPartsMock.mockResolvedValue({ session: makeSession(), messages: [] })
    resolveQuestionMock.mockResolvedValue({ request_id: 'q-1', status: 'answered' })
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

  it('settles descendant gates the parent timeline no longer lists', async () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ status: 'idle' }),
      makeSession({ id: 'child-1', parent_id: 'session-1', status: 'idle' }),
      makeSession({ id: 'session-2', parent_id: null }),
    ]
    store.setActiveSession('session-1')
    store.handlePermissionRequired(
      makePermission({ request_id: 'child-perm', session_id: 'child-1' }),
    )
    store.handleQuestionRequired(makeQuestion({ request_id: 'child-q', session_id: 'child-1' }))
    store.handlePermissionRequired(
      makePermission({ request_id: 'other-perm', session_id: 'session-2' }),
    )
    const childDraftKey = questionKey({ request_id: 'child-q', session_id: 'child-1' })
    saveQuestionInputCache(childDraftKey, draft)
    // Stopped elsewhere while the socket was down: the root's authoritative
    // timeline (which covers descendants) no longer lists the subagent gates.
    listPartsMock.mockResolvedValueOnce({
      session: makeSession({ status: 'idle' }),
      messages: [],
      permissions: [],
      questions: [],
    })

    await store.fetchParts('session-1')

    expect(store.activePermissionRequests).toEqual([])
    expect(store.activeQuestionRequests).toEqual([])
    expect(loadQuestionInputCache(childDraftKey, 1)).toBeNull()
    expect(store.pendingPermissions['other-perm']?.session_id).toBe('session-2')
  })

  it('keeps a descendant gate that arrives while the timeline request is in flight', async () => {
    const store = useHarnessStore()
    store.sessions = [makeSession(), makeSession({ id: 'child-1', parent_id: 'session-1' })]
    store.setActiveSession('session-1')
    let respond!: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void
    listPartsMock.mockReturnValueOnce(new Promise((resolve) => (respond = resolve)))

    const fetching = store.fetchParts('session-1', false)
    store.handlePermissionRequired(
      makePermission({ request_id: 'fresh-perm', session_id: 'child-1' }),
    )
    respond({ session: makeSession(), messages: [], permissions: [], questions: [] })
    await fetching

    expect(store.activePermissionRequests.map((row) => row.request_id)).toEqual(['fresh-perm'])
  })

  it('surfaces descendant permission and question gates on the parent session', () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession(),
      makeSession({
        id: 'child-1',
        parent_id: 'session-1',
        agent_name: 'explore',
        title: 'Explore backend',
      }),
      makeSession({ id: 'session-2', parent_id: null, title: 'Other' }),
    ]
    store.setActiveSession('session-1')
    store.handlePermissionRequired(
      makePermission({
        request_id: 'child-perm',
        session_id: 'child-1',
        agent_name: 'explore',
      }),
    )
    store.handleQuestionRequired(
      makeQuestion({ request_id: 'child-q', session_id: 'child-1', agent_name: 'explore' }),
    )
    store.handlePermissionRequired(
      makePermission({ request_id: 'sibling-perm', session_id: 'session-2' }),
    )

    expect(store.activePermissionRequests.map((row) => row.request_id)).toEqual(['child-perm'])
    expect(store.activePermissionRequests[0]?.agent_name).toBe('explore')
    expect(store.activeQuestionRequests.map((row) => row.request_id)).toEqual(['child-q'])
    expect(store.activeQuestionRequests[0]?.agent_name).toBe('explore')
  })

  it('fills agent_name from the child session when the gate omits it', () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession(),
      makeSession({ id: 'child-1', parent_id: 'session-1', agent_name: 'explore' }),
    ]
    store.setActiveSession('session-1')
    store.handlePermissionRequired(
      makePermission({ request_id: 'child-perm', session_id: 'child-1' }),
    )

    expect(store.activePermissionRequests[0]?.agent_name).toBe('explore')
  })

  it.each([
    ['submit', false, ['blue']],
    ['skip', true, []],
  ])('clears a draft after successful question %s', async (_action, reject, answers) => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
    const store = useHarnessStore()
    const question = makeQuestion()
    const cacheKey = questionKey()
    store.handleQuestionRequired(question)
    saveQuestionInputCache(cacheKey, draft)

    await store.resolveQuestion('session-1', 'q-1', answers, reject)

    expect(resolveQuestionMock).toHaveBeenCalledWith('session-1', 'q-1', answers, reject)
    expect(loadQuestionInputCache(cacheKey, 1)).toBeNull()
    expect(store.pendingQuestions['q-1']).toBeUndefined()
  })

  it('keeps a draft when question resolution fails', async () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
    const store = useHarnessStore()
    store.handleQuestionRequired(makeQuestion())
    const cacheKey = questionKey()
    saveQuestionInputCache(cacheKey, draft)
    resolveQuestionMock.mockRejectedValueOnce(new Error('network error'))

    await store.resolveQuestion('session-1', 'q-1', ['blue'])

    expect(loadQuestionInputCache(cacheKey, 1)).toEqual(draft)
    expect(store.pendingQuestions['q-1']?.status).toBe('pending')
  })

  it('clears a draft when an external resolution arrives', () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
    const store = useHarnessStore()
    store.handleQuestionRequired(makeQuestion())
    const cacheKey = questionKey()
    saveQuestionInputCache(cacheKey, draft)

    store.handleQuestionResolved('q-1', 'rejected')

    expect(loadQuestionInputCache(cacheKey, 1)).toBeNull()
  })

  it('prunes persisted drafts from an authoritative parts response after reload', async () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
    const store = useHarnessStore()
    const pendingKey = questionKey({ request_id: 'pending' })
    const staleKey = questionKey({ request_id: 'stale' })
    const otherSessionKey = questionInputCacheKey(
      { userId: user.id, organizationId: 'org-1' },
      { workspace_id: 'ws-1', session_id: 'session-2', request_id: 'other-session' },
    )
    for (const cacheKey of [pendingKey, staleKey, otherSessionKey]) {
      saveQuestionInputCache(cacheKey, draft)
    }
    listPartsMock.mockResolvedValueOnce({
      session: makeSession(),
      messages: [],
      questions: [makeQuestion({ request_id: 'pending' })],
    })

    await store.fetchParts('session-1', false)

    expect(loadQuestionInputCache(pendingKey, 1)).toEqual(draft)
    expect(loadQuestionInputCache(staleKey, 1)).toBeNull()
    expect(loadQuestionInputCache(otherSessionKey, 1)).toEqual(draft)
  })

  it('does not prune drafts when fetching parts fails', async () => {
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
    const store = useHarnessStore()
    const cacheKey = questionKey({ request_id: 'persisted' })
    saveQuestionInputCache(cacheKey, draft)
    listPartsMock.mockRejectedValueOnce(new Error('offline'))

    await store.fetchParts('session-1', false)

    expect(loadQuestionInputCache(cacheKey, 1)).toEqual(draft)
  })
})

describe('question cache async navigation', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
    const auth = useAuthStore()
    auth.user = user
    auth.activeOrganizationId = 'org-1'
  })

  it('removes a rehydrated pending request after successful submit', async () => {
    const store = useHarnessStore()
    const request = makeQuestion()
    store.handleQuestionRequired(request)
    let finish!: (outcome: { request_id: string; status: string }) => void
    resolveQuestionMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve
        }),
    )
    const flight = store.resolveQuestion(request.session_id, request.request_id, ['blue'])
    store.handleQuestionRequired({ ...request })
    finish({ request_id: request.request_id, status: 'answered' })
    await flight
    expect(store.pendingQuestions[request.request_id]).toBeUndefined()
  })

  it.each(['organization', 'user', 'workspace'])(
    'clears the original draft when %s changes during submit',
    async (change) => {
      const auth = useAuthStore()
      const store = useHarnessStore()
      const request = makeQuestion()
      store.handleQuestionRequired(request)
      const originalKey = questionKey()
      saveQuestionInputCache(originalKey, draft)
      let finish!: (outcome: { request_id: string; status: string }) => void
      resolveQuestionMock.mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve
          }),
      )
      const flight = store.resolveQuestion(request.session_id, request.request_id, ['blue'])
      store.reset()
      if (change === 'organization') auth.activeOrganizationId = 'org-2'
      else if (change === 'user') auth.user = { ...user, id: user.id + 1 }
      const other =
        change === 'workspace'
          ? makeQuestion({ workspace_id: 'ws-2', session_id: 'session-2' })
          : makeQuestion()
      const otherKey = questionInputCacheKey(
        { userId: auth.user?.id, organizationId: auth.activeOrganizationId },
        other,
      )
      store.handleQuestionRequired(other)
      saveQuestionInputCache(otherKey, draft)
      finish({ request_id: request.request_id, status: 'answered' })
      await flight

      expect(loadQuestionInputCache(originalKey, 1)).toBeNull()
      expect(loadQuestionInputCache(otherKey, 1)).toEqual(draft)
      expect(store.pendingQuestions[other.request_id]).toBeDefined()
    },
  )
})
