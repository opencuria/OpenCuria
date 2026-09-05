/**
 * Session context-window occupancy for the composer usage ring and sheet.
 */

import { resolveCatalogModel, type ProviderModel } from '@/lib/harnessModels'
import type { HarnessMessage, HarnessPart } from '@/types/harness'

export interface SessionContextUsage {
  used: number
  limit: number
  percent: number
  promptTokens: number
  completionTokens: number
}

export interface SessionContextUsageInput {
  messages: HarnessMessage[]
  models: ProviderModel[]
  modelId: string
  defaultModel?: string
}

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
  const meta = part.meta ?? {}
  return tokensFromRecord(
    meta['tokens'] && typeof meta['tokens'] === 'object'
      ? (meta['tokens'] as Record<string, unknown>)
      : undefined,
  )
}

/** Prompt + completion for one assistant message (last step when present). */
export function resolveMessageContextTokens(message: HarnessMessage): {
  prompt: number
  completion: number
  used: number
} {
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
} {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.role !== 'assistant') continue
    const tokens = resolveMessageContextTokens(message)
    if (tokens.used > 0) {
      return {
        used: tokens.used,
        promptTokens: tokens.prompt,
        completionTokens: tokens.completion,
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

/** Resolve used, limit, and fill percent for the active session model. */
export function getSessionContextUsage(input: SessionContextUsageInput): SessionContextUsage {
  const { used, promptTokens, completionTokens } = resolveSessionUsedTokens(input.messages)
  const catalogModel = resolveCatalogModel(
    input.models,
    input.modelId,
    input.defaultModel ?? '',
  )
  const limit = catalogModel?.context_length ?? 0
  const percent = limit > 0 ? Math.round((used / limit) * 100) : 0
  return { used, limit, percent, promptTokens, completionTokens }
}
