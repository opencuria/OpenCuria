/**
 * Sum and format cost/token usage for a finished harness assistant message.
 */

import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { formatEffort, resolveCatalogModel, type ProviderModel } from './harnessModels'

export interface MessageUsage {
  cost: number
  promptTokens: number
  completionTokens: number
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

function usageFromStepFinish(part: HarnessPart): MessageUsage {
  const meta = part.meta ?? {}
  const tokens = tokensFromRecord(
    meta['tokens'] && typeof meta['tokens'] === 'object'
      ? (meta['tokens'] as Record<string, unknown>)
      : undefined,
  )
  return {
    cost: asFiniteNumber(meta['cost']),
    promptTokens: tokens.prompt,
    completionTokens: tokens.completion,
  }
}

/** Resolve billed cost and in/out tokens for an assistant message. */
export function resolveMessageUsage(message: HarnessMessage): MessageUsage {
  const fromMessage = tokensFromRecord(message.tokens)
  const cost = asFiniteNumber(message.cost)
  if (cost > 0 || fromMessage.prompt > 0 || fromMessage.completion > 0) {
    return {
      cost,
      promptTokens: fromMessage.prompt,
      completionTokens: fromMessage.completion,
    }
  }

  return message.parts
    .filter((part) => part.type === 'step-finish')
    .map(usageFromStepFinish)
    .reduce<MessageUsage>(
      (sum, part) => ({
        cost: sum.cost + part.cost,
        promptTokens: sum.promptTokens + part.promptTokens,
        completionTokens: sum.completionTokens + part.completionTokens,
      }),
      { cost: 0, promptTokens: 0, completionTokens: 0 },
    )
}

function formatTokens(value: number): string {
  return Math.round(value).toLocaleString('en-US')
}

function formatCost(value: number): string {
  if (value >= 1) return `$${value.toFixed(2)}`
  return `$${value.toFixed(4)}`
}

/** Compact hover line, or null when there is nothing to show. */
export function formatMessageUsage(usage: MessageUsage): string | null {
  const parts: string[] = []
  if (usage.cost > 0) parts.push(formatCost(usage.cost))
  if (usage.promptTokens > 0) parts.push(`${formatTokens(usage.promptTokens)} in`)
  if (usage.completionTokens > 0) {
    parts.push(`${formatTokens(usage.completionTokens)} out`)
  }
  return parts.length > 0 ? parts.join(' · ') : null
}

/** Hover footer: catalog model, effort, then cost/tokens. */
export function formatMessageHoverLine(
  message: HarnessMessage,
  models: ProviderModel[] = [],
): string | null {
  const segments: string[] = []
  const modelId = (message.model ?? '').trim()
  if (modelId) {
    segments.push(resolveCatalogModel(models, modelId)?.name ?? modelId)
  } else {
    segments.push('Auto')
  }
  const effort = (message.reasoning_effort ?? '').trim()
  if (effort) segments.push(formatEffort(effort))
  const usage = formatMessageUsage(resolveMessageUsage(message))
  if (usage) segments.push(usage)
  return segments.length > 0 ? segments.join(' · ') : null
}
