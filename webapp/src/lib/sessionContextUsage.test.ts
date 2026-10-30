import { describe, expect, it } from 'vitest'

import type { HarnessMessage, HarnessPart } from '@/types/harness'
import {
  formatTokenCount,
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
    const usage = resolveSessionUsedTokens([
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
    ])
    expect(usage).toEqual({
      used: 189_000,
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

describe('runtime context', () => {
  it.each(['meta', 'display'] as const)(
    'uses Codex runtime context from %s instead of cumulative tokens',
    (source) => {
      const usage = resolveSessionUsedTokens([
        makeMessage({
          tokens: { prompt: 300_000, completion: 15_000, total: 315_000 },
          parts: [
            makePart({
              [source]: {
                tokens: { prompt_tokens: 300_000, completion_tokens: 15_000 },
                context: {
                  prompt_tokens: 50_000,
                  completion_tokens: 1500,
                  total_tokens: 51_500,
                  context_length: 258_400,
                },
              },
            }),
          ],
        }),
      ])
      expect(usage).toEqual({
        used: 51_500,
        limit: 258_400,
        promptTokens: 50_000,
        completionTokens: 1500,
      })
    },
  )

  it('uses live context on the running step before an answer completes', () => {
    expect(
      resolveSessionUsedTokens([
        makeMessage({
          parts: [
            makePart({
              type: 'step-start',
              state: 'running',
              meta: {
                context: {
                  prompt_tokens: 50_000,
                  completion_tokens: 1500,
                  context_length: 258_400,
                },
              },
            }),
          ],
        }),
      ]),
    ).toEqual({
      used: 51_500,
      promptTokens: 50_000,
      completionTokens: 1500,
      limit: 258_400,
    })
  })

  it.each([null, 'invalid', { total_tokens: Infinity, context_length: NaN }])(
    'keeps the existing token fallback for malformed context: %s',
    (context) => {
      expect(
        resolveMessageContextTokens(
          makeMessage({
            parts: [
              makePart({
                meta: {
                  context,
                  tokens: { prompt_tokens: 40, completion_tokens: 10 },
                },
              }),
            ],
          }),
        ),
      ).toEqual({ prompt: 40, completion: 10, used: 50 })
    },
  )
})
