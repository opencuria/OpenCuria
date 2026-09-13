import { describe, expect, it } from 'vitest'

import { elapsedMs, formatElapsed } from './formatElapsed'

describe('formatElapsed', () => {
  it('formats seconds only', () => {
    expect(formatElapsed(11_000)).toBe('11s')
    expect(formatElapsed(400)).toBe('0s')
  })

  it('formats minutes and seconds', () => {
    expect(formatElapsed(5 * 60_000 + 11_000)).toBe('5m 11s')
  })

  it('formats hours, minutes, and seconds', () => {
    expect(formatElapsed(3600_000 + 5 * 60_000 + 11_000)).toBe('1h 5m 11s')
  })
})

describe('elapsedMs', () => {
  it('returns the delta between ISO timestamps', () => {
    expect(
      elapsedMs('2026-03-29T10:00:00.000Z', '2026-03-29T10:05:11.000Z'),
    ).toBe(5 * 60_000 + 11_000)
  })

  it('returns null when a timestamp is missing', () => {
    expect(elapsedMs('2026-03-29T10:00:00.000Z', null)).toBeNull()
    expect(elapsedMs(undefined, '2026-03-29T10:00:00.000Z')).toBeNull()
  })
})
