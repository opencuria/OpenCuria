import { describe, expect, it } from 'vitest'

import type { HarnessMessage, HarnessPart, HarnessSession } from '@/types/harness'
import {
  collectAncestorSessions,
  collectDescendantSessionIds,
  collectRunningChildSessionIds,
  formatSubagentType,
  gateSourceLabel,
  hasRunningToolOrSubtask,
  isTaskToolPart,
  latestRunningChildTool,
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

describe('collectAncestorSessions', () => {
  it('returns a single session for a root', () => {
    const sessions = [makeSession({ id: 'root', parent_id: null })]
    expect(collectAncestorSessions('root', sessions).map((s) => s.id)).toEqual(['root'])
  })

  it('walks from child to root', () => {
    const sessions = [
      makeSession({ id: 'root', parent_id: null }),
      makeSession({ id: 'child', parent_id: 'root' }),
    ]
    expect(collectAncestorSessions('child', sessions).map((s) => s.id)).toEqual([
      'root',
      'child',
    ])
  })

  it('walks a nested grandchild chain', () => {
    const sessions = [
      makeSession({ id: 'root', parent_id: null }),
      makeSession({ id: 'child', parent_id: 'root' }),
      makeSession({ id: 'grand', parent_id: 'child' }),
    ]
    expect(collectAncestorSessions('grand', sessions).map((s) => s.id)).toEqual([
      'root',
      'child',
      'grand',
    ])
  })

  it('stops when a parent is missing', () => {
    const sessions = [makeSession({ id: 'child', parent_id: 'missing' })]
    expect(collectAncestorSessions('child', sessions).map((s) => s.id)).toEqual(['child'])
  })

  it('is cycle-safe', () => {
    const sessions = [
      makeSession({ id: 'a', parent_id: 'b' }),
      makeSession({ id: 'b', parent_id: 'a' }),
    ]
    expect(collectAncestorSessions('a', sessions).map((s) => s.id)).toEqual(['b', 'a'])
  })

  it('returns an empty list when the session is unknown', () => {
    expect(collectAncestorSessions('missing', [])).toEqual([])
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
