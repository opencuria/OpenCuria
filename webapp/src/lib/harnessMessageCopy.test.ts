import { describe, expect, it } from 'vitest'
import type { HarnessMessage, HarnessPart } from '@/types/harness'
import { getMessageCopyText } from './harnessMessageCopy'

function makePart(
  id: string,
  type: HarnessPart['type'],
  output = '',
  overrides: Partial<HarnessPart> = {},
): HarnessPart {
  return {
    id,
    session_id: 'session-1',
    type,
    state: 'completed',
    title: '',
    output,
    ...overrides,
  }
}

function makeMessage(parts: HarnessPart[], content = 'stale message content'): HarnessMessage {
  return {
    id: 'message-1',
    session_id: 'session-1',
    role: 'assistant',
    content,
    parts,
  }
}

describe('getMessageCopyText', () => {
  it('returns the complete raw user prompt, including markdown, even during streaming', () => {
    const message = { ...makeMessage([], '**raw**\n```ts\ncode\n```'), role: 'user' as const }
    expect(getMessageCopyText(message, true)).toBe(message.content)
  })

  it('copies a complete text-only answer without changing markdown or whitespace', () => {
    const message = makeMessage([
      makePart('text-1', 'text', '**Keep** '),
      makePart('text-2', 'text', 'the `markdown`.\n'),
    ])

    expect(getMessageCopyText(message)).toBe('**Keep** the `markdown`.\n')
  })

  it('copies only the final answer after interleaved tools and reasoning', () => {
    const message = makeMessage([
      makePart('early', 'text', 'Earlier response'),
      makePart('work-1', 'tool', 'private tool output'),
      makePart('middle', 'text', 'intermediate note'),
      makePart('work-2', 'reasoning', 'private reasoning'),
      makePart('final-1', 'text', 'Final '),
      makePart('final-2', 'text', 'answer'),
    ])

    expect(getMessageCopyText(message)).toBe('Final answer')
  })

  it('does not copy earlier prose or stale content when there is no final text', () => {
    const message = makeMessage([
      makePart('answer-before-work', 'text', 'Not final'),
      makePart('tool', 'tool', 'hidden result'),
    ])

    expect(getMessageCopyText(message)).toBeNull()
  })

  it('does not mix stale content into empty trailing text or copy empty final parts', () => {
    const message = makeMessage([
      makePart('answer-before-work', 'text', 'Not final'),
      makePart('tool', 'tool', 'hidden result'),
      makePart('empty-final', 'text', ''),
    ])

    expect(getMessageCopyText(message)).toBeNull()
  })

  it('does not offer assistant copy while streaming, including after cancellation state', () => {
    const message = makeMessage([makePart('text', 'text', 'Incomplete answer')])
    message.finish = 'aborted'
    message.error = 'cancelled'

    expect(getMessageCopyText(message, true)).toBeNull()
    expect(getMessageCopyText(message)).toBe('Incomplete answer')
  })

  it('does not copy aggregate content without visible text parts', () => {
    expect(getMessageCopyText(makeMessage([]))).toBeNull()
    expect(
      getMessageCopyText(makeMessage([makePart('compact', 'compaction', 'private summary')])),
    ).toBeNull()
  })

  it('keeps trailing text after compaction and excludes compaction content', () => {
    const message = makeMessage([
      makePart('text-1', 'text', 'Before compaction'),
      makePart('tool', 'tool', 'private result'),
      makePart('compact', 'compaction', 'private summary'),
      makePart('final', 'text', 'Visible answer'),
    ])

    expect(getMessageCopyText(message)).toBe('Visible answer')
  })

  it('never copies steer text and only the closing prose after it', () => {
    const message = makeMessage([
      makePart('text-1', 'text', 'Working on it'),
      makePart('tool', 'tool', 'private result'),
      makePart('steer-1', 'steer', 'Use pnpm instead', { meta: { steer_id: 's1' } }),
      makePart('final', 'text', 'Switched to pnpm'),
    ])
    expect(getMessageCopyText(message)).toBe('Switched to pnpm')

    const steerLast = makeMessage([
      makePart('text-1', 'text', 'Working on it'),
      makePart('steer-1', 'steer', 'Use pnpm instead'),
    ])
    expect(getMessageCopyText(steerLast)).toBeNull()
  })
})
