import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import type { HarnessSession } from '@/types/harness'

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

describe('harness store session lineage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('returns an empty lineage when no session is active', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession()]
    expect(store.activeSessionLineage).toEqual([])
  })

  it('returns a single session for a root', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ id: 'root', title: 'Root chat' })]
    store.setActiveSession('root')
    expect(store.activeSessionLineage.map((session) => session.id)).toEqual(['root'])
  })

  it('returns root then child for a subagent session', () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'root', title: 'Root chat' }),
      makeSession({
        id: 'child',
        parent_id: 'root',
        title: 'Explore auth',
        agent_name: 'explore',
      }),
    ]
    store.setActiveSession('child')
    expect(store.activeSessionLineage.map((session) => session.id)).toEqual(['root', 'child'])
  })

  it('returns the full grandchild chain', () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'root' }),
      makeSession({ id: 'child', parent_id: 'root' }),
      makeSession({ id: 'grand', parent_id: 'child', agent_name: 'general' }),
    ]
    store.setActiveSession('grand')
    expect(store.activeSessionLineage.map((session) => session.id)).toEqual([
      'root',
      'child',
      'grand',
    ])
  })

  it('is cycle-safe', () => {
    const store = useHarnessStore()
    store.sessions = [
      makeSession({ id: 'a', parent_id: 'b' }),
      makeSession({ id: 'b', parent_id: 'a' }),
    ]
    store.setActiveSession('a')
    expect(store.activeSessionLineage.map((session) => session.id)).toEqual(['b', 'a'])
  })

  it('stops when a parent is missing from sessions', () => {
    const store = useHarnessStore()
    store.sessions = [makeSession({ id: 'child', parent_id: 'missing', title: 'Orphan' })]
    store.setActiveSession('child')
    expect(store.activeSessionLineage.map((session) => session.id)).toEqual(['child'])
    expect(store.activeSessionLineage[0]?.parent_id).toBe('missing')
  })
})
