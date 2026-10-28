import { describe, expect, it } from 'vitest'

import { resolveActiveChatTitle } from '@/lib/activeChatTitle'
import type { HarnessConversation, HarnessSession } from '@/types/harness'

function makeSession(overrides: Partial<HarnessSession> = {}): HarnessSession {
  return {
    id: 'session-1',
    workspace_id: 'workspace-1',
    title: 'The full initial prompt',
    mode: 'build',
    agent_name: 'build',
    model: '',
    status: 'idle',
    cost: 0,
    tokens: {},
    updated_at: '2026-10-27T10:00:00.000Z',
    ...overrides,
  }
}

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 'session-1',
    workspace_id: 'workspace-1',
    workspace_name: 'Workspace One',
    title: 'Generated title',
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    model: '',
    unread: false,
    updated_at: '2026-10-27T10:01:00.000Z',
    last_message_at: '2026-10-27T10:00:00.000Z',
    ...overrides,
  }
}

describe('resolveActiveChatTitle', () => {
  it('uses the newer backend conversation title while the workspace session snapshot is stale', () => {
    expect(resolveActiveChatTitle(makeSession(), 'workspace-1', [makeConversation()])).toBe(
      'Generated title',
    )
  })

  it('does not replace a more recently renamed session with an older feed title', () => {
    const renamed = makeSession({
      title: 'Manually renamed',
      updated_at: '2026-10-27T10:02:00.000Z',
    })
    const olderFeed = makeConversation({
      title: 'Earlier generated title',
      updated_at: '2026-10-27T10:01:00.000Z',
    })

    expect(resolveActiveChatTitle(renamed, 'workspace-1', [olderFeed])).toBe('Manually renamed')
  })

  it('only reads the feed row for the active session in the active workspace', () => {
    const session = makeSession()
    const otherSession = makeConversation({ session_id: 'session-other', title: 'Wrong session' })
    const otherWorkspace = makeConversation({
      workspace_id: 'workspace-other',
      title: 'Wrong workspace',
    })

    expect(resolveActiveChatTitle(session, 'workspace-1', [otherSession, otherWorkspace])).toBe(
      'The full initial prompt',
    )
    expect(resolveActiveChatTitle(session, 'workspace-other', [makeConversation()])).toBeNull()
  })
})
