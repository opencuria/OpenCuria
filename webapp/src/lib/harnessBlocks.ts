/**
 * Group harness message parts into render blocks for the chat timeline.
 *
 * Parts stay in chronological order. Consecutive simple tool calls collapse
 * into a "Worked" group. Reasoning and failed tools break the run and render
 * as standalone rows. Subtasks, patches, and answered questions stay as
 * top-level cards. Pending questions stay invisible (they live in the
 * composer sheet) and only appear as cards once answered.
 * After a turn finishes, wrapFinishedWork wraps the flat chronological
 * timeline into "Worked for" card(s) from the first response block
 * (initial text/reasoning plus interleaved text/tools) through the last
 * tool/subagent and its result/patch — tools are never hoisted above
 * intervening text, and only trailing text after the last tool stays
 * outside. Compaction dividers move inside to honor chronology unless
 * they lead the turn before any response. Agent-S plans are the
 * computer-use timeline and always stay visible outside; each response
 * slice between plans keeps its own chronological card.
 */

import type { HarnessPart, HarnessPartType } from '@/types/harness'
import { isTaskToolPart } from '@/lib/harnessSubtaskActivity'
import { resolveToolName } from '@/lib/toolDisplay'

const GROUPABLE_TYPES = new Set<HarnessPartType>(['tool'])
const CARD_TYPES = new Set<HarnessPartType>(['subtask', 'patch'])
const AGENT_TYPES = new Set<HarnessPartType>(['agent'])
const SKIP_TYPES = new Set<HarnessPartType>(['step-start', 'step-finish'])
const PATCHED_FILE_TOOLS = new Set(['edit', 'write'])
const QUESTION_TOOLS = new Set(['question', 'ask_user'])

/**
 * A question tool call (`question` / `ask_user`). Pending ones stay
 * invisible (composer sheet owns them); answered ones render as cards.
 */
export function isQuestionToolPart(part: HarnessPart): boolean {
  if (part.type !== 'tool') return false
  return QUESTION_TOOLS.has(resolveToolName(part).toLowerCase())
}

/** True while the question is still open (no answers yet). */
export function isPendingQuestionPart(part: HarnessPart): boolean {
  return isQuestionToolPart(part) && (part.state === 'running' || part.state === 'pending')
}

/** A simple tool call that can join a consecutive "Worked" run. */
export function isWorkItem(part: HarnessPart): boolean {
  if (isQuestionToolPart(part)) return false
  return GROUPABLE_TYPES.has(part.type) && part.state !== 'error'
}

/** Top-level card parts that break a work run. */
export function isCardPart(part: HarnessPart): boolean {
  return CARD_TYPES.has(part.type)
}

/** Agent-S plan steps render as their own timeline blocks. */
export function isAgentPart(part: HarnessPart): boolean {
  return AGENT_TYPES.has(part.type)
}

export type TextRenderBlock = { kind: 'text'; part: HarnessPart }
export type SingleRenderBlock = { kind: 'single'; part: HarnessPart }
export type GroupRenderBlock = { kind: 'group'; parts: HarnessPart[] }
export type CardRenderBlock = { kind: 'card'; part: HarnessPart }
export type AgentRenderBlock = { kind: 'agent'; part: HarnessPart }
export type CompactionRenderBlock = { kind: 'compaction'; part: HarnessPart }
export type WorkedForRenderBlock = { kind: 'workedFor'; blocks: RenderBlock[] }

export type RenderBlock =
  | TextRenderBlock
  | SingleRenderBlock
  | GroupRenderBlock
  | CardRenderBlock
  | AgentRenderBlock
  | CompactionRenderBlock

/** Top-level blocks after a finished turn may wrap work in `workedFor`. */
export type MessageRenderBlock = RenderBlock | WorkedForRenderBlock

function isEmptyText(part: HarnessPart): boolean {
  return part.type === 'text' && !part.output
}

function isStandaloneWork(part: HarnessPart): boolean {
  if (isQuestionToolPart(part)) return false
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
    if (isPendingQuestionPart(part)) {
      // Still open: the composer sheet owns the interaction, no chat row.
      continue
    }
    if (isQuestionToolPart(part)) {
      // Answered/skipped/failed: compact Q/A card on patch level.
      flushRun()
      blocks.push({ kind: 'card', part })
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
    if (part.type === 'agent') {
      // Agent-S plan steps break tool runs so consecutive steps stay a
      // visible vertical sequence instead of merging into Worked groups.
      flushRun()
      blocks.push({ kind: 'agent', part })
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
  return block.kind !== 'text' && block.kind !== 'compaction' && block.kind !== 'agent'
}

/**
 * Wrap the flat chronological timeline into "Worked for" card(s) from the
 * first response block through the last work block, preserving strict
 * chronological order (never hoist tools above intervening text).
 *
 * - No work → no card (text-only, agent-only, compaction-only stay flat).
 * - Each card spans blocks[firstResponse .. lastWork] where firstResponse
 *   is the first text/work response block and lastWork is the final
 *   single/group/card (tool, reasoning, error, subtask/patch/question
 *   card). Inner text/compaction between response blocks stays inside in
 *   order; a leading compaction divider before any response stays outside.
 * - Agent-S plans are the computer-use timeline and always stay visible
 *   outside, so the timeline splits at each plan and every response slice
 *   keeps its own chronological card. Only trailing blocks after the last
 *   work (typically the final answer text) stay outside after the card.
 */
export function wrapFinishedWork(blocks: RenderBlock[]): MessageRenderBlock[] {
  // Agent-S plans are the computer-use timeline: they always stay visible
  // outside the collapsed card. Split the flat timeline at each plan and
  // wrap every response slice (first response .. last work) separately so
  // tools never hoist above intervening blocks, plans never hide inside
  // the card, and order stays strictly chronological. Compaction dividers
  // between response blocks move inside to honor chronology; a leading
  // divider before any response text/work stays outside.
  if (!blocks.some((block) => block.kind === 'agent')) {
    return wrapWorkSlice(blocks)
  }
  const top: MessageRenderBlock[] = []
  let slice: RenderBlock[] = []
  function flushSlice(): void {
    if (slice.length === 0) return
    const wrapped = wrapWorkSlice(slice)
    top.push(...wrapped)
    slice = []
  }
  for (const block of blocks) {
    if (block.kind === 'agent') {
      flushSlice()
      top.push(block)
      continue
    }
    slice.push(block)
  }
  flushSlice()
  return top
}

/** Wrap one agent-free slice from its first response through its last work. */
function wrapWorkSlice(blocks: RenderBlock[]): MessageRenderBlock[] {
  let lastWork = -1
  for (let index = 0; index < blocks.length; index += 1) {
    if (isOuterWorkBlock(blocks[index]!)) lastWork = index
  }
  if (lastWork === -1) return blocks
  let start = -1
  for (let index = 0; index <= lastWork; index += 1) {
    const block = blocks[index]!
    if (block.kind === 'text' || isOuterWorkBlock(block)) {
      start = index
      break
    }
  }
  // Work exists but no text/work response precedes it (only leading
  // compaction): wrap from the first work block itself.
  if (start === -1) start = lastWork
  const inner = blocks.slice(start, lastWork + 1)
  if (inner.length === 0) return blocks
  const shell: WorkedForRenderBlock = { kind: 'workedFor', blocks: inner }
  return [...blocks.slice(0, start), shell, ...blocks.slice(lastWork + 1)]
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
