/**
 * Group harness message parts into render blocks for the chat timeline.
 *
 * Parts stay in chronological order. Consecutive simple tool calls collapse
 * into a "Worked" group. Reasoning and failed tools break the run and render
 * as standalone rows. Subtasks and patches stay as top-level cards.
 * After a turn finishes, wrapFinishedWork scoops work blocks into one
 * "Worked for" shell; text and compaction stay outside.
 */

import type { HarnessPart, HarnessPartType } from '@/types/harness'
import { isTaskToolPart } from '@/lib/harnessSubtaskActivity'
import { resolveToolName } from '@/lib/toolDisplay'

const GROUPABLE_TYPES = new Set<HarnessPartType>(['tool'])
const CARD_TYPES = new Set<HarnessPartType>(['subtask', 'patch', 'agent'])
const SKIP_TYPES = new Set<HarnessPartType>(['step-start', 'step-finish'])
const PATCHED_FILE_TOOLS = new Set(['edit', 'write'])

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
export type WorkedForRenderBlock = { kind: 'workedFor'; blocks: RenderBlock[] }

export type RenderBlock =
  | TextRenderBlock
  | SingleRenderBlock
  | GroupRenderBlock
  | CardRenderBlock
  | CompactionRenderBlock

/** Top-level blocks after a finished turn may wrap work in `workedFor`. */
export type MessageRenderBlock = RenderBlock | WorkedForRenderBlock

function isEmptyText(part: HarnessPart): boolean {
  return part.type === 'text' && !part.output
}

function isStandaloneWork(part: HarnessPart): boolean {
  return part.type === 'reasoning' || (part.type === 'tool' && part.state === 'error')
}

function patchCallIds(parts: HarnessPart[]): Set<string> {
  const ids = new Set<string>()
  for (const part of parts) {
    const callId = (part.call_id || '').trim()
    if (part.type === 'patch' && callId) ids.add(callId)
  }
  return ids
}

/** Successful edit/write whose patch card already represents the same call. */
function isPatchedFileTool(part: HarnessPart, patchedCalls: Set<string>): boolean {
  if (part.type !== 'tool' || part.state === 'error') return false
  const tool = resolveToolName(part).toLowerCase()
  if (!PATCHED_FILE_TOOLS.has(tool)) return false
  const callId = (part.call_id || '').trim()
  return Boolean(callId) && patchedCalls.has(callId)
}

/**
 * Collapse consecutive successful/running tool parts into a group when two
 * or more appear in a row. Reasoning, errors, text, and cards flush the run.
 */
export function buildRenderBlocks(parts: HarnessPart[]): RenderBlock[] {
  const blocks: RenderBlock[] = []
  let run: HarnessPart[] = []
  const hasSubtask = parts.some((part) => part.type === 'subtask')
  const patchedCalls = patchCallIds(parts)

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
    if (
      SKIP_TYPES.has(part.type) ||
      isEmptyText(part) ||
      isPatchedFileTool(part, patchedCalls)
    ) {
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

function isOuterWorkBlock(block: RenderBlock): boolean {
  return block.kind !== 'text' && block.kind !== 'compaction'
}

/**
 * Scoop every work block into one `workedFor` shell at the first work
 * position. Text and compaction stay in chronological order around it.
 */
export function wrapFinishedWork(blocks: RenderBlock[]): MessageRenderBlock[] {
  const work: RenderBlock[] = []
  const top: MessageRenderBlock[] = []
  let inserted = false
  for (const block of blocks) {
    if (!isOuterWorkBlock(block)) {
      top.push(block)
      continue
    }
    work.push(block)
    if (!inserted) {
      top.push({ kind: 'workedFor', blocks: work })
      inserted = true
    }
  }
  return top
}

/** Build timeline blocks, wrapping work after the assistant turn is idle. */
export function buildMessageBlocks(
  parts: HarnessPart[],
  options: { finished?: boolean } = {},
): MessageRenderBlock[] {
  const blocks = buildRenderBlocks(parts)
  if (!options.finished) return blocks
  return wrapFinishedWork(blocks)
}

/** Number of grouped tool rows inside a work run. */
export function countWorkItems(parts: HarnessPart[]): number {
  return parts.filter(isWorkItem).length
}
