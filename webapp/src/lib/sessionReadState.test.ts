import { describe, expect, it } from 'vitest'

import { SessionStatus, type Session } from '@/types'
import { findLatestUnreadDoneSession, isSessionEligibleForAutoRead } from './sessionReadState'

function makeSession(overrides: Partial<Session>): Session {
  return {
    id: overrides.id ?? 'session-1',
    workspace_id: overrides.workspace_id ?? 'workspace-1',
    chat_id: overrides.chat_id ?? 'chat-1',
    prompt: overrides.prompt ?? 'Prompt',
    agent_model: overrides.agent_model ?? 'gpt-5-mini',
    agent_options: overrides.agent_options ?? {},
    output: overrides.output ?? 'Output',
    error_message: overrides.error_message ?? null,
    status: overrides.status ?? SessionStatus.COMPLETED,
    read_at: overrides.read_at ?? null,
    created_at: overrides.created_at ?? '2026-03-27T10:00:00.000Z',
    completed_at: overrides.completed_at ?? '2026-03-27T10:01:00.000Z',
    skills: overrides.skills ?? [],
  }
}

describe('findLatestUnreadDoneSession', () => {
  it('treats newly completed visible sessions as eligible for auto-read', () => {
    expect(
      isSessionEligibleForAutoRead(
        makeSession({
          id: 'completed-now',
          status: SessionStatus.COMPLETED,
          read_at: null,
        }),
      ),
    ).toBe(true)
  })

  it('does not auto-read sessions the user explicitly kept unread', () => {
    expect(
      isSessionEligibleForAutoRead(
        makeSession({
          id: 'keep-unread',
          status: SessionStatus.FAILED,
          read_at: null,
        }),
        new Set(['keep-unread']),
      ),
    ).toBe(false)
  })

  it('returns the newest unread completed or failed session', () => {
    const sessions = [
      makeSession({ id: 'older', created_at: '2026-03-27T09:00:00.000Z' }),
      makeSession({ id: 'newer', created_at: '2026-03-27T11:00:00.000Z' }),
      makeSession({
        id: 'running',
        created_at: '2026-03-27T12:00:00.000Z',
        status: SessionStatus.RUNNING,
      }),
    ]

    expect(findLatestUnreadDoneSession(sessions)?.id).toBe('newer')
  })

  it('skips sessions suppressed from auto-read', () => {
    const sessions = [
      makeSession({ id: 'keep-unread', created_at: '2026-03-27T11:00:00.000Z' }),
      makeSession({ id: 'fallback', created_at: '2026-03-27T10:00:00.000Z' }),
    ]

    expect(findLatestUnreadDoneSession(sessions, new Set(['keep-unread']))?.id).toBe('fallback')
  })

  it('returns null when every done session is already read or suppressed', () => {
    const sessions = [
      makeSession({ id: 'read', read_at: '2026-03-27T11:05:00.000Z' }),
      makeSession({ id: 'suppressed' }),
    ]

    expect(findLatestUnreadDoneSession(sessions, new Set(['suppressed']))).toBeNull()
  })
})
