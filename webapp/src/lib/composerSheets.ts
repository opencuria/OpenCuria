import type { HarnessPermissionRequest, HarnessQuestionRequest, HarnessTodo } from '@/types/harness'
import type { MentionCandidate } from '@/lib/harnessMentions'

/**
 * One entry of the composer sheet stack.
 *
 * Sheets are ordered by interaction priority (highest first): `mention` >
 * `question` > `permission` > `notice` > `processes` > `context` > `todos`.
 * Only the topmost sheet is interactive; lower sheets render as
 * non-interactive peek edges, iOS sheet-stack style.
 */
export type ComposerSheetKind =
  | 'mention'
  | 'question'
  | 'permission'
  | 'notice'
  | 'processes'
  | 'context'
  | 'todos'

export interface ContextSheetState {
  used: number
  limit: number
  percent: number
  promptTokens?: number
  completionTokens?: number
}

export interface NoticeSheetState {
  messageId: string
  text: string
  tone: 'error' | 'info'
}

export interface MentionSheetState {
  candidates: MentionCandidate[]
  activeIndex: number
}

export interface ComposerSheet {
  kind: ComposerSheetKind
  mention?: MentionSheetState
  question?: HarnessQuestionRequest
  /** All pending question requests (for the `i of N` pager). */
  questions?: HarnessQuestionRequest[]
  permission?: HarnessPermissionRequest
  /** All pending permission requests (for the `i of N` pager). */
  permissions?: HarnessPermissionRequest[]
  notice?: NoticeSheetState
  todos?: HarnessTodo[]
  context?: ContextSheetState
}

export interface ComposerSheetInput {
  mention?: MentionSheetState | null
  questions?: HarnessQuestionRequest[]
  permissions?: HarnessPermissionRequest[]
  notice?: NoticeSheetState | null
  todos?: HarnessTodo[]
  processesOpen?: boolean
  contextOpen?: boolean
  context?: ContextSheetState | null
}

/** Priority rank: lower order renders on top of the stack. */
const SHEET_ORDER: Record<ComposerSheetKind, number> = {
  mention: 0,
  question: 1,
  permission: 2,
  notice: 3,
  processes: 4,
  context: 5,
  todos: 6,
}

/**
 * Build the ordered composer sheet stack from the currently active
 * overlays. Empty sources contribute no sheet.
 */
export function buildComposerSheets(input: ComposerSheetInput): ComposerSheet[] {
  const sheets: ComposerSheet[] = []
  if (input.mention && input.mention.candidates.length > 0) {
    sheets.push({ kind: 'mention', mention: input.mention })
  }
  const questions = (input.questions ?? []).filter((request) => request.questions.length > 0)
  if (questions.length > 0) {
    sheets.push({ kind: 'question', question: questions[0], questions })
  }
  const permissions = input.permissions ?? []
  if (permissions.length > 0 && permissions[0]) {
    sheets.push({ kind: 'permission', permission: permissions[0], permissions })
  }
  if (input.notice) {
    sheets.push({ kind: 'notice', notice: input.notice })
  }
  if (input.processesOpen) {
    sheets.push({ kind: 'processes' })
  }
  if (input.contextOpen && input.context) {
    sheets.push({ kind: 'context', context: input.context })
  }
  if ((input.todos ?? []).length > 0) {
    sheets.push({ kind: 'todos', todos: input.todos })
  }
  sheets.sort((a, b) => SHEET_ORDER[a.kind] - SHEET_ORDER[b.kind])
  return sheets
}

/** Letter label for option rows (`A`, `B`, …, `Z`, `AA`, …). */
export function optionLetter(index: number): string {
  let label = ''
  let n = index
  do {
    label = String.fromCharCode(65 + (n % 26)) + label
    n = Math.floor(n / 26) - 1
  } while (n >= 0)
  return label
}
