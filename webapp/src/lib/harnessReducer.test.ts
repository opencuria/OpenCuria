import { beforeEach, describe, expect, it } from 'vitest'

import type { HarnessMessage } from '@/types/harness'
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureAssistantMessage,
  findPart,
  resetHarnessPartCounter,
} from './harnessReducer'

function makeMessages(): HarnessMessage[] {
  return [
    {
      id: 'msg-user-1',
      session_id: 'session-1',
      role: 'user',
      content: 'hello',
      parts: [],
    },
  ]
}

describe('harnessReducer', () => {
  beforeEach(() => {
    resetHarnessPartCounter()
  })

  it('appends text deltas to a single running text part', () => {
    const messages = makeMessages()

    applyPartDelta(messages, 'session-1', { text: 'Hello ' })
    applyPartDelta(messages, 'session-1', { text: 'world' })

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const textParts = assistant.parts.filter((p) => p.type === 'text')
    expect(textParts).toHaveLength(1)
    expect(textParts[0]!.output).toBe('Hello world')
    expect(assistant.content).toBe('Hello world')
    expect(textParts[0]!.state).toBe('running')
  })

  it('transitions tool parts from running to completed and error', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1' },
      { step: 2, partId: 'part-tool-1' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    let tool = findPart(assistant, { callId: 'call-1' })
    expect(tool?.state).toBe('running')
    expect(tool?.title).toBe('$ ls')

    applyPartDelta(
      messages,
      'session-1',
      { tool_completed: 'bash', call_id: 'call-1', output: 'file.txt' },
      { step: 2 },
    )
    tool = findPart(assistant, { callId: 'call-1' })
    expect(tool?.state).toBe('completed')
    expect(tool?.output).toBe('file.txt')

    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'read a.txt', call_id: 'call-2' },
      {},
    )
    applyPartDelta(messages, 'session-1', { tool_error: 'boom', call_id: 'call-2' }, {})
    const failed = findPart(assistant, { callId: 'call-2' })
    expect(failed?.state).toBe('error')
    expect(failed?.output).toBe('boom')
  })

  it('replaces the todo list on todo_updated', () => {
    const next = applyTodoUpdate(
      [{ id: 'old', content: 'old', status: 'pending', priority: 'medium', order: 0 }],
      {
        todos: [
          { id: 'a', content: 'first', status: 'in_progress', priority: 'high', order: 0 },
          { id: 'b', content: 'second', status: 'pending', priority: 'low', order: 1 },
        ],
      },
    )

    expect(next).toHaveLength(2)
    expect(next[0]!.status).toBe('in_progress')
    expect(next.find((t) => t.id === 'old')).toBeUndefined()
  })

  it('tracks subtask start and finish transitions', () => {
    const messages = makeMessages()
    const assistant = ensureAssistantMessage(messages, 'session-1')

    const started = applySubtaskStarted(assistant, 'session-1', {
      workspace_id: 'workspace-1',
      session_id: 'session-1',
      subtask_id: 'sub-1',
      agent: 'explore',
      description: 'research the codebase',
    })
    expect(started.state).toBe('running')
    expect(started.meta?.['subtask_id']).toBe('sub-1')

    const finished = applySubtaskFinished(assistant, {
      workspace_id: 'workspace-1',
      session_id: 'session-1',
      subtask_id: 'sub-1',
      status: 'completed',
      summary: 'found it',
    })
    expect(finished?.state).toBe('completed')
    expect(finished?.output).toBe('found it')
  })
})
