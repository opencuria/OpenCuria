import { beforeEach, describe, expect, it } from 'vitest'

import type { HarnessMessage } from '@/types/harness'
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureAssistantMessage,
  findPart,
  mergeBusyFetchedMessages,
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
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1', arguments: 'ls -la' },
      { step: 2, partId: 'part-tool-1' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    let tool = findPart(assistant, { callId: 'call-1' })
    expect(tool?.state).toBe('running')
    expect(tool?.title).toBe('$ ls')
    expect(tool?.input).toEqual({ tool: 'bash', arguments: 'ls -la' })

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

  it('completes parallel tools by call_id even when they finish out of order', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'read a.txt', call_id: 'call-1' },
      { partId: 'part-1' },
    )
    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'read b.txt', call_id: 'call-2' },
      { partId: 'part-2' },
    )
    applyPartDelta(
      messages,
      'session-1',
      { tool_completed: 'read', call_id: 'call-2', output: 'b' },
      { partId: 'part-2' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    expect(findPart(assistant, { callId: 'call-1' })?.state).toBe('running')
    expect(findPart(assistant, { callId: 'call-2' })?.state).toBe('completed')
    expect(findPart(assistant, { callId: 'call-2' })?.output).toBe('b')
  })

  it('stores cost and tokens on step-finish parts', () => {
    const messages = makeMessages()

    applyPartDelta(messages, 'session-1', {
      step_finish: 1,
      cost: 0.0123,
      tokens: { prompt_tokens: 10, completion_tokens: 4, total_tokens: 14 },
    })

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const finish = assistant.parts.find((part) => part.type === 'step-finish')
    expect(finish?.meta).toMatchObject({
      step: 1,
      cost: 0.0123,
      tokens: { prompt_tokens: 10, completion_tokens: 4, total_tokens: 14 },
    })
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
      child_session_id: 'child-1',
    })
    expect(started.state).toBe('running')
    expect(started.meta?.['subtask_id']).toBe('sub-1')
    expect(started.meta?.['child_session_id']).toBe('child-1')

    const finished = applySubtaskFinished(assistant, {
      workspace_id: 'workspace-1',
      session_id: 'session-1',
      subtask_id: 'sub-1',
      status: 'completed',
      summary: 'found it',
      child_session_id: 'child-1',
    })
    expect(finished?.state).toBe('completed')
    expect(finished?.output).toBe('found it')
    expect(finished?.meta?.['child_session_id']).toBe('child-1')
  })

  it('starts a new assistant message after a completed turn', () => {
    const messages: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'first',
        parts: [],
      },
      {
        id: 'msg-assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'done',
        parts: [
          {
            id: 'part-1',
            session_id: 'session-1',
            type: 'text',
            state: 'completed',
            title: '',
            output: 'done',
          },
        ],
        completed_at: '2026-03-29T10:00:00.000Z',
      },
      {
        id: 'msg-user-2',
        session_id: 'session-1',
        role: 'user',
        content: 'second',
        parts: [],
      },
    ]

    applyPartDelta(messages, 'session-1', { text: 'new reply' })

    const assistants = messages.filter((message) => message.role === 'assistant')
    expect(assistants).toHaveLength(2)
    expect(assistants[0]!.content).toBe('done')
    expect(assistants[1]!.content).toBe('new reply')
    expect(messages[messages.length - 1]!.role).toBe('assistant')
  })

  it('keeps appending deltas to a running last assistant message', () => {
    const messages: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'hello',
        parts: [],
      },
      {
        id: 'msg-assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hel',
        parts: [
          {
            id: 'part-1',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hel',
          },
        ],
        completed_at: null,
      },
    ]

    applyPartDelta(messages, 'session-1', { text: 'lo' })

    expect(messages.filter((message) => message.role === 'assistant')).toHaveLength(1)
    expect(messages[1]!.content).toBe('Hello')
    expect(messages[1]!.parts[0]!.output).toBe('Hello')
  })

  it('keeps local streaming content when a busy fetch snapshot is behind', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'hello',
        parts: [],
      },
      {
        id: 'local-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello world',
        parts: [
          {
            id: 'local-part',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hello world',
          },
        ],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'hello',
        parts: [],
      },
      {
        id: 'server-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello',
        parts: [
          {
            id: 'server-part',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hello',
          },
        ],
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const last = merged[merged.length - 1]
    expect(last!.id).toBe('server-assistant')
    expect(last!.content).toBe('Hello world')
    expect(last!.parts[0]!.output).toBe('Hello world')
  })
})
