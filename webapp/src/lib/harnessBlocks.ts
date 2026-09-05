/**
 * Group harness message parts into render blocks for the chat timeline.
 *
 * Parts stay in chronological order. Consecutive tool/reasoning work items
 * collapse into a "Worked" group when two or more occur in a row.
 */

import type { HarnessPart, HarnessPartType } from '@/types/harness'

const WORK_ITEM_TYPES = new Set<HarnessPartType>(['tool', 'reasoning'])
const CARD_TYPES = new Set<HarnessPartType>(['subtask', 'patch', 'agent'])
const SKIP_TYPES = new Set<HarnessPartType>(['step-start'])

/** A work item that can join a consecutive "Worked" run. */
export function isWorkItem(part: HarnessPart): boolean {
  return WORK_ITEM_TYPES.has(part.type)
}

/** Top-level card parts that break a work run. */
export function isCardPart(part: HarnessPart): boolean {
  return CARD_TYPES.has(part.type)
}

export type TextRenderBlock = { kind: 'text'; part: HarnessPart }
export type SingleRenderBlock = { kind: 'single'; part: HarnessPart }
export type GroupRenderBlock = { kind: 'group'; parts: HarnessPart[] }
export type CardRenderBlock = { kind: 'card'; part: HarnessPart }
export type StepRenderBlock = { kind: 'step'; part: HarnessPart }

export type RenderBlock =
  | TextRenderBlock
  | SingleRenderBlock
  | GroupRenderBlock
  | CardRenderBlock
  | StepRenderBlock

function isEmptyText(part: HarnessPart): boolean {
  return part.type === 'text' && !part.output
}

/**
 * Collapse consecutive tool/reasoning parts (with optional step-finish
 * markers in between) into a group when two or more work items appear.
 */
export function buildRenderBlocks(parts: HarnessPart[]): RenderBlock[] {
  const blocks: RenderBlock[] = []
  let run: HarnessPart[] = []

  function flushRun(): void {
    if (run.length === 0) return
    const workItems = run.filter(isWorkItem)
    if (workItems.length >= 2) {
      blocks.push({ kind: 'group', parts: [...run] })
    } else {
      for (const part of run) {
        if (isWorkItem(part)) {
          blocks.push({ kind: 'single', part })
        } else {
          blocks.push({ kind: 'step', part })
        }
      }
    }
    run = []
  }

  for (const part of parts) {
    if (SKIP_TYPES.has(part.type) || isEmptyText(part)) {
      continue
    }
    if (part.type === 'text') {
      flushRun()
      blocks.push({ kind: 'text', part })
      continue
    }
    if (isWorkItem(part)) {
      run.push(part)
      continue
    }
    if (part.type === 'step-finish') {
      if (run.some(isWorkItem)) {
        run.push(part)
      } else {
        flushRun()
        blocks.push({ kind: 'step', part })
      }
      continue
    }
    flushRun()
    blocks.push({ kind: 'card', part })
  }

  flushRun()
  return blocks
}

/** Number of tool/reasoning rows inside a work run. */
export function countWorkItems(parts: HarnessPart[]): number {
  return parts.filter(isWorkItem).length
}
