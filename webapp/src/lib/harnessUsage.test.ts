import { describe, expect, it } from 'vitest'

import type { HarnessMessage, HarnessPart } from '@/types/harness'
import type { ProviderModel } from './harnessModels'
import { formatMessageHoverLine, formatMessageUsage, resolveMessageUsage } from './harnessUsage'

function makePart(overrides: Partial<HarnessPart> = {}): HarnessPart {
  return {
    id: 'part-1',
    session_id: 'session-1',
    type: 'step-finish',
    state: 'completed',
    title: '',
    output: '',
    ...overrides,
  }
}

function makeMessage(overrides: Partial<HarnessMessage> = {}): HarnessMessage {
  return {
    id: 'msg-1',
    session_id: 'session-1',
    role: 'assistant',
    content: 'done',
    parts: [],
    ...overrides,
  }
}

describe('resolveMessageUsage', () => {
  it('prefers message-level cost and tokens', () => {
    const usage = resolveMessageUsage(
      makeMessage({
        cost: 0.0123,
        tokens: { prompt: 1204, completion: 318, total: 1522 },
        parts: [
          makePart({
            meta: { cost: 9, tokens: { prompt_tokens: 1, completion_tokens: 1 } },
          }),
        ],
      }),
    )
    expect(usage).toEqual({ cost: 0.0123, promptTokens: 1204, completionTokens: 318 })
  })

  it('sums step-finish parts when message totals are empty', () => {
    const usage = resolveMessageUsage(
      makeMessage({
        parts: [
          makePart({
            id: 's1',
            meta: { cost: 0.01, tokens: { prompt_tokens: 10, completion_tokens: 4 } },
          }),
          makePart({
            id: 's2',
            meta: { cost: 0.0023, tokens: { prompt: 5, completion: 2 } },
          }),
        ],
      }),
    )
    expect(usage.cost).toBeCloseTo(0.0123)
    expect(usage.promptTokens).toBe(15)
    expect(usage.completionTokens).toBe(6)
  })
})

describe('formatMessageUsage', () => {
  it('formats cost and in/out tokens', () => {
    expect(
      formatMessageUsage({ cost: 0.0123, promptTokens: 1204, completionTokens: 318 }),
    ).toBe('$0.0123 · 1,204 in · 318 out')
  })

  it('omits zero cost and returns null when empty', () => {
    expect(
      formatMessageUsage({ cost: 0, promptTokens: 12, completionTokens: 0 }),
    ).toBe('12 in')
    expect(
      formatMessageUsage({ cost: 0, promptTokens: 0, completionTokens: 0 }),
    ).toBeNull()
  })
})

const catalog: ProviderModel[] = [
  {
    id: 'acme/think',
    name: 'Think',
    reasoning_efforts: ['low', 'high'],
    default_effort: 'high',
    supports_tools: true,
    context_length: 128_000,
    max_output_tokens: 16_384,
  },
]

describe('formatMessageHoverLine', () => {
  it('joins catalog name, effort, and usage', () => {
    expect(
      formatMessageHoverLine(
        makeMessage({
          model: 'acme/think',
          reasoning_effort: 'high',
          cost: 0.0123,
          tokens: { prompt: 1204, completion: 318, total: 1522 },
        }),
        catalog,
      ),
    ).toBe('Think · High · $0.0123 · 1,204 in · 318 out')
  })

  it('falls back to the model id and omits empty effort', () => {
    expect(
      formatMessageHoverLine(
        makeMessage({
          model: 'unknown/model',
          cost: 0.01,
          tokens: { prompt: 10, completion: 4, total: 14 },
        }),
        catalog,
      ),
    ).toBe('unknown/model · $0.0100 · 10 in · 4 out')
  })

  it('shows Auto and model when usage is zero', () => {
    expect(formatMessageHoverLine(makeMessage({}), catalog)).toBe('Auto')
    expect(
      formatMessageHoverLine(makeMessage({ model: 'acme/think', reasoning_effort: 'low' }), catalog),
    ).toBe('Think · Low')
  })
})
