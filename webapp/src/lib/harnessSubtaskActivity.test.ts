import { describe, expect, it } from 'vitest'

import type { HarnessMessage, HarnessPart } from '@/types/harness'
import {
  collectRunningChildSessionIds,
  formatSubagentType,
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

describe('formatSubagentType', () => {
  it('maps known subagent ids to display labels', () => {
    expect(formatSubagentType('explore')).toBe('Explorer')
    expect(formatSubagentType('general')).toBe('General')
    expect(formatSubagentType('EXPLORER')).toBe('Explorer')
  })

  it('capitalizes unknown agents and ignores blanks', () => {
    expect(formatSubagentType('research')).toBe('Research')
    expect(formatSubagentType('')).toBeNull()
    expect(formatSubagentType(null)).toBeNull()
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
