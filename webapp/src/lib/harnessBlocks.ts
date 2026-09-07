/**
 * Group harness message parts into render blocks for the chat timeline.
 *
 * Parts stay in chronological order. Consecutive simple tool calls collapse
 * into a "Worked" group. Reasoning and failed tools break the run and render
 * as standalone rows. Subtasks and patches stay as top-level cards.
 */

import type { HarnessPart, HarnessPartType } from '@/types/harness'
import { isTaskToolPart } from '@/lib/harnessSubtaskActivity'

const GROUPABLE_TYPES = new Set<HarnessPartType>(['tool'])
const CARD_TYPES = new Set<HarnessPartType>(['subtask', 'patch', 'agent'])
const SKIP_TYPES = new Set<HarnessPartType>(['step-start', 'step-finish'])

/** A simple tool call that can join a consecutive "Worked" run. */
export function isWorkItem(part: HarnessPart): boolean {
  return GROUPABLE_TYPES.has(part.type) && part.state !== 'error'
}

/** Top-level card parts that break a work run. */
export function isCardPart(part: HarnessPart): boolean {
  return CARD_TYPES.has(part.type)
}

export type TextRenderBlock = { kind: 'text'; part: HarnessPart }
export type SingleRenderBlock = { kind: 'single'; part: HarnessPart }
export type GroupRenderBlock = { kind: 'group'; parts: HarnessPart[] }
export type CardRenderBlock = { kind: 'card'; part: HarnessPart }
export type CompactionRenderBlock = { kind: 'compaction'; part: HarnessPart }

export type RenderBlock =
  | TextRenderBlock
  | SingleRenderBlock
  | GroupRenderBlock
  | CardRenderBlock
  | CompactionRenderBlock

function isEmptyText(part: HarnessPart): boolean {
  return part.type === 'text' && !part.output
}

function isStandaloneWork(part: HarnessPart): boolean {
  return part.type === 'reasoning' || (part.type === 'tool' && part.state === 'error')
}

/**
 * Collapse consecutive successful/running tool parts into a group when two
 * or more appear in a row. Reasoning, errors, text, and cards flush the run.
 */
export function buildRenderBlocks(parts: HarnessPart[]): RenderBlock[] {
  const blocks: RenderBlock[] = []
  let run: HarnessPart[] = []
  const hasSubtask = parts.some((part) => part.type === 'subtask')

  function flushRun(): void {
    if (run.length === 0) return
    if (run.length >= 2) {
      blocks.push({ kind: 'group', parts: [...run] })
    } else {
      blocks.push({ kind: 'single', part: run[0]! })
    }
    run = []
  }

  for (const part of parts) {
    if (SKIP_TYPES.has(part.type) || isEmptyText(part)) {
      continue
    }
    if (hasSubtask && isTaskToolPart(part)) {
      continue
    }
    if (part.type === 'text') {
      flushRun()
      blocks.push({ kind: 'text', part })
      continue
    }
    if (part.type === 'compaction') {
      flushRun()
      blocks.push({ kind: 'compaction', part })
      continue
    }
    if (isStandaloneWork(part)) {
      flushRun()
      blocks.push({ kind: 'single', part })
      continue
    }
    if (isWorkItem(part)) {
      run.push(part)
      continue
    }
    flushRun()
    blocks.push({ kind: 'card', part })
  }

  flushRun()
  return blocks
}

/** Number of grouped tool rows inside a work run. */
export function countWorkItems(parts: HarnessPart[]): number {
  return parts.filter(isWorkItem).length
}
