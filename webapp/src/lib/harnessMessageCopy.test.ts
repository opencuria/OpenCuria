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

  it('copies only the final answer after work and repeated Agent-S sequences', () => {
    const message = makeMessage([
      makePart('early', 'text', 'Earlier response'),
      makePart('agent-1', 'agent', 'private plan'),
      makePart('work-1', 'tool', 'private tool output'),
      makePart('middle', 'text', 'intermediate note'),
      makePart('agent-2', 'agent', 'another private plan'),
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

  it('uses legacy content only when there are no text parts', () => {
    expect(getMessageCopyText(makeMessage([], 'legacy answer'))).toBe('legacy answer')
    expect(
      getMessageCopyText({ ...makeMessage([]), parts: undefined as unknown as HarnessPart[] }),
    ).toBe('stale message content')
    expect(
      getMessageCopyText(
        makeMessage([makePart('compact', 'compaction', 'private summary')], 'legacy answer'),
      ),
    ).toBeNull()
    expect(getMessageCopyText(makeMessage([], '   '))).toBeNull()
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
})
