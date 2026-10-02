import { describe, expect, it } from 'vitest'

import type { HarnessMessage, HarnessPart } from '@/types/harness'
import type { ProviderModel } from '@/lib/harnessModels'
import {
  formatTokenCount,
  getSessionContextUsage,
  resolveMessageContextTokens,
  resolveSessionUsedTokens,
} from './sessionContextUsage'

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

const models: ProviderModel[] = [
  {
    id: 'model-big',
    name: 'Big',
    reasoning_efforts: [],
    default_effort: '',
    supports_tools: true,
    context_length: 200_000,
    max_output_tokens: 32_768,
  },
]

describe('formatTokenCount', () => {
  it('formats thousands and millions with one decimal', () => {
    expect(formatTokenCount(82200)).toBe('82.2K')
    expect(formatTokenCount(1000)).toBe('1.0K')
    expect(formatTokenCount(1_500_000)).toBe('1.5M')
  })

  it('rounds sub-thousand values', () => {
    expect(formatTokenCount(512)).toBe('512')
    expect(formatTokenCount(0)).toBe('0')
  })
})

describe('resolveMessageContextTokens', () => {
  it('prefers the last step-finish part over message totals', () => {
    const tokens = resolveMessageContextTokens(
      makeMessage({
        tokens: { prompt: 900, completion: 100 },
        parts: [
          makePart({
            id: 's1',
            meta: { tokens: { prompt_tokens: 40, completion_tokens: 10 } },
          }),
          makePart({
            id: 's2',
            meta: { tokens: { prompt: 30, completion: 5 } },
          }),
        ],
      }),
    )
    expect(tokens).toEqual({ prompt: 30, completion: 5, used: 35 })
  })

  it('uses the last compact timeline step, not accumulated message totals', () => {
    const usage = getSessionContextUsage({
      modelId: 'chatgpt/gpt-6.1-sol',
      models: [{ ...models[0]!, id: 'chatgpt/gpt-6.1-sol', context_length: 1_050_000 }],
      messages: [
        makeMessage({
          model: 'chatgpt/gpt-6.1-sol',
          tokens: { prompt: 300_000, completion: 15_000, total: 315_000 },
          parts: [
            makePart({
              id: 's1',
              meta: {},
              display: { step: 1, tokens: { prompt_tokens: 120_000, completion_tokens: 6_000 } },
            }),
            makePart({
              id: 's2',
              meta: {},
              display: {
                step: 2,
                tokens: { prompt_tokens: 180_000, completion_tokens: 9_000, total_tokens: 189_000 },
              },
            }),
          ],
        }),
      ],
    })
    expect(usage).toEqual({
      used: 189_000,
      limit: 1_050_000,
      percent: 18,
      promptTokens: 180_000,
      completionTokens: 9_000,
    })
  })

  it('prefers streamed metadata over an older timeline display', () => {
    const tokens = resolveMessageContextTokens(
      makeMessage({
        parts: [
          makePart({
            meta: { tokens: { prompt_tokens: 40, completion_tokens: 10 } },
            display: { tokens: { prompt_tokens: 30, completion_tokens: 5 } },
          }),
        ],
      }),
    )
    expect(tokens).toEqual({ prompt: 40, completion: 10, used: 50 })
  })

  it('ignores malformed or non-finite timeline usage without using accumulated totals', () => {
    for (const tokens of [
      undefined,
      'invalid',
      { prompt_tokens: NaN, completion_tokens: Infinity },
    ]) {
      const usage = resolveMessageContextTokens(
        makeMessage({
          tokens: { prompt: 900, completion: 100 },
          parts: [makePart({ display: { tokens } })],
        }),
      )
      expect(usage).toEqual({ prompt: 0, completion: 0, used: 0 })
    }
  })

  it('falls back to message totals when no step-finish parts exist', () => {
    const tokens = resolveMessageContextTokens(
      makeMessage({
        tokens: { prompt: 120, completion: 80 },
      }),
    )
    expect(tokens).toEqual({ prompt: 120, completion: 80, used: 200 })
  })
})

describe('resolveSessionUsedTokens', () => {
  it('uses the last assistant message with token usage', () => {
    const used = resolveSessionUsedTokens([
      makeMessage({
        id: 'm1',
        tokens: { prompt: 10, completion: 5 },
      }),
      { id: 'm2', session_id: 'session-1', role: 'user', content: 'hi', parts: [] },
      makeMessage({
        id: 'm3',
        tokens: { prompt: 100, completion: 22 },
      }),
    ])
    expect(used).toEqual({ used: 122, promptTokens: 100, completionTokens: 22 })
  })
})

describe('getSessionContextUsage', () => {
  it('rounds usage percent against the catalog context length', () => {
    const usage = getSessionContextUsage({
      messages: [makeMessage({ tokens: { prompt: 50_000, completion: 14_400 } })],
      models,
      modelId: 'model-big',
      defaultModel: '',
    })
    expect(usage.used).toBe(64_400)
    expect(usage.limit).toBe(200_000)
    expect(usage.percent).toBe(32)
  })

  it('returns zero percent when the catalog limit is missing', () => {
    const usage = getSessionContextUsage({
      messages: [makeMessage({ tokens: { prompt: 500, completion: 500 } })],
      models: [{ ...models[0]!, id: 'model-big', context_length: 0 }],
      modelId: 'model-big',
    })
    expect(usage.limit).toBe(0)
    expect(usage.percent).toBe(0)
    expect(usage.used).toBe(1000)
  })
})
