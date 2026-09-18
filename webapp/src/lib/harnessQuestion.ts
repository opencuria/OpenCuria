/**
 * Parsing helpers for answered `question` / `ask_user` tool calls.
 *
 * The backend persists the asked questions in `input.arguments.questions`
 * (mirrored into `meta.questions` on live parts) and the user answers as
 * `{"answers": [...]}` in `output` (mirrored into `meta.answers`). Skipped
 * or timed-out questions land as `error` parts whose output carries the
 * failure reason. Extracted from `ToolDetailQuestion.vue` so both the chat
 * card and tests share one implementation.
 */

import type { HarnessPart } from '@/types/harness'
import { parseToolArguments } from '@/lib/toolDisplay'

export interface HarnessQuestionRow {
  question: string
  answer: string
}

function asQuestionList(value: unknown): { question: string }[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const question = (item as { question?: unknown }).question
    if (typeof question !== 'string' || !question) return []
    return [{ question }]
  })
}

function asAnswerList(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.map((item) => (typeof item === 'string' ? item : JSON.stringify(item)))
}

/** Pair asked questions with their answers (index-aligned). */
export function parseQuestionRows(part: HarnessPart): HarnessQuestionRow[] {
  const args = parseToolArguments(part)
  const questions = asQuestionList(part.meta?.['questions'] ?? args.questions)
  let answers = asAnswerList(part.meta?.['answers'])
  if (answers.length === 0 && part.output) {
    try {
      const parsed: unknown = JSON.parse(part.output)
      if (parsed && typeof parsed === 'object' && 'answers' in parsed) {
        answers = asAnswerList((parsed as { answers: unknown }).answers)
      }
    } catch {
      answers = []
    }
  }
  if (questions.length === 0 && answers.length === 0) return []
  if (questions.length === 0) {
    return answers.map((answer) => ({ question: 'Answer', answer }))
  }
  return questions.map((item, index) => ({
    question: item.question,
    answer: answers[index] ?? '',
  }))
}

export type QuestionCardTone = 'ok' | 'skipped' | 'failed'

export interface QuestionCardStatus {
  tone: QuestionCardTone
  /** Badge label; null when answered normally. */
  label: string | null
  /** Short failure reason for error parts (args suffix stripped). */
  detail: string | null
}

/** Strip the `Tool 'question' failed: … (args: …)` wrapper for compact display. */
export function cleanQuestionError(output: string): string {
  let text = (output || '').trim()
  text = text.replace(/^Tool 'question' failed:\s*/i, '')
  text = text.replace(/\s*\(args:.*\)\s*$/s, '').trim()
  return text
}

/** Card status: answered parts show no badge, error parts map to Skip/Timeout/Failed. */
export function questionCardStatus(part: HarnessPart): QuestionCardStatus {
  if (part.state !== 'error') {
    return { tone: 'ok', label: null, detail: null }
  }
  const detail = cleanQuestionError(part.output)
  const lowered = detail.toLowerCase()
  if (/reject|cancel|skip|abort/.test(lowered)) {
    return { tone: 'skipped', label: 'Skipped', detail: detail || null }
  }
  if (/timed?\s*out|timeout/.test(lowered)) {
    return { tone: 'skipped', label: 'Timed out', detail: detail || null }
  }
  return { tone: 'failed', label: 'Failed', detail: detail || null }
}
