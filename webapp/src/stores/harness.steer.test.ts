import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { ApiRequestError } from '@/services/api'
import {
  abortHarnessSession,
  listHarnessParts,
  sendHarnessMessage,
  steerHarnessSession,
  withdrawHarnessSteer,
} from '@/services/harness.api'
import type { HarnessMessage, HarnessSession, HarnessSteer } from '@/types/harness'

const notifyError = vi.hoisted(() => vi.fn())

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessParts: vi.fn(),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    sendHarnessMessage: vi.fn(),
    abortHarnessSession: vi.fn(),
    steerHarnessSession: vi.fn(),
    withdrawHarnessSteer: vi.fn(),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: notifyError, success: vi.fn(), info: vi.fn() }),
}))

const partsMock = vi.mocked(listHarnessParts)
const sendMock = vi.mocked(sendHarnessMessage)
const abortMock = vi.mocked(abortHarnessSession)
const steerMock = vi.mocked(steerHarnessSession)
const withdrawMock = vi.mocked(withdrawHarnessSteer)

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
  return {
    id: 'session-1',
    workspace_id: 'ws-1',
    parent_id: null,
    title: 'Chat',
    mode: 'build',
    agent_name: 'build',
    model: 'm',
    status: 'busy',
    cost: 0,
    tokens: {},
    ...overrides,
  }
}

function makeSteer(id: string, content = `Steer ${id}`): HarnessSteer {
  return { id, content, created_at: '2026-10-16T10:00:00Z' }
}

function makeAssistant(overrides: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'assistant-1',
    session_id: 'session-1',
    role: 'assistant',
    content: '',
    parts: [],
    ...overrides,
  }
}

function ready() {
  const store = useHarnessStore()
  store.sessions = [makeSession()]
  store.setActiveSession('session-1')
  return store
}

describe('harness store steering', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    partsMock.mockResolvedValue({ session: makeSession(), messages: [] })
  })

  describe('queue snapshots', () => {
    it('replaces the queue per snapshot and drops the key when it empties', () => {
      const store = ready()

      store.handleSteerQueue('session-1', [makeSteer('a'), makeSteer('b')])
      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['a', 'b'])

      store.handleSteerQueue('session-1', [makeSteer('b')])
      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['b'])

      store.handleSteerQueue('session-1', [])
      expect(store.activeQueuedSteers).toEqual([])
      expect('session-1' in store.queuedSteers).toBe(false)
    })

    it('is idempotent and keeps queues per session', () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a')])
      store.handleSteerQueue('session-1', [makeSteer('a')])
      store.handleSteerQueue('session-2', [makeSteer('x')])

      expect(store.queuedSteers['session-1']).toHaveLength(1)
      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['a'])
      expect(store.queuedSteers['session-2']).toHaveLength(1)
    })

    it('hydrates the queue from the parts response and tolerates a missing field', async () => {
      const store = ready()
      partsMock.mockResolvedValueOnce({
        session: makeSession(),
        messages: [],
        steers: [makeSteer('a')],
      })
      await store.fetchParts('session-1')
      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['a'])

      partsMock.mockResolvedValueOnce({ session: makeSession(), messages: [] })
      await store.fetchParts('session-1')
      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['a'])

      partsMock.mockResolvedValueOnce({ session: makeSession(), messages: [], steers: [] })
      await store.fetchParts('session-1')
      expect(store.activeQueuedSteers).toEqual([])
    })

    it('keeps a newer socket snapshot that lands while the parts request is in flight', async () => {
      const store = ready()
      let resolveParts: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void = () => {}
      partsMock.mockReturnValueOnce(new Promise((resolve) => (resolveParts = resolve)))

      const flight = store.fetchParts('session-1')
      store.handleSteerQueue('session-1', [makeSteer('fresh')])
      resolveParts({ session: makeSession(), messages: [], steers: [] })
      await flight

      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['fresh'])
    })

    it('forgets the queue together with the transcript on reset', () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a')])
      store.reset()
      expect(store.queuedSteers).toEqual({})
    })
  })

  describe('delivery', () => {
    it('creates a completed steer part from a part_updated delta, once', () => {
      const store = ready()
      const opts = { messageId: 'assistant-1', partId: 'steer-part', partPosition: 3 }
      store.handlePartUpdated(
        'session-1',
        { text: 'Working' },
        { messageId: 'assistant-1', partId: 't1' },
      )

      store.handlePartUpdated('session-1', { steer: 'Use pnpm', steer_id: 's1' }, opts)
      store.handlePartUpdated('session-1', { steer: 'Use pnpm', steer_id: 's1' }, opts)

      const parts = store.messagesBySession['session-1']!.find((m) => m.id === 'assistant-1')!.parts
      expect(parts.filter((part) => part.type === 'steer')).toHaveLength(1)
      expect(parts.find((part) => part.id === 't1')?.state).toBe('completed')
    })
  })

  describe('steerSession', () => {
    it('queues the message and adds it to the tray when the snapshot has not landed yet', async () => {
      const store = ready()
      steerMock.mockResolvedValueOnce(makeSteer('a', 'Use pnpm'))

      await expect(store.steerSession('session-1', 'Use pnpm')).resolves.toBe(true)

      expect(steerMock).toHaveBeenCalledWith('session-1', 'Use pnpm')
      expect(store.activeQueuedSteers).toEqual([makeSteer('a', 'Use pnpm')])
      expect(sendMock).not.toHaveBeenCalled()
    })

    it('does not duplicate an entry the snapshot already delivered', async () => {
      const store = ready()
      steerMock.mockImplementationOnce(async () => {
        store.handleSteerQueue('session-1', [makeSteer('a')])
        return makeSteer('a')
      })

      await expect(store.steerSession('session-1', 'x')).resolves.toBe(true)
      expect(store.activeQueuedSteers).toHaveLength(1)
    })

    it('does not resurrect a steer that was delivered or discarded before the response', async () => {
      const store = ready()
      steerMock.mockImplementationOnce(async () => {
        store.handleSteerQueue('session-1', [makeSteer('a')])
        store.handleSteerQueue('session-1', [])
        return makeSteer('a')
      })
      await store.steerSession('session-1', 'x')
      expect(store.activeQueuedSteers).toEqual([])

      store.messagesBySession['session-1'] = [
        makeAssistant({
          parts: [
            {
              id: 'p',
              session_id: 'session-1',
              type: 'steer',
              state: 'completed',
              title: '',
              output: 'y',
              meta: { steer_id: 'b' },
            },
          ],
        }),
      ]
      steerMock.mockResolvedValueOnce(makeSteer('b', 'y'))
      await store.steerSession('session-1', 'y')
      expect(store.activeQueuedSteers).toEqual([])
    })

    it('falls back to a normal message with the run options when the run just ended', async () => {
      const store = ready()
      steerMock.mockRejectedValueOnce(new ApiRequestError(409, 'No active run', 'no_active_run'))
      sendMock.mockResolvedValueOnce(makeSession({ status: 'busy' }))

      const accepted = await store.steerSession('session-1', 'Use pnpm', {
        mode: 'plan',
        model: 'm2',
        skillIds: ['sk'],
        reasoningEffort: 'high',
      })

      expect(accepted).toBe(true)
      expect(sendMock).toHaveBeenCalledWith('session-1', {
        prompt: 'Use pnpm',
        mode: 'plan',
        model: 'm2',
        skill_ids: ['sk'],
        reasoning_effort: 'high',
      })
      expect(notifyError).not.toHaveBeenCalled()
      expect(store.activeQueuedSteers).toEqual([])
    })

    it('reports a failing fallback so the composer keeps the text', async () => {
      const store = ready()
      steerMock.mockRejectedValueOnce(new ApiRequestError(409, 'No active run', 'no_active_run'))
      sendMock.mockRejectedValueOnce(new Error('boom'))

      await expect(store.steerSession('session-1', 'x')).resolves.toBe(false)
      expect(notifyError).toHaveBeenCalledWith('Prompt failed', 'boom')
    })

    it('notifies and resolves false on other errors, including other conflicts', async () => {
      const store = ready()
      steerMock.mockRejectedValueOnce(new ApiRequestError(403, 'Forbidden', 'authentication_error'))
      await expect(store.steerSession('session-1', 'x')).resolves.toBe(false)
      expect(notifyError).toHaveBeenLastCalledWith('Steer failed', 'Forbidden')

      steerMock.mockRejectedValueOnce(new ApiRequestError(409, 'Busy elsewhere', 'conflict'))
      await expect(store.steerSession('session-1', 'x')).resolves.toBe(false)
      expect(sendMock).not.toHaveBeenCalled()
      expect(store.activeQueuedSteers).toEqual([])
    })
  })

  describe('withdrawSteer', () => {
    it('removes the entry and returns its text for the composer', async () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a', 'first'), makeSteer('b', 'second')])
      withdrawMock.mockResolvedValueOnce(makeSteer('a', 'first'))

      await expect(store.withdrawSteer('session-1', 'a')).resolves.toBe('first')

      expect(withdrawMock).toHaveBeenCalledWith('session-1', 'a')
      expect(store.activeQueuedSteers.map((steer) => steer.id)).toEqual(['b'])
    })

    it('treats an already delivered steer quietly: drops it and returns nothing', async () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a')])
      withdrawMock.mockRejectedValueOnce(new ApiRequestError(404, 'gone', 'steer_not_found'))

      await expect(store.withdrawSteer('session-1', 'a')).resolves.toBeNull()

      expect(store.activeQueuedSteers).toEqual([])
      expect(notifyError).not.toHaveBeenCalled()
    })

    it('notifies on other failures and keeps the queue', async () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a')])
      withdrawMock.mockRejectedValueOnce(new Error('offline'))

      await expect(store.withdrawSteer('session-1', 'a')).resolves.toBeNull()

      expect(notifyError).toHaveBeenCalledWith('Withdraw failed', 'offline')
      expect(store.activeQueuedSteers).toHaveLength(1)
    })
  })

  describe('abortSession', () => {
    it('reports a pending stop immediately and sends only one request per session', async () => {
      const store = ready()
      let finish!: (session: HarnessSession) => void
      abortMock.mockReturnValueOnce(
        new Promise((resolve) => {
          finish = resolve
        }),
      )

      const stopping = store.abortSession('session-1')
      expect(store.stoppingSessionIds).toEqual(['session-1'])
      await expect(store.abortSession('session-1')).resolves.toEqual([])
      expect(abortMock).toHaveBeenCalledTimes(1)
      expect(store.sessions[0]?.status).toBe('busy')

      finish(makeSession({ status: 'idle' }))
      await stopping
      expect(store.stoppingSessionIds).toEqual([])
    })

    it('hands the queued texts back after a successful abort and clears the queue', async () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a', 'first'), makeSteer('b', 'second')])
      abortMock.mockResolvedValueOnce(makeSession({ status: 'idle' }))

      await expect(store.abortSession('session-1')).resolves.toEqual(['first', 'second'])

      expect(store.activeQueuedSteers).toEqual([])
      expect(store.sessions[0]?.status).toBe('idle')
    })

    it('does not hand back a steer that was delivered while the abort was in flight', async () => {
      const store = ready()
      store.messagesBySession['session-1'] = [makeAssistant()]
      store.handleSteerQueue('session-1', [makeSteer('a', 'first'), makeSteer('b', 'second')])
      abortMock.mockImplementationOnce(async () => {
        store.handlePartUpdated(
          'session-1',
          { steer: 'first', steer_id: 'a' },
          { messageId: 'assistant-1', partId: 'steer-part' },
        )
        store.handleSteerQueue('session-1', [])
        return makeSession({ status: 'idle' })
      })

      await expect(store.abortSession('session-1')).resolves.toEqual(['second'])
    })

    it('keeps the queue and returns nothing when the abort fails', async () => {
      const store = ready()
      store.handleSteerQueue('session-1', [makeSteer('a')])
      abortMock.mockRejectedValueOnce(new Error('nope'))

      await expect(store.abortSession('session-1')).resolves.toEqual([])

      expect(notifyError).toHaveBeenCalledWith('Abort failed', 'nope')
      expect(store.activeQueuedSteers).toHaveLength(1)
      expect(store.stoppingSessionIds).toEqual([])

      abortMock.mockResolvedValueOnce(makeSession({ status: 'idle' }))
      await expect(store.abortSession('session-1')).resolves.toEqual(['Steer a'])
      expect(abortMock).toHaveBeenCalledTimes(2)
    })
  })

  describe('ensureUserTurnLoaded', () => {
    it('loads the transcript when a server-opened run brings an unknown user message', async () => {
      const store = ready()
      store.messagesBySession['session-1'] = [makeAssistant()]

      await store.ensureUserTurnLoaded('session-1', 'user-2')

      expect(partsMock).toHaveBeenCalledTimes(1)
    })

    it('skips known messages, unknown sessions and runs this client started itself', async () => {
      const store = ready()
      store.messagesBySession['session-1'] = [
        makeAssistant({ id: 'user-2', role: 'user', content: 'x' }),
      ]
      await store.ensureUserTurnLoaded('session-1', 'user-2')
      await store.ensureUserTurnLoaded('session-unknown', 'user-9')
      expect(partsMock).not.toHaveBeenCalled()

      let resolveSend: (session: HarnessSession) => void = () => {}
      sendMock.mockReturnValueOnce(new Promise((resolve) => (resolveSend = resolve)))
      const sending = store.sendMessage('session-1', 'hello')
      await store.ensureUserTurnLoaded('session-1', 'user-3')
      expect(partsMock).not.toHaveBeenCalled()
      resolveSend(makeSession())
      await sending
      expect(partsMock).toHaveBeenCalledTimes(1)
    })
  })

  describe('sendMessage', () => {
    it('resolves true on success and false on failure', async () => {
      const store = ready()
      sendMock.mockResolvedValueOnce(makeSession())
      await expect(store.sendMessage('session-1', 'ok')).resolves.toBe(true)

      sendMock.mockRejectedValueOnce(new Error('bad'))
      await expect(store.sendMessage('session-1', 'bad')).resolves.toBe(false)
      expect(notifyError).toHaveBeenCalledWith('Prompt failed', 'bad')
    })
  })
})
