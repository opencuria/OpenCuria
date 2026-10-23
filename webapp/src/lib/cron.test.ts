import { describe, expect, it } from 'vitest'
import {
  cronOccurrencesBetween,
  describeCron,
  nextCronOccurrences,
  parseCron,
  validateCronExpression,
} from './cron'

const iso = (dates: Date[]): string[] => dates.map((date) => date.toISOString())

describe('cron helpers', () => {
  it('parses lists, ranges, steps, names, macros and Sunday as 7', () => {
    const spec = parseCron('*/20 9-11 * JAN-MAR mon,fri')
    expect(spec.minutes).toEqual([0, 20, 40])
    expect(spec.hours).toEqual([9, 10, 11])
    expect([...spec.months].sort()).toEqual([1, 2, 3])
    expect([...spec.weekdays].sort()).toEqual([1, 5])
    expect([...parseCron('0 0 * * 7').weekdays]).toEqual([0])
    expect(parseCron('@hourly').minutes).toEqual([0])
  })

  it('reports invalid and impossible expressions', () => {
    for (const bad of ['', '* * * *', '60 * * * *', '* 24 * * *', '*/0 * * * *', '5-1 * * * *']) {
      expect(validateCronExpression(bad)).not.toBeNull()
    }
    expect(validateCronExpression('0 0 30 2 *')).toMatch(/never matches/)
    expect(validateCronExpression('0 9 * * 1-5; 30 14 * * *')).toBeNull()
  })

  it('lists several runs per day in the chosen timezone', () => {
    const now = new Date('2025-01-06T08:10:00Z') // Mon 09:10 in Berlin
    expect(iso(nextCronOccurrences('*/30 8-17 * * 1-5', 'Europe/Berlin', now, 3))).toEqual([
      '2025-01-06T08:30:00.000Z',
      '2025-01-06T09:00:00.000Z',
      '2025-01-06T09:30:00.000Z',
    ])
    expect(iso(nextCronOccurrences('0 9,18 * * *; 30 14 * * *', 'UTC', new Date('2025-01-06T09:05:00Z'), 3))).toEqual([
      '2025-01-06T14:30:00.000Z',
      '2025-01-06T18:00:00.000Z',
      '2025-01-07T09:00:00.000Z',
    ])
  })

  it('lists occurrences within a half-open range, capped and across DST', () => {
    const start = new Date('2025-01-06T08:00:00Z') // Mon 09:00 Berlin
    const end = new Date('2025-01-06T10:00:00Z')
    expect(iso(cronOccurrencesBetween('0 * * * *', 'Europe/Berlin', start, end))).toEqual([
      '2025-01-06T08:00:00.000Z',
      '2025-01-06T09:00:00.000Z',
    ])
    expect(cronOccurrencesBetween('*/5 * * * *', 'UTC', start, end, 3)).toHaveLength(3)
    // Monthly schedules do not scan years ahead to fill the cap.
    expect(cronOccurrencesBetween('0 9 1 * *', 'UTC', start, end)).toEqual([])
    // Berlin falls back on Oct 26 2025: 02:30 happens once, at the earlier instant.
    expect(iso(cronOccurrencesBetween(
      '30 2 * * *', 'Europe/Berlin', new Date('2025-10-25T00:00:00Z'), new Date('2025-10-27T00:00:00Z'),
    ))).toEqual(['2025-10-25T00:30:00.000Z', '2025-10-26T00:30:00.000Z'])
    expect(cronOccurrencesBetween('0 * * * *', 'UTC', new Date('invalid'), end)).toEqual([])
  })

  it('applies classic day-of-month OR day-of-week matching', () => {
    const now = new Date('2025-01-06T10:00:00Z')
    expect(iso(nextCronOccurrences('0 9 15 * 3', 'UTC', now, 2))).toEqual([
      '2025-01-08T09:00:00.000Z',
      '2025-01-15T09:00:00.000Z',
    ])
  })

  it('skips DST gaps and runs a repeated hour once', () => {
    const spring = new Date('2025-03-30T00:50:00Z') // 01:50 CET
    expect(iso(nextCronOccurrences('*/30 * * * *', 'Europe/Berlin', spring, 2))).toEqual([
      '2025-03-30T01:00:00.000Z', // 03:00 CEST
      '2025-03-30T01:30:00.000Z',
    ])
    const fall = new Date('2025-10-26T00:45:00Z') // 02:45 CEST
    expect(iso(nextCronOccurrences('0 * * * *', 'Europe/Berlin', fall, 1))).toEqual([
      '2025-10-26T02:00:00.000Z', // 03:00 CET, not the repeated 02:00
    ])
  })

  it('finds leap-day schedules and returns nothing for invalid input', () => {
    expect(iso(nextCronOccurrences('0 0 29 2 *', 'UTC', new Date('2025-03-01T00:00:00Z')))).toEqual([
      '2028-02-29T00:00:00.000Z',
    ])
    expect(nextCronOccurrences('nope', 'UTC')).toEqual([])
    expect(nextCronOccurrences('* * * * *', 'Not/AZone')).toEqual([])
  })

  it('describes common cadences compactly', () => {
    expect(describeCron('*/15 8-17 * * 1-5')).toBe('Weekdays · Every 15 min, 08:00–17:59')
    expect(describeCron('0 */2 * * *')).toBe('Daily · Every 2 h')
    expect(describeCron('0 9,17 * * 0,6')).toBe('Weekends · 09:00, 17:00')
    expect(describeCron('0 9 1 * *')).toBe('Day 1 of month · 09:00')
    expect(describeCron('bad')).toBe('Custom schedule')
  })
})
