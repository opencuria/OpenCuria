import { describe, expect, it } from 'vitest'
import { shouldResetHarnessPage } from './harnessPagination'

describe('shouldResetHarnessPage', () => {
  it('keeps the page on append-only stream growth or unchanged structural identities', () => {
    expect(shouldResetHarnessPage(['text:a', 'work:b'], ['text:a', 'work:b'])).toBe(false)
    expect(shouldResetHarnessPage(['text:a', 'work:b'], ['text:a', 'work:b', 'text:c'])).toBe(false)
  })

  it('resets on replacement, reordering, or truncation', () => {
    expect(shouldResetHarnessPage(['text:a', 'work:b'], ['text:a', 'work:c'])).toBe(true)
    expect(shouldResetHarnessPage(['text:a', 'work:b'], ['work:b', 'text:a'])).toBe(true)
    expect(shouldResetHarnessPage(['text:a', 'work:b'], ['text:a'])).toBe(true)
  })
})
