import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

import { useHarnessStore } from '@/stores/harness'
import { ApiRequestError } from '@/services/api'
import {
  listHarnessParts,
  rescheduleHarnessMessage,
  scheduleHarnessMessage,
  sendHarnessScheduledMessage,
  withdrawHarnessScheduledMessage,
} from '@/services/harness.api'
import type { HarnessScheduledMessage, HarnessSession } from '@opencuria/chat-core/harness'

const notifyError = vi.hoisted(() => vi.fn())

vi.mock('@/services/harness.api', async () => {
  const actual =
    await vi.importActual<typeof import('@/services/harness.api')>('@/services/harness.api')
  return {
    ...actual,
    listHarnessParts: vi.fn(),
    listHarnessTodos: vi.fn().mockResolvedValue([]),
    listHarnessSessions: vi.fn().mockResolvedValue([]),
    scheduleHarnessMessage: vi.fn(),
    rescheduleHarnessMessage: vi.fn(),
    withdrawHarnessScheduledMessage: vi.fn(),
    sendHarnessScheduledMessage: vi.fn(),
    markHarnessSessionRead: vi.fn().mockResolvedValue(undefined),
  }
})

vi.mock('@/stores/notifications', () => ({
  useNotificationStore: () => ({ error: notifyError, success: vi.fn(), info: vi.fn() }),
}))

const partsMock = vi.mocked(listHarnessParts)
const scheduleMock = vi.mocked(scheduleHarnessMessage)
const rescheduleMock = vi.mocked(rescheduleHarnessMessage)
const withdrawMock = vi.mocked(withdrawHarnessScheduledMessage)
const sendNowMock = vi.mocked(sendHarnessScheduledMessage)

const SEND_AT = new Date('2026-11-17T08:00:00Z')

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

function makeScheduled(
  id: string,
  overrides: Partial<HarnessScheduledMessage> = {},
): HarnessScheduledMessage {
  return {
    id,
    session_id: 'session-1',
    content: `Later ${id}`,
    send_at: '2026-11-17T08:00:00+00:00',
    status: 'pending',
    error: '',
    skill_ids: [],
    created_at: '2026-11-16T10:00:00+00:00',
    ...overrides,
  }
}

function ready() {
  const store = useHarnessStore()
  store.sessions = [makeSession()]
  store.setActiveSession('session-1')
  return store
}

describe('harness store scheduled messages', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    partsMock.mockResolvedValue({ session: makeSession(), messages: [] })
  })

  describe('snapshots', () => {
    it('replaces the list per snapshot, keeps lists per session and drops empty ones', () => {
      const store = ready()

      store.handleScheduledMessages('session-1', [makeScheduled('a'), makeScheduled('b')])
      store.handleScheduledMessages('session-2', [makeScheduled('x')])
      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['a', 'b'])
      expect(store.scheduledMessages['session-2']).toHaveLength(1)

      store.handleScheduledMessages('session-1', [makeScheduled('b', { status: 'sending' })])
      expect(store.activeScheduledMessages).toEqual([makeScheduled('b', { status: 'sending' })])

      store.handleScheduledMessages('session-1', [])
      expect(store.activeScheduledMessages).toEqual([])
      expect('session-1' in store.scheduledMessages).toBe(false)
    })

    it('hydrates from the timeline and tolerates a missing field', async () => {
      const store = ready()
      partsMock.mockResolvedValueOnce({
        session: makeSession(),
        messages: [],
        scheduled_messages: [makeScheduled('a')],
      })
      await store.fetchParts('session-1')
      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['a'])

      partsMock.mockResolvedValueOnce({ session: makeSession(), messages: [] })
      await store.fetchParts('session-1')
      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['a'])

      partsMock.mockResolvedValueOnce({
        session: makeSession(),
        messages: [],
        scheduled_messages: [],
      })
      await store.fetchParts('session-1')
      expect(store.activeScheduledMessages).toEqual([])
    })

    it('keeps a newer socket snapshot that lands while the timeline request is in flight', async () => {
      const store = ready()
      let resolveParts: (value: Awaited<ReturnType<typeof listHarnessParts>>) => void = () => {}
      partsMock.mockReturnValueOnce(new Promise((resolve) => (resolveParts = resolve)))

      const flight = store.fetchParts('session-1')
      store.handleScheduledMessages('session-1', [makeScheduled('fresh')])
      resolveParts({
        session: makeSession(),
        messages: [],
        scheduled_messages: [makeScheduled('stale')],
      })
      await flight

      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['fresh'])
    })

    it('forgets the lists together with the transcripts on reset', () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a')])
      store.reset()
      expect(store.scheduledMessages).toEqual({})
    })
  })

  describe('scheduleMessage', () => {
    it('schedules with the run options and adds the message when no snapshot landed yet', async () => {
      const store = ready()
      scheduleMock.mockResolvedValueOnce(makeScheduled('a', { content: 'Use pnpm' }))

      const accepted = await store.scheduleMessage('session-1', 'Use pnpm', SEND_AT, {
        mode: 'plan',
        model: 'm2',
        skillIds: ['sk'],
        reasoningEffort: 'high',
      })

      expect(accepted).toBe(true)
      expect(scheduleMock).toHaveBeenCalledWith('session-1', {
        prompt: 'Use pnpm',
        send_at: '2026-11-17T08:00:00.000Z',
        mode: 'plan',
        model: 'm2',
        reasoning_effort: 'high',
        skill_ids: ['sk'],
      })
      expect(store.activeScheduledMessages).toEqual([makeScheduled('a', { content: 'Use pnpm' })])
    })

    it('neither duplicates nor resurrects a message a newer snapshot already covered', async () => {
      const store = ready()
      scheduleMock.mockImplementationOnce(async () => {
        store.handleScheduledMessages('session-1', [makeScheduled('a')])
        return makeScheduled('a')
      })
      await store.scheduleMessage('session-1', 'x', SEND_AT)
      expect(store.activeScheduledMessages).toHaveLength(1)

      scheduleMock.mockImplementationOnce(async () => {
        // Scheduled and sent before the response arrived.
        store.handleScheduledMessages('session-1', [])
        return makeScheduled('b')
      })
      await store.scheduleMessage('session-1', 'y', SEND_AT)
      expect(store.activeScheduledMessages).toEqual([])
    })

    it('reports a failure and resolves false so the composer keeps the text', async () => {
      const store = ready()
      scheduleMock.mockRejectedValueOnce(
        new ApiRequestError(400, 'send_at must be in the future', 'validation_error'),
      )

      await expect(store.scheduleMessage('session-1', 'x', SEND_AT)).resolves.toBe(false)

      expect(notifyError).toHaveBeenCalledWith('Scheduling failed', 'send_at must be in the future')
      expect(store.activeScheduledMessages).toEqual([])
    })
  })

  describe('rescheduleMessage', () => {
    it('moves the message to the new time', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [
        makeScheduled('a', { status: 'failed', error: 'Workspace is not running' }),
      ])
      rescheduleMock.mockResolvedValueOnce(makeScheduled('a', { send_at: SEND_AT.toISOString() }))

      await expect(store.rescheduleMessage('session-1', 'a', SEND_AT)).resolves.toBe(true)

      expect(rescheduleMock).toHaveBeenCalledWith('session-1', 'a', '2026-11-17T08:00:00.000Z')
      expect(store.activeScheduledMessages).toEqual([
        makeScheduled('a', { send_at: SEND_AT.toISOString() }),
      ])
    })

    it('drops a message that is no longer scheduled and says so', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a')])
      rescheduleMock.mockRejectedValueOnce(new ApiRequestError(404, 'gone', 'not_found'))

      await expect(store.rescheduleMessage('session-1', 'a', SEND_AT)).resolves.toBe(false)

      expect(store.activeScheduledMessages).toEqual([])
      expect(notifyError).toHaveBeenCalledWith(
        'Rescheduling failed',
        'The message is no longer scheduled.',
      )
    })

    it('keeps the message on other failures', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a')])
      rescheduleMock.mockRejectedValueOnce(
        new ApiRequestError(409, 'The message is being sent', 'scheduled_message_sending'),
      )

      await expect(store.rescheduleMessage('session-1', 'a', SEND_AT)).resolves.toBe(false)

      expect(notifyError).toHaveBeenCalledWith('Rescheduling failed', 'The message is being sent')
      expect(store.activeScheduledMessages).toHaveLength(1)
    })
  })

  describe('withdrawScheduledMessage', () => {
    it('removes the message and returns its text and skills for the composer', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a'), makeScheduled('b')])
      withdrawMock.mockResolvedValueOnce(
        makeScheduled('a', { content: 'first', skill_ids: ['sk'] }),
      )

      await expect(store.withdrawScheduledMessage('session-1', 'a')).resolves.toEqual({
        content: 'first',
        skillIds: ['sk'],
      })

      expect(withdrawMock).toHaveBeenCalledWith('session-1', 'a')
      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['b'])
    })

    it('quietly drops a message that was sent in the meantime', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a')])
      withdrawMock.mockRejectedValueOnce(new ApiRequestError(404, 'gone', 'not_found'))

      await expect(store.withdrawScheduledMessage('session-1', 'a')).resolves.toBeNull()

      expect(store.activeScheduledMessages).toEqual([])
      expect(notifyError).not.toHaveBeenCalled()
    })

    it('notifies on other failures and keeps the message', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a')])
      withdrawMock.mockRejectedValueOnce(
        new ApiRequestError(409, 'The message is being sent', 'scheduled_message_sending'),
      )

      await expect(store.withdrawScheduledMessage('session-1', 'a')).resolves.toBeNull()

      expect(notifyError).toHaveBeenCalledWith('Withdraw failed', 'The message is being sent')
      expect(store.activeScheduledMessages).toHaveLength(1)
    })
  })

  describe('sendScheduledMessageNow', () => {
    it('sends as a user turn: updates the session, drops the message and loads the turn', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a'), makeScheduled('b')])
      let resolveSend: (session: HarnessSession) => void = () => {}
      sendNowMock.mockReturnValueOnce(new Promise((resolve) => (resolveSend = resolve)))

      const sending = store.sendScheduledMessageNow('session-1', 'a')
      // The busy event of this turn must not trigger a second transcript load.
      await store.ensureUserTurnLoaded('session-1', 'user-2')
      expect(partsMock).not.toHaveBeenCalled()
      resolveSend(makeSession({ status: 'busy' }))

      await expect(sending).resolves.toBe(true)
      expect(sendNowMock).toHaveBeenCalledWith('session-1', 'a')
      expect(store.sessions[0]?.status).toBe('busy')
      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['b'])
      expect(partsMock).toHaveBeenCalledTimes(1)
    })

    it('reports a refused delivery and leaves the failed message to the server snapshot', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a')])
      sendNowMock.mockImplementationOnce(async () => {
        store.handleScheduledMessages('session-1', [
          makeScheduled('a', { status: 'failed', error: 'Workspace is not running' }),
        ])
        throw new ApiRequestError(503, 'Workspace is not running', 'service_unavailable')
      })

      await expect(store.sendScheduledMessageNow('session-1', 'a')).resolves.toBe(false)

      expect(notifyError).toHaveBeenCalledWith('Sending failed', 'Workspace is not running')
      expect(store.activeScheduledMessages).toEqual([
        makeScheduled('a', { status: 'failed', error: 'Workspace is not running' }),
      ])
    })

    it('stays quiet when the message is already on its way or gone', async () => {
      const store = ready()
      store.handleScheduledMessages('session-1', [makeScheduled('a'), makeScheduled('b')])
      sendNowMock.mockRejectedValueOnce(
        new ApiRequestError(409, 'The message is being sent', 'scheduled_message_sending'),
      )
      await expect(store.sendScheduledMessageNow('session-1', 'a')).resolves.toBe(false)
      expect(store.activeScheduledMessages).toHaveLength(2)

      sendNowMock.mockRejectedValueOnce(new ApiRequestError(404, 'gone', 'not_found'))
      await expect(store.sendScheduledMessageNow('session-1', 'b')).resolves.toBe(false)
      expect(store.activeScheduledMessages.map((message) => message.id)).toEqual(['a'])

      expect(notifyError).not.toHaveBeenCalled()
    })
  })
})
