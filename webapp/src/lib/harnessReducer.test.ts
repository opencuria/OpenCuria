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
  settleOpenStreamParts,
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

  it('merges live attachments into meta on tool_completed and keeps step', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'read cat.png', call_id: 'call-img' },
      { step: 3, partId: 'part-img' },
    )

    applyPartDelta(
      messages,
      'session-1',
      {
        tool_completed: 'read',
        call_id: 'call-img',
        output: 'Image read successfully',
        attachments: [
          { type: 'file', mime: 'image/png', url: 'data:image/png;base64,iVBORw0KGgo=', filename: 'cat.png' },
          { type: 'file', mime: '', url: 'https://example.com/a.png' },
        ],
      },
      { step: 3, partId: 'part-img' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const tool = findPart(assistant, { callId: 'call-img' })
    expect(tool?.state).toBe('completed')
    expect(tool?.output).toBe('Image read successfully')
    expect(tool?.meta).toMatchObject({
      step: 3,
      attachments: [
        { type: 'file', mime: 'image/png', url: 'data:image/png;base64,iVBORw0KGgo=', filename: 'cat.png' },
      ],
    })
  })

  it('creates meta.attachments on the fallback part when tool_completed arrives first', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      {
        tool_completed: 'read',
        call_id: 'call-late',
        output: 'PDF read successfully',
        attachments: [
          { type: 'file', mime: 'application/pdf', url: 'data:application/pdf;base64,JVBERi0=', filename: 'doc.pdf' },
        ],
      },
      { step: 4, partId: 'part-late' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const tool = findPart(assistant, { callId: 'call-late' })
    expect(tool?.state).toBe('completed')
    expect(tool?.meta).toMatchObject({
      step: 4,
      attachments: [
        { type: 'file', mime: 'application/pdf', url: 'data:application/pdf;base64,JVBERi0=', filename: 'doc.pdf' },
      ],
    })
  })

  it('does not crash on tool_completed without attachments', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'read a.txt', call_id: 'call-plain' },
      { step: 1, partId: 'part-plain' },
    )
    applyPartDelta(
      messages,
      'session-1',
      { tool_completed: 'read', call_id: 'call-plain', output: 'body' },
      { step: 1, partId: 'part-plain' },
    )
    // Broken attachment payloads are dropped defensively.
    applyPartDelta(
      messages,
      'session-1',
      { tool_completed: 'read', call_id: 'call-broken', attachments: 'nope' as unknown as [] },
      {},
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const plain = findPart(assistant, { callId: 'call-plain' })
    expect(plain?.state).toBe('completed')
    expect(plain?.output).toBe('body')
    expect(plain?.meta).toMatchObject({ step: 1 })
    const broken = findPart(assistant, { callId: 'call-broken' })
    expect(broken?.state).toBe('completed')
    expect(broken?.meta?.['attachments']).toBeUndefined()
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

  it('creates a completed agent plan part on live delta.agent without touching content', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      {
        agent: '(Previous action verification)\nok\n(Screenshot Analysis)\nlogin form\n(Next Action)\nclick submit\n(Grounded Action)\n```python\nagent.click("Submit")\n```',
        agent_meta: {
          verification: 'ok',
          analysis: 'login form',
          next_action: 'click submit',
          action: 'click "Submit"',
          action_kind: 'click',
        },
      },
      { step: 3, partId: 'agent-part-3' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    expect(assistant.content).toBe('')
    const agent = findPart(assistant, { partId: 'agent-part-3' })
    expect(agent?.type).toBe('agent')
    expect(agent?.state).toBe('completed')
    expect(agent?.title).toBe('Agent plan')
    expect(agent?.output).toContain('click submit')
    expect(agent?.meta).toMatchObject({
      step: 3,
      agent_meta: {
        verification: 'ok',
        analysis: 'login form',
        next_action: 'click submit',
        action: 'click "Submit"',
        action_kind: 'click',
      },
    })
  })

  it('updates (not duplicates) a repeated delta.agent with the same part id', () => {
    const messages = makeMessages()

    applyPartDelta(messages, 'session-1', { agent: 'plan v1' }, { step: 2, partId: 'agent-2' })
    applyPartDelta(messages, 'session-1', { agent: 'plan v2' }, { step: 2, partId: 'agent-2' })

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const agents = assistant.parts.filter((part) => part.type === 'agent')
    expect(agents).toHaveLength(1)
    expect(agents[0]!.output).toBe('plan v2')
    expect(assistant.content).toBe('')
  })

  it('drops unknown agent_meta keys and keeps step on the live part', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      {
        agent: 'plan',
        agent_meta: {
          action: 'click',
          action_kind: 'click',
          exec_code: 'agent.click(1,2)',
        } as unknown as Record<string, string>,
      },
      { step: 1, partId: 'agent-1' },
    )

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const agent = findPart(assistant, { partId: 'agent-1' })
    expect(agent?.meta?.['agent_meta']).toEqual({ action: 'click', action_kind: 'click' })
    expect(agent?.meta?.['step']).toBe(1)
  })

  it('attributes live reasoning to the event step', () => {
    const messages = makeMessages()

    applyPartDelta(messages, 'session-1', { reasoning: 'reflecting' }, { step: 4 })

    const assistant = ensureAssistantMessage(messages, 'session-1')
    const reasoning = assistant.parts.find((part) => part.type === 'reasoning')
    expect(reasoning?.output).toBe('reflecting')
    expect(reasoning?.meta?.['step']).toBe(4)
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
      model: 'acme/think',
      reasoning_effort: 'high',
    })
    expect(started.state).toBe('running')
    expect(started.meta?.['subtask_id']).toBe('sub-1')
    expect(started.meta?.['child_session_id']).toBe('child-1')
    expect(started.meta?.['model']).toBe('acme/think')
    expect(started.meta?.['reasoning_effort']).toBe('high')

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

  it('keeps live agent and step parts when lengths are equal (busy race)', () => {
    const stepMeta = (step: number) => ({ step })
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'local-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello',
        parts: [
          {
            id: 'text-1',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hello',
          },
          {
            id: 'agent-live-2',
            session_id: 'session-1',
            type: 'agent',
            state: 'completed',
            title: 'Agent plan',
            output: 'plan two',
            meta: { ...stepMeta(2), agent_meta: { action: 'click' } },
          },
          {
            id: 'start-live-2',
            session_id: 'session-1',
            type: 'step-start',
            state: 'running',
            title: 'Step 2',
            output: '',
            meta: stepMeta(2),
          },
          {
            id: 'reason-live-2',
            session_id: 'session-1',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'thinking',
            meta: stepMeta(2),
          },
        ],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'server-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello',
        parts: [
          {
            id: 'text-1',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hello',
          },
          {
            id: 'agent-server-1',
            session_id: 'session-1',
            type: 'agent',
            state: 'completed',
            title: 'Agent plan',
            output: 'plan one',
            meta: stepMeta(1),
          },
        ],
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const last = merged[merged.length - 1]!
    const ids = last.parts.map((part) => part.id)
    // Same-length text does not drop the live step-2 parts.
    expect(ids).toContain('agent-server-1')
    expect(ids).toContain('agent-live-2')
    expect(ids).toContain('start-live-2')
    expect(ids).toContain('reason-live-2')
    expect(last.content).toBe('Hello')
  })

  it('does not duplicate a reconciled step part under a new server id', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'local-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello',
        parts: [
          {
            id: 'agent-live-1',
            session_id: 'session-1',
            type: 'agent',
            state: 'completed',
            title: 'Agent plan',
            output: 'same plan',
            meta: { step: 1 },
          },
        ],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'server-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello',
        parts: [
          {
            id: 'agent-server-1',
            session_id: 'session-1',
            type: 'agent',
            state: 'completed',
            title: 'Agent plan',
            output: 'same plan',
            meta: { step: 1 },
          },
        ],
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const last = merged[merged.length - 1]!
    expect(last.parts.filter((part) => part.type === 'agent')).toHaveLength(1)
    expect(last.parts[0]!.id).toBe('agent-server-1')
  })

  it('merges live agent_meta onto a server agent part missing it', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'local-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'agent-1',
            session_id: 'session-1',
            type: 'agent',
            state: 'completed',
            title: 'Agent plan',
            output: 'plan one',
            meta: { step: 1, agent_meta: { action: 'click' } },
          },
        ],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'server-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'agent-1',
            session_id: 'session-1',
            type: 'agent',
            state: 'completed',
            title: 'Agent plan',
            output: 'plan one',
            meta: { step: 1 },
          },
        ],
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const agent = merged[merged.length - 1]!.parts[0]!
    expect(agent.meta?.['agent_meta']).toEqual({ action: 'click' })
  })

  it('folds a live reasoning prefix into the server row (same step, server id wins)', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'local-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'local-session-1-1',
            session_id: 'session-1',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'thinking more',
            meta: { step: 2 },
          },
        ],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'server-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'server-reasoning-uuid',
            session_id: 'session-1',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'think',
            meta: { step: 2 },
          },
        ],
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const last = merged[merged.length - 1]!
    const reasoning = last.parts.filter((part) => part.type === 'reasoning')
    expect(reasoning).toHaveLength(1)
    expect(reasoning[0]!.id).toBe('server-reasoning-uuid')
    expect(reasoning[0]!.output).toBe('thinking more')
  })

  it('keeps same-step reasoning rows with genuinely different content', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'local-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'local-session-1-1',
            session_id: 'session-1',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'checking the network',
            meta: { step: 2 },
          },
        ],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'go',
        parts: [],
      },
      {
        id: 'server-assistant',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [
          {
            id: 'server-reasoning-uuid',
            session_id: 'session-1',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'reviewing the screen',
            meta: { step: 2 },
          },
        ],
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const last = merged[merged.length - 1]!
    expect(last.parts.filter((part) => part.type === 'reasoning')).toHaveLength(2)
  })

  it('settles leftover running text and reasoning parts without touching tools', () => {
    const messages: HarnessMessage[] = [
      {
        id: 'msg-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'done',
        parts: [
          {
            id: 'r1',
            session_id: 'session-1',
            type: 'reasoning',
            state: 'running',
            title: '',
            output: 'planning',
          },
          {
            id: 't1',
            session_id: 'session-1',
            type: 'text',
            state: 'pending',
            title: '',
            output: 'done',
          },
          {
            id: 'tool-1',
            session_id: 'session-1',
            type: 'tool',
            state: 'running',
            title: 'bash',
            output: '',
          },
        ],
      },
    ]

    const settled = settleOpenStreamParts(messages)
    expect(settled[0]!.parts[0]!.state).toBe('completed')
    expect(settled[0]!.parts[0]!.output).toBe('planning')
    expect(settled[0]!.parts[1]!.state).toBe('completed')
    expect(settled[0]!.parts[2]!.state).toBe('running')
  })
})
