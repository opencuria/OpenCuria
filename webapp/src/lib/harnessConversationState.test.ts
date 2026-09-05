import { describe, expect, it } from 'vitest'

import {
  isHarnessConversationAvailable,
  isHarnessConversationDoneUnread,
  isHarnessConversationRunning,
} from '@/lib/harnessConversationState'
import type { HarnessConversation } from '@/types/harness'

function makeConversation(overrides: Partial<HarnessConversation> = {}): HarnessConversation {
  return {
    session_id: 'session-1',
    workspace_id: 'ws-1',
    workspace_name: 'Workspace One',
    title: 'Fix tests',
    status: 'idle',
    mode: 'build',
    agent_name: 'build',
    unread: false,
    updated_at: '2026-03-29T10:00:00.000Z',
    ...overrides,
  }
}

describe('harnessConversationState', () => {
  it('maps busy sessions to in-progress column', () => {
    const conv = makeConversation({ status: 'busy' })
    expect(isHarnessConversationRunning(conv)).toBe(true)
    expect(isHarnessConversationDoneUnread(conv)).toBe(false)
    expect(isHarnessConversationAvailable(conv)).toBe(false)
  })

  it('maps idle unread sessions to done column', () => {
    const conv = makeConversation({ status: 'idle', unread: true })
    expect(isHarnessConversationDoneUnread(conv)).toBe(true)
    expect(isHarnessConversationAvailable(conv)).toBe(false)
  })

  it('maps idle read sessions to available column', () => {
    const conv = makeConversation({ status: 'idle', unread: false })
    expect(isHarnessConversationAvailable(conv)).toBe(true)
    expect(isHarnessConversationDoneUnread(conv)).toBe(false)
  })
})
