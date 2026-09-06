import { describe, expect, it } from 'vitest'

import type { HarnessMessage, HarnessPart, HarnessSession } from '@/types/harness'
import {
  collectDescendantSessionIds,
  collectRunningChildSessionIds,
  formatSubagentType,
  gateSourceLabel,
  hasRunningToolOrSubtask,
  isTaskToolPart,
  latestRunningChildTool,
  resolveChildSessionId,
  buildChildSessionIdMap,
  subtaskActivityLabel,
} from './harnessSubtaskActivity'

function makePart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-1',
    session_id: 'session-1',
    type: 'tool',
    state: 'running',
    title: '',
    output: '',
    ...overrides,
  }
}

function makeMessage(parts: HarnessPart[]): HarnessMessage {
  return {
    id: 'msg-1',
    session_id: 'session-child',
    role: 'assistant',
    content: '',
    parts,
  }
}

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

describe('formatSubagentType', () => {
  it('maps known subagent ids to display labels', () => {
    expect(formatSubagentType('explore')).toBe('Explorer')
    expect(formatSubagentType('general')).toBe('General')
    expect(formatSubagentType('computeruse')).toBe('Computer use')
    expect(formatSubagentType('EXPLORER')).toBe('Explorer')
  })

  it('capitalizes unknown agents and ignores blanks', () => {
    expect(formatSubagentType('research')).toBe('Research')
    expect(formatSubagentType('')).toBeNull()
    expect(formatSubagentType(null)).toBeNull()
  })
})

describe('gateSourceLabel', () => {
  it('returns display labels only for subagent types', () => {
    expect(gateSourceLabel('explore')).toBe('Explorer')
    expect(gateSourceLabel('general')).toBe('General')
    expect(gateSourceLabel('computeruse')).toBe('Computer use')
    expect(gateSourceLabel('build')).toBeNull()
    expect(gateSourceLabel('plan')).toBeNull()
    expect(gateSourceLabel('')).toBeNull()
  })
})

describe('collectDescendantSessionIds', () => {
  it('includes the root, children, and nested descendants', () => {
    const sessions = [
      makeSession({ id: 'root', parent_id: null }),
      makeSession({ id: 'child-a', parent_id: 'root' }),
      makeSession({ id: 'child-b', parent_id: 'root' }),
      makeSession({ id: 'grand', parent_id: 'child-a' }),
      makeSession({ id: 'other', parent_id: null }),
      makeSession({ id: 'other-child', parent_id: 'other' }),
    ]
    expect(collectDescendantSessionIds('root', sessions)).toEqual([
      'root',
      'child-a',
      'child-b',
      'grand',
    ])
    expect(collectDescendantSessionIds('child-a', sessions)).toEqual(['child-a', 'grand'])
  })
})

describe('isTaskToolPart', () => {
  it('detects task tools by name, input, or Subagent title', () => {
    expect(isTaskToolPart(makePart({ tool: 'task' }))).toBe(true)
    expect(isTaskToolPart(makePart({ tool: 'read' }))).toBe(false)
    expect(
      isTaskToolPart(makePart({ tool: undefined, input: { tool: 'task' } })),
    ).toBe(true)
    expect(
      isTaskToolPart(makePart({ tool: 'read', title: 'Subagent: find renderer' })),
    ).toBe(true)
    expect(isTaskToolPart(makePart({ type: 'subtask' }))).toBe(false)
  })
})

describe('latestRunningChildTool', () => {
  it('returns the newest running tool part', () => {
    const messages = [
      makeMessage([
        makePart({ id: 'old', state: 'completed', title: 'Read a.ts', tool: 'read' }),
        makePart({ id: 'live', state: 'running', title: 'Grep foo', tool: 'grep' }),
      ]),
    ]
    expect(latestRunningChildTool(messages)?.id).toBe('live')
  })

  it('ignores reasoning and completed tools', () => {
    const messages = [
      makeMessage([
        makePart({
          id: 'r1',
          type: 'reasoning',
          state: 'running',
          title: '',
        }),
        makePart({
          id: 'done',
          type: 'tool',
          state: 'completed',
          title: 'Read a.ts',
        }),
      ]),
    ]
    expect(latestRunningChildTool(messages)).toBeNull()
  })
})

describe('subtaskActivityLabel', () => {
  const subtask = makePart({ type: 'subtask', title: 'Find renderer' })

  it('uses Completed / Failed for finished states', () => {
    expect(subtaskActivityLabel({ ...subtask, state: 'completed' }, [])).toBe(
      'Completed',
    )
    expect(subtaskActivityLabel({ ...subtask, state: 'error' }, [])).toBe('Failed')
  })

  it('returns the running child tool title, else null', () => {
    expect(subtaskActivityLabel({ ...subtask, state: 'running' }, [])).toBeNull()
    expect(
      subtaskActivityLabel(
        { ...subtask, state: 'running' },
        [
          makeMessage([
            makePart({ title: 'Read renderer.ts', tool: 'read', state: 'running' }),
          ]),
        ],
      ),
    ).toBe('Read renderer.ts')
  })
})

describe('hasRunningToolOrSubtask', () => {
  it('treats running tools and subtasks as active work', () => {
    expect(hasRunningToolOrSubtask([makePart({ type: 'tool', state: 'running' })])).toBe(
      true,
    )
    expect(
      hasRunningToolOrSubtask([makePart({ type: 'subtask', state: 'running' })]),
    ).toBe(true)
    expect(
      hasRunningToolOrSubtask([makePart({ type: 'reasoning', state: 'running' })]),
    ).toBe(false)
    expect(
      hasRunningToolOrSubtask([makePart({ type: 'tool', state: 'completed' })]),
    ).toBe(false)
  })
})

describe('collectRunningChildSessionIds', () => {
  it('collects unique child ids from running subtasks', () => {
    const messages = [
      makeMessage([
        makePart({
          type: 'subtask',
          state: 'running',
          meta: { child_session_id: 'child-a' },
        }),
        makePart({
          type: 'subtask',
          state: 'completed',
          meta: { child_session_id: 'child-done' },
        }),
        makePart({
          type: 'subtask',
          state: 'running',
          meta: { child_session_id: 'child-a' },
        }),
        makePart({
          type: 'subtask',
          state: 'running',
          meta: { child_session_id: 'child-b' },
        }),
      ]),
    ]
    expect(collectRunningChildSessionIds(messages)).toEqual(['child-a', 'child-b'])
  })
})

describe('resolveChildSessionId', () => {
  const child = {
    id: 'session-child',
    workspace_id: 'ws-1',
    parent_id: 'session-parent',
    title: 'Webapp Login und Dashboard testen',
    mode: 'build' as const,
    agent_name: 'computeruse',
    model: 'm',
    status: 'idle' as const,
    cost: 0,
    tokens: {},
  }

  it('prefers meta.child_session_id', () => {
    expect(
      resolveChildSessionId(
        makePart({
          type: 'subtask',
          session_id: 'session-parent',
          title: 'Webapp Login und Dashboard testen',
          meta: { child_session_id: 'from-meta', subtask_id: 'sub-1' },
        }),
        [child],
      ),
    ).toBe('from-meta')
  })

  it('falls back to a unique matching child title', () => {
    expect(
      resolveChildSessionId(
        makePart({
          type: 'subtask',
          session_id: 'session-parent',
          title: 'Webapp Login und Dashboard testen',
          meta: { subtask_id: 'sub-1' },
        }),
        [child],
      ),
    ).toBe('session-child')
  })

  it('uses the only child of the parent when titles were rewritten', () => {
    expect(
      resolveChildSessionId(
        makePart({
          type: 'subtask',
          session_id: 'session-parent',
          title: 'Webapp Login und Dashboard testen',
          meta: { subtask_id: 'sub-1' },
        }),
        [{ ...child, title: 'Generated login test title' }],
      ),
    ).toBe('session-child')
  })

  it('returns null when several children share the same title', () => {
    expect(
      resolveChildSessionId(
        makePart({
          type: 'subtask',
          session_id: 'session-parent',
          title: 'Webapp Login und Dashboard testen',
          meta: { subtask_id: 'sub-1' },
        }),
        [child, { ...child, id: 'session-child-2' }],
      ),
    ).toBeNull()
  })
})

describe('buildChildSessionIdMap', () => {
  it('maps subtask id and part id for legacy parts without child_session_id', () => {
    const map = buildChildSessionIdMap(
      [
        {
          id: 'session-parent',
          workspace_id: 'ws-1',
          parent_id: null,
          title: 'parent',
          mode: 'build',
          agent_name: 'build',
          model: 'm',
          status: 'idle',
          cost: 0,
          tokens: {},
        },
        {
          id: 'session-child',
          workspace_id: 'ws-1',
          parent_id: 'session-parent',
          title: 'Find renderer',
          mode: 'build',
          agent_name: 'explore',
          model: 'm',
          status: 'idle',
          cost: 0,
          tokens: {},
        },
      ],
      {
        'session-parent': [
          {
            id: 'msg-1',
            session_id: 'session-parent',
            role: 'assistant',
            content: '',
            parts: [
              makePart({
                id: 'part-sub-1',
                type: 'subtask',
                session_id: 'session-parent',
                title: 'Find renderer',
                meta: { subtask_id: 'sub-1' },
              }),
            ],
          },
        ],
      },
    )

    expect(map['sub-1']).toBe('session-child')
    expect(map['part-sub-1']).toBe('session-child')
  })

  it('pairs leftover subtasks with children in creation order', () => {
    const map = buildChildSessionIdMap(
      [
        {
          id: 'session-parent',
          workspace_id: 'ws-1',
          parent_id: null,
          title: 'parent',
          mode: 'build',
          agent_name: 'build',
          model: 'm',
          status: 'idle',
          cost: 0,
          tokens: {},
        },
        {
          id: 'child-a',
          workspace_id: 'ws-1',
          parent_id: 'session-parent',
          title: 'Generated A',
          mode: 'build',
          agent_name: 'explore',
          model: 'm',
          status: 'idle',
          cost: 0,
          tokens: {},
          created_at: '2026-01-01T10:00:00.000Z',
        },
        {
          id: 'child-b',
          workspace_id: 'ws-1',
          parent_id: 'session-parent',
          title: 'Generated B',
          mode: 'build',
          agent_name: 'general',
          model: 'm',
          status: 'idle',
          cost: 0,
          tokens: {},
          created_at: '2026-01-01T10:00:01.000Z',
        },
      ],
      {
        'session-parent': [
          {
            id: 'msg-1',
            session_id: 'session-parent',
            role: 'assistant',
            content: '',
            parts: [
              makePart({
                id: 'part-a',
                type: 'subtask',
                session_id: 'session-parent',
                title: 'Explore renderer',
                meta: { subtask_id: 'sub-a' },
              }),
              makePart({
                id: 'part-b',
                type: 'subtask',
                session_id: 'session-parent',
                title: 'General research',
                meta: { subtask_id: 'sub-b' },
              }),
            ],
          },
        ],
      },
    )

    expect(map['sub-a']).toBe('child-a')
    expect(map['part-a']).toBe('child-a')
    expect(map['sub-b']).toBe('child-b')
    expect(map['part-b']).toBe('child-b')
  })
})
