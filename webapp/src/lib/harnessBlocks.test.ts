import { describe, expect, it } from 'vitest'

import type { HarnessPart, HarnessPartType } from '@/types/harness'
import { buildRenderBlocks, countWorkItems, isCardPart, isWorkItem } from './harnessBlocks'

function makePart(
  type: HarnessPartType,
  overrides: Partial<HarnessPart> = {},
): HarnessPart {
  return {
    id: overrides.id ?? `part-${type}`,
    session_id: 'session-1',
    type,
    state: 'completed',
    title: overrides.title ?? type,
    output: overrides.output ?? '',
    ...overrides,
  }
}

describe('buildRenderBlocks', () => {
  it('keeps text, tool, and text in chronological order', () => {
    const parts = [
      makePart('text', { id: 't1', output: 'Hello' }),
      makePart('tool', { id: 'tool-1', title: 'Read a.ts', tool: 'read' }),
      makePart('text', { id: 't2', output: 'Done' }),
    ]

    const blocks = buildRenderBlocks(parts)

    expect(blocks.map((block) => block.kind)).toEqual(['text', 'single', 'text'])
    expect(blocks[0]).toMatchObject({ kind: 'text', part: { id: 't1' } })
    expect(blocks[1]).toMatchObject({ kind: 'single', part: { id: 'tool-1' } })
    expect(blocks[2]).toMatchObject({ kind: 'text', part: { id: 't2' } })
  })

  it('renders a single work item at the top level', () => {
    const parts = [makePart('reasoning', { id: 'r1', output: 'thinking' })]

    expect(buildRenderBlocks(parts)).toEqual([
      { kind: 'single', part: parts[0] },
    ])
  })

  it('groups two or more consecutive work items', () => {
    const parts = [
      makePart('tool', { id: 'tool-1', tool: 'read', title: 'Read a.ts' }),
      makePart('tool', { id: 'tool-2', tool: 'grep', title: 'Grep foo' }),
      makePart('reasoning', { id: 'r1', output: 'hmm' }),
    ]

    const blocks = buildRenderBlocks(parts)

    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toMatchObject({ kind: 'group' })
    if (blocks[0]?.kind === 'group') {
      expect(blocks[0].parts.map((part) => part.id)).toEqual(['tool-1', 'tool-2', 'r1'])
    }
  })

  it('does not count step-finish toward the group threshold', () => {
    const parts = [
      makePart('tool', { id: 'tool-1', tool: 'read' }),
      makePart('step-finish', { id: 'step-1', title: 'Step 1 finished' }),
    ]

    const blocks = buildRenderBlocks(parts)

    expect(blocks.map((block) => block.kind)).toEqual(['single', 'step'])
    expect(blocks[0]).toMatchObject({ kind: 'single', part: { id: 'tool-1' } })
    expect(blocks[1]).toMatchObject({ kind: 'step', part: { id: 'step-1' } })
  })

  it('keeps step-finish inside a group of two work items', () => {
    const parts = [
      makePart('tool', { id: 'tool-1', tool: 'read' }),
      makePart('step-finish', { id: 'step-1' }),
      makePart('tool', { id: 'tool-2', tool: 'grep' }),
    ]

    const blocks = buildRenderBlocks(parts)

    expect(blocks).toHaveLength(1)
    expect(blocks[0]?.kind).toBe('group')
    if (blocks[0]?.kind === 'group') {
      expect(blocks[0].parts.map((part) => part.id)).toEqual([
        'tool-1',
        'step-1',
        'tool-2',
      ])
    }
  })

  it('does not break a run on step-start and skips it', () => {
    const parts = [
      makePart('tool', { id: 'tool-1', tool: 'read' }),
      makePart('step-start', { id: 'start-1' }),
      makePart('tool', { id: 'tool-2', tool: 'grep' }),
    ]

    const blocks = buildRenderBlocks(parts)

    expect(blocks).toHaveLength(1)
    expect(blocks[0]?.kind).toBe('group')
    if (blocks[0]?.kind === 'group') {
      expect(blocks[0].parts.map((part) => part.id)).toEqual(['tool-1', 'tool-2'])
    }
  })

  it('breaks a run on subtask and patch cards', () => {
    const parts = [
      makePart('tool', { id: 'tool-1', tool: 'read' }),
      makePart('subtask', { id: 'sub-1', title: 'explore' }),
      makePart('tool', { id: 'tool-2', tool: 'grep' }),
      makePart('patch', { id: 'patch-1', title: 'Patch a.ts' }),
    ]

    const blocks = buildRenderBlocks(parts)

    expect(blocks.map((block) => block.kind)).toEqual([
      'single',
      'card',
      'single',
      'card',
    ])
    expect(blocks[1]).toMatchObject({ kind: 'card', part: { id: 'sub-1' } })
    expect(blocks[3]).toMatchObject({ kind: 'card', part: { id: 'patch-1' } })
  })

  it('emits a standalone step-finish outside a work run', () => {
    const parts = [makePart('step-finish', { id: 'step-1' })]

    expect(buildRenderBlocks(parts)).toEqual([
      { kind: 'step', part: parts[0] },
    ])
  })

  it('skips empty text parts', () => {
    const parts = [
      makePart('text', { id: 'empty', output: '' }),
      makePart('tool', { id: 'tool-1', tool: 'read' }),
    ]

    const blocks = buildRenderBlocks(parts)
    expect(blocks.map((block) => block.kind)).toEqual(['single'])
  })

  it('splits work runs around text', () => {
    const parts = [
      makePart('tool', { id: 'tool-1', tool: 'read' }),
      makePart('tool', { id: 'tool-2', tool: 'grep' }),
      makePart('text', { id: 't1', output: 'mid' }),
      makePart('tool', { id: 'tool-3', tool: 'list' }),
    ]

    const blocks = buildRenderBlocks(parts)
    expect(blocks.map((block) => block.kind)).toEqual(['group', 'text', 'single'])
  })
})

describe('work item helpers', () => {
  it('identifies work items and card parts', () => {
    expect(isWorkItem(makePart('tool'))).toBe(true)
    expect(isWorkItem(makePart('reasoning'))).toBe(true)
    expect(isWorkItem(makePart('text'))).toBe(false)
    expect(isCardPart(makePart('subtask'))).toBe(true)
    expect(isCardPart(makePart('patch'))).toBe(true)
    expect(isCardPart(makePart('tool'))).toBe(false)
  })

  it('counts only tool and reasoning parts', () => {
    const parts = [
      makePart('tool'),
      makePart('step-finish'),
      makePart('reasoning'),
    ]
    expect(countWorkItems(parts)).toBe(2)
  })
})
