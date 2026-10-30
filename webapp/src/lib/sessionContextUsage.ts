/**
 * Session context-window occupancy for the composer usage ring and sheet.
 */

import type { HarnessMessage, HarnessPart } from '@/types/harness'

function asFiniteNumber(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

function tokensFromRecord(record: Record<string, unknown> | undefined): {
  prompt: number
  completion: number
} {
  if (!record) return { prompt: 0, completion: 0 }
  return {
    prompt: asFiniteNumber(record['prompt'] ?? record['prompt_tokens']),
    completion: asFiniteNumber(record['completion'] ?? record['completion_tokens']),
  }
}

function tokensFromStepFinish(part: HarnessPart): { prompt: number; completion: number } {
  // Live/full parts carry meta; compact timeline snapshots carry display.
  const tokens = part.meta?.['tokens'] ?? part.display?.['tokens']
  return tokensFromRecord(
    tokens && typeof tokens === 'object' ? (tokens as Record<string, unknown>) : undefined,
  )
}

/** Current runtime context, falling back to the last completed model step. */
export function resolveMessageContextTokens(message: HarnessMessage): {
  prompt: number
  completion: number
  used: number
  limit?: number
} {
  for (let index = message.parts.length - 1; index >= 0; index -= 1) {
    const part = message.parts[index]!
    if (part.type !== 'step-start' && part.type !== 'step-finish') continue
    const context = part.meta?.['context'] ?? part.display?.['context']
    if (!context || typeof context !== 'object') continue
    const record = context as Record<string, unknown>
    const tokens = tokensFromRecord(record)
    const used = asFiniteNumber(record['total_tokens']) || tokens.prompt + tokens.completion
    if (used <= 0) continue
    const limit = asFiniteNumber(record['context_length'])
    return {
      ...tokens,
      used,
      ...(limit > 0 ? { limit } : {}),
    }
  }
  const stepFinishes = message.parts.filter((part) => part.type === 'step-finish')
  const tokens =
    stepFinishes.length > 0
      ? tokensFromStepFinish(stepFinishes[stepFinishes.length - 1]!)
      : tokensFromRecord(message.tokens)
  return {
    prompt: tokens.prompt,
    completion: tokens.completion,
    used: tokens.prompt + tokens.completion,
  }
}

/** Used tokens from the last assistant message that reports usage. */
export function resolveSessionUsedTokens(messages: HarnessMessage[]): {
  used: number
  promptTokens: number
  completionTokens: number
  limit?: number
} {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (!message || message.role !== 'assistant') continue
    const tokens = resolveMessageContextTokens(message)
    if (tokens.used > 0) {
      return {
        used: tokens.used,
        promptTokens: tokens.prompt,
        completionTokens: tokens.completion,
        ...(tokens.limit ? { limit: tokens.limit } : {}),
      }
    }
  }
  return { used: 0, promptTokens: 0, completionTokens: 0 }
}

/** Compact token count label (e.g. `82.2K`, `1.0M`). */
export function formatTokenCount(value: number): string {
  const amount = Math.max(0, value)
  if (amount >= 1_000_000) return `${(amount / 1_000_000).toFixed(1)}M`
  if (amount >= 1_000) return `${(amount / 1_000).toFixed(1)}K`
  return String(Math.round(amount))
}
