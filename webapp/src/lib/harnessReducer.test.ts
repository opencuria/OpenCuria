import { beforeEach, describe, expect, it } from 'vitest'

import type { HarnessMessage } from '@/types/harness'
import {
  applyPartDelta,
  applySubtaskFinished,
  applySubtaskStarted,
  applyTodoUpdate,
  ensureAssistantMessage,
  ensureBusyAssistant,
  findPart,
  isLocalId,
  mergeBusyFetchedMessages,
  resetHarnessPartCounter,
  resolveQueuedToolState,
  routeAssistantMessage,
  settleOpenStreamParts,
  sortHarnessMessages,
  sortHarnessParts,
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

  it('keeps compact live tool projections unloaded while preserving legacy full deltas', () => {
    const messages = makeMessages()
    applyPartDelta(
      messages,
      'session-1',
      {
        tool_started: 'read',
        title: 'Read file',
        call_id: 'call-live',
      },
      { partId: 'tool-live' },
    )
    let assistant = ensureAssistantMessage(messages, 'session-1')
    let tool = findPart(assistant, { partId: 'tool-live' })!
    expect(tool.detail_loaded).toBe(false)
    expect(tool.input).toEqual({ tool: 'read' })

    applyPartDelta(
      messages,
      'session-1',
      {
        tool_completed: 'read',
        call_id: 'call-live',
        state: 'completed',
        display: { tool: 'read', summary: 'Read file' },
      },
      { partId: 'tool-live' },
    )
    expect(tool.state).toBe('completed')
    expect(tool.display?.summary).toBe('Read file')
    expect(tool.detail_loaded).toBe(false)
    expect(tool.output).toBe('')

    applyPartDelta(
      messages,
      'session-1',
      {
        tool_started: 'bash',
        call_id: 'call-legacy',
        arguments: '{"command":"ls"}',
      },
      { partId: 'tool-legacy' },
    )
    applyPartDelta(
      messages,
      'session-1',
      {
        tool_completed: 'bash',
        call_id: 'call-legacy',
        output: 'file.txt',
        attachments: [
          {
            type: 'file',
            mime: 'text/plain',
            url: 'data:text/plain;base64,QQ==',
            filename: 'file.txt',
          },
        ],
      },
      { partId: 'tool-legacy' },
    )
    assistant = ensureAssistantMessage(messages, 'session-1')
    const legacy = findPart(assistant, { partId: 'tool-legacy' })!
    expect(legacy.detail_loaded).toBe(true)
    expect(legacy.output).toBe('file.txt')
    expect(legacy.meta?.['attachments']).toHaveLength(1)
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
          {
            type: 'file',
            mime: 'image/png',
            url: 'data:image/png;base64,iVBORw0KGgo=',
            filename: 'cat.png',
          },
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
        {
          type: 'file',
          mime: 'image/png',
          url: 'data:image/png;base64,iVBORw0KGgo=',
          filename: 'cat.png',
        },
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
          {
            type: 'file',
            mime: 'application/pdf',
            url: 'data:application/pdf;base64,JVBERi0=',
            filename: 'doc.pdf',
          },
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
        {
          type: 'file',
          mime: 'application/pdf',
          url: 'data:application/pdf;base64,JVBERi0=',
          filename: 'doc.pdf',
        },
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

  it('stores compact live agent summary as display, not a raw full plan', () => {
    const messages = makeMessages()
    applyPartDelta(
      messages,
      'session-1',
      {
        agent: 'Form ready',
        agent_meta: { action: 'Click Save', action_kind: 'click', analysis: 'Form ready' },
        display: { summary: 'Form ready', step: 2 },
      },
      { step: 2, partId: 'agent-live' },
    )
    const part = ensureAssistantMessage(messages, 'session-1').parts[0]!
    expect(part.type).toBe('agent')
    expect(part.output).toBe('Form ready')
    expect(part.display).toMatchObject({
      summary: 'Form ready',
      agent_meta: { analysis: 'Form ready' },
    })
    expect(part.detail_loaded).toBe(false)
  })

  it('uses the explicit display marker for compact agent projections', () => {
    const messages = makeMessages()
    applyPartDelta(
      messages,
      'session-1',
      {
        agent: 'Agent plan',
        agent_meta: { analysis: 'Form ready' },
        display: { summary: 'Form ready' },
      },
      { step: 1, partId: 'agent-projection' },
    )
    const part = ensureAssistantMessage(messages, 'session-1').parts[0]!
    expect(part.output).toBe('Agent plan')
    expect(part.display?.summary).toBe('Form ready')
    expect(part.detail_loaded).toBe(false)
  })

  it('preserves legacy plan detail when a summary event updates an existing part', () => {
    const messages = makeMessages()
    applyPartDelta(
      messages,
      'session-1',
      {
        agent: 'Full plan text',
        agent_meta: { analysis: 'Old analysis', next_action: 'Old action' },
      },
      { step: 1, partId: 'agent-existing' },
    )
    const assistant = ensureAssistantMessage(messages, 'session-1')
    const part = findPart(assistant, { partId: 'agent-existing' })!
    part.detail_loaded = false
    applyPartDelta(
      messages,
      'session-1',
      {
        agent: 'Safe summary',
        agent_meta: { analysis: 'Safe summary', next_action: 'New action' },
        display: { summary: 'Safe summary' },
      },
      { step: 1, partId: 'agent-existing' },
    )
    expect(part.output).toBe('Safe summary')
    expect(part.display?.summary).toBe('Safe summary')
    expect(part.detail_loaded).toBe(false)
  })

  it('does not mistake a legacy plan equal to its parsed summary for a projection', () => {
    const messages = makeMessages()
    const plan = 'Form ready'
    applyPartDelta(
      messages,
      'session-1',
      { agent: plan, agent_meta: { analysis: plan, next_action: 'Click Save' } },
      { step: 1, partId: 'agent-legacy-short' },
    )
    const part = ensureAssistantMessage(messages, 'session-1').parts[0]!
    expect(part.output).toBe(plan)
    expect(part.detail_loaded).toBe(true)
    expect(part.display).toBeUndefined()
  })

  it('preserves a legacy full agent plan delta when structured fields accompany it', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      {
        agent:
          '(Previous action verification)\nok\n(Screenshot Analysis)\nlogin form\n(Next Action)\nclick submit\n(Grounded Action)\n```python\nagent.click("Submit")\n```',
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
    expect(agent?.detail_loaded).toBe(true)
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

  it('routes fresh turns by backend message_id instead of reusing the previous answer', () => {
    const messages: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'first',
        parts: [],
        position: 0,
      },
      {
        id: 'msg-assistant-old',
        session_id: 'session-1',
        role: 'assistant',
        content: 'old answer',
        parts: [
          {
            id: 'part-old',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'old answer',
          },
        ],
        position: 1,
      },
      {
        id: 'msg-user-2',
        session_id: 'session-1',
        role: 'user',
        content: 'follow up',
        parts: [],
        position: 2,
      },
    ]

    const fresh = applyPartDelta(
      messages,
      'session-1',
      { text: 'new' },
      { messageId: 'msg-assistant-new' },
    )

    expect(fresh.id).toBe('msg-assistant-new')
    expect(fresh.content).toBe('new')
    const old = messages.find((m) => m.id === 'msg-assistant-old')!
    expect(old.content).toBe('old answer')
    expect(messages.filter((m) => m.role === 'assistant')).toHaveLength(2)
  })

  it('ignores late stale events for a completed turn (never attaches to latest)', () => {
    const messages: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'first',
        parts: [],
      },
      {
        id: 'msg-assistant-old',
        session_id: 'session-1',
        role: 'assistant',
        content: 'old',
        parts: [
          {
            id: 'part-old',
            session_id: 'session-1',
            type: 'text',
            state: 'completed',
            title: '',
            output: 'old',
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
      {
        id: 'msg-assistant-new',
        session_id: 'session-1',
        role: 'assistant',
        content: '',
        parts: [],
      },
    ]

    applyPartDelta(messages, 'session-1', { text: 'stale' }, { messageId: 'msg-assistant-old' })

    expect(messages.find((m) => m.id === 'msg-assistant-old')!.content).toBe('old')
    expect(messages.find((m) => m.id === 'msg-assistant-new')!.content).toBe('')
  })

  it('anchors a busy fresh shell after the optimistic follow-up user', () => {
    const messages: HarnessMessage[] = [
      { id: 'msg-user-1', session_id: 'session-1', role: 'user', content: 'first', parts: [] },
      {
        id: 'msg-assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'done',
        parts: [],
        completed_at: '2026-03-29T10:00:00.000Z',
      },
      {
        id: 'local-user-session-1-1',
        session_id: 'session-1',
        role: 'user',
        content: 'follow up',
        parts: [],
      },
    ]

    const shell = ensureBusyAssistant(
      messages,
      'session-1',
      'server-assistant-2',
      'missing-user-id',
    )

    expect(shell.id).toBe('server-assistant-2')
    expect(messages.map((m) => m.id)).toEqual([
      'msg-user-1',
      'msg-assistant-1',
      'local-user-session-1-1',
      'server-assistant-2',
    ])
  })

  it('creates queued pending tool then flips the same part_id to running (no duplicate)', () => {
    const messages = makeMessages()

    const queued = applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1', state: 'pending' },
      { step: 1, partId: 'part-1' },
    )
    expect(findPart(queued, { partId: 'part-1' })?.state).toBe('pending')

    const running = applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1', state: 'running' },
      { step: 1, partId: 'part-1' },
    )
    const tools = running.parts.filter((p) => p.type === 'tool')
    expect(tools).toHaveLength(1)
    expect(tools[0]!.state).toBe('running')
  })

  it('detects queued pending via the queued flag when delta.state is absent', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'Read', call_id: 'call-q', queued: true },
      { partId: 'part-q' },
    )
    const assistant = ensureAssistantMessage(messages, 'session-1')
    expect(findPart(assistant, { partId: 'part-q' })?.state).toBe('pending')

    // The actual start repeats the same part_id: flips to running.
    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', title: 'Read', call_id: 'call-q' },
      { partId: 'part-q' },
    )
    expect(findPart(assistant, { partId: 'part-q' })?.state).toBe('running')
    expect(assistant.parts.filter((p) => p.type === 'tool')).toHaveLength(1)
  })

  it('resolves queued tool state from delta.state, flags, and legacy singles', () => {
    expect(
      resolveQueuedToolState({ tool_started: 'bash', state: 'pending' }, { known: false }),
    ).toBe('pending')
    expect(
      resolveQueuedToolState({ tool_started: 'bash', state: 'running' }, { known: true }),
    ).toBe('running')
    expect(resolveQueuedToolState({ tool_started: 'bash', queued: true }, { known: false })).toBe(
      'pending',
    )
    // Legacy single emit (no signal) stays running.
    expect(resolveQueuedToolState({ tool_started: 'bash' }, { known: false })).toBe('running')
  })

  it('keeps text/tool/text in interleaved order (no stream merge across tools)', () => {
    const messages = makeMessages()

    applyPartDelta(
      messages,
      'session-1',
      { text: 'before ' },
      { partId: 'text-1', messageId: 'assistant-1' },
    )
    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'bash', title: '$ ls', call_id: 'call-1', queued: true },
      { partId: 'tool-1', messageId: 'assistant-1' },
    )
    applyPartDelta(
      messages,
      'session-1',
      { tool_completed: 'bash', call_id: 'call-1', output: 'out' },
      { partId: 'tool-1', messageId: 'assistant-1' },
    )
    applyPartDelta(
      messages,
      'session-1',
      { text: 'after' },
      { partId: 'text-2', messageId: 'assistant-1' },
    )

    const assistant = messages.find((m) => m.id === 'assistant-1')!
    expect(assistant.parts.map((p) => p.type)).toEqual(['text', 'tool', 'text'])
    expect(assistant.parts.map((p) => p.output)).toEqual(['before ', 'out', 'after'])
    expect(assistant.content).toBe('before after')
  })

  it('adopts the server part_id for a local placeholder text stream', () => {
    const messages: HarnessMessage[] = [
      { id: 'msg-user-1', session_id: 'session-1', role: 'user', content: 'hi', parts: [] },
      {
        id: 'assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'hel',
        parts: [
          {
            id: 'local-session-1-1',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'hel',
          },
        ],
      },
    ]

    applyPartDelta(
      messages,
      'session-1',
      { text: 'lo' },
      { partId: 'server-text-1', messageId: 'assistant-1' },
    )

    const assistant = messages.find((m) => m.id === 'assistant-1')!
    expect(assistant.parts.filter((p) => p.type === 'text')).toHaveLength(1)
    expect(assistant.parts[0]!.id).toBe('server-text-1')
    expect(assistant.parts[0]!.output).toBe('hello')
  })

  it('routeAssistantMessage returns the running shell and creates server-id shells', () => {
    const messages = makeMessages()
    const first = routeAssistantMessage(messages, 'session-1', 'server-1')
    expect(first.id).toBe('server-1')
    const same = routeAssistantMessage(messages, 'session-1', 'server-1')
    expect(same).toBe(first)
    expect(isLocalId('local-session-1-1')).toBe(true)
    expect(isLocalId('server-1')).toBe(false)
  })

  it('sorts messages by position with creation-order fallback', () => {
    const messages: HarnessMessage[] = [
      {
        id: 'b',
        session_id: 's',
        role: 'user',
        content: 'b',
        parts: [],
        position: 1,
        created_at: '2026-03-29T10:00:01.000Z',
      },
      {
        id: 'a',
        session_id: 's',
        role: 'user',
        content: 'a',
        parts: [],
        position: 0,
        created_at: '2026-03-29T10:00:02.000Z',
      },
      { id: 'local', session_id: 's', role: 'user', content: 'c', parts: [] },
    ]
    expect(sortHarnessMessages(messages).map((m) => m.id)).toEqual(['a', 'b', 'local'])
  })

  it('sorts parts by position while preserving local observed order', () => {
    const parts = [
      {
        id: 'p2',
        session_id: 's',
        type: 'tool' as const,
        state: 'completed' as const,
        title: '',
        output: '',
        position: 1,
      },
      {
        id: 'p1',
        session_id: 's',
        type: 'text' as const,
        state: 'completed' as const,
        title: '',
        output: '',
        position: 0,
      },
      {
        id: 'local-1',
        session_id: 's',
        type: 'text' as const,
        state: 'running' as const,
        title: '',
        output: '',
      },
    ]
    expect(sortHarnessParts(parts).map((p) => p.id)).toEqual(['p1', 'p2', 'local-1'])
  })

  it('preserves already-received live data when the busy snapshot is stale', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'hi',
        parts: [],
        position: 0,
      },
      {
        id: 'assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello world, more streamed',
        parts: [
          {
            id: 'server-text-1',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hello world, more streamed',
            position: 0,
          },
          {
            id: 'server-tool-1',
            session_id: 'session-1',
            type: 'tool',
            state: 'running',
            title: 'bash',
            output: '',
            call_id: 'call-1',
            position: 1,
          },
        ],
        position: 1,
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'hi',
        parts: [],
        position: 0,
      },
      {
        id: 'assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'Hello',
        parts: [
          {
            id: 'server-text-1',
            session_id: 'session-1',
            type: 'text',
            state: 'running',
            title: '',
            output: 'Hello',
            position: 0,
          },
        ],
        position: 1,
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    const assistant = merged.find((m) => m.id === 'assistant-1')!
    expect(assistant.content).toBe('Hello world, more streamed')
    expect(assistant.parts.find((p) => p.id === 'server-text-1')!.output).toBe(
      'Hello world, more streamed',
    )
    // Live-only tool row the stale snapshot missed is kept.
    expect(assistant.parts.some((p) => p.id === 'server-tool-1')).toBe(true)
  })

  it('carries the optimistic follow-up user across a busy fetch until echoed', () => {
    const previous: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'first',
        parts: [],
        position: 0,
      },
      {
        id: 'assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'done',
        parts: [],
        position: 1,
        completed_at: '2026-03-29T10:00:00.000Z',
      },
      {
        id: 'local-user-session-1-9',
        session_id: 'session-1',
        role: 'user',
        content: 'follow up',
        parts: [],
      },
    ]
    const incoming: HarnessMessage[] = [
      {
        id: 'msg-user-1',
        session_id: 'session-1',
        role: 'user',
        content: 'first',
        parts: [],
        position: 0,
      },
      {
        id: 'assistant-1',
        session_id: 'session-1',
        role: 'assistant',
        content: 'done',
        parts: [],
        position: 1,
        completed_at: '2026-03-29T10:00:00.000Z',
      },
    ]

    const merged = mergeBusyFetchedMessages(previous, incoming)
    expect(merged.some((m) => m.id === 'local-user-session-1-9')).toBe(true)

    // Server echo arrives: the optimistic row drops (no duplicate).
    const echoed = mergeBusyFetchedMessages(previous, [
      ...incoming,
      {
        id: 'msg-user-2',
        session_id: 'session-1',
        role: 'user',
        content: 'follow up',
        parts: [],
        position: 2,
      },
    ])
    expect(echoed.filter((m) => m.role === 'user' && m.content === 'follow up')).toHaveLength(1)
    expect(echoed.some((m) => m.id === 'local-user-session-1-9')).toBe(false)
  })

  it('routes subtask events to the per-turn shell via message_id', () => {
    const messages: HarnessMessage[] = [
      { id: 'msg-user-1', session_id: 'session-1', role: 'user', content: 'go', parts: [] },
      { id: 'assistant-fresh', session_id: 'session-1', role: 'assistant', content: '', parts: [] },
    ]
    const target = routeAssistantMessage(messages, 'session-1', 'assistant-fresh')
    const started = applySubtaskStarted(target, 'session-1', {
      workspace_id: 'ws',
      session_id: 'session-1',
      message_id: 'assistant-fresh',
      subtask_id: 'sub-1',
      agent: 'explore',
      description: 'research',
      part_id: 'subtask-part-1',
    })
    expect(started.id).toBe('subtask-part-1')
    expect(started.state).toBe('running')
    expect(target.parts.some((p) => p.id === 'subtask-part-1')).toBe(true)
  })
})

describe('server live positions', () => {
  it('keeps text/tool/text order after a stale REST snapshot is merged', () => {
    const messages = makeMessages()
    applyPartDelta(
      messages,
      'session-1',
      { text: 'before' },
      { messageId: 'assistant-1', partId: 'text-a', partPosition: 1 },
    )
    applyPartDelta(
      messages,
      'session-1',
      { tool_started: 'read', call_id: 'call-1', state: 'pending' },
      { messageId: 'assistant-1', partId: 'tool-a', partPosition: 2 },
    )
    applyPartDelta(
      messages,
      'session-1',
      { text: 'between' },
      { messageId: 'assistant-1', partId: 'text-b', partPosition: 3 },
    )
    const assistant = messages.find((m) => m.id === 'assistant-1')!
    expect(assistant.parts.map((p) => [p.id, p.position])).toEqual([
      ['text-a', 1],
      ['tool-a', 2],
      ['text-b', 3],
    ])
    const incoming: HarnessMessage[] = [
      { ...messages[0]!, position: 0 },
      {
        ...assistant,
        content: 'before',
        position: 1,
        parts: [
          { ...assistant.parts[0]!, output: 'before' },
          { ...assistant.parts[1]!, state: 'pending' },
        ],
      },
    ]
    const merged = mergeBusyFetchedMessages(messages, incoming)
    expect(merged[1]?.parts.map((p) => p.id)).toEqual(['text-a', 'tool-a', 'text-b'])
    expect(merged[1]?.parts[2]?.output).toBe('between')
  })

  it('keeps a running assistant when a busy snapshot is empty', () => {
    const messages = makeMessages()
    applyPartDelta(
      messages,
      'session-1',
      { text: 'live' },
      { messageId: 'assistant-live', partId: 'live-part', partPosition: 0 },
    )
    expect(mergeBusyFetchedMessages(messages, []).map((m) => m.id)).toEqual([
      'msg-user-1',
      'assistant-live',
    ])
  })
})
