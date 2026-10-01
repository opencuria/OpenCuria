import { describe, expect, it } from 'vitest'
import { isValidTimeZone, nextLocalOccurrence, validateSchedule } from './scheduledTasks'

const base = {
  name: 'Daily review', workspace_id: 'ws-1', prompt: 'Check the project',
  recurrence: 'daily' as const, weekdays: [], timezone_name: 'Europe/Berlin',
}

describe('scheduled-task schedule helpers', () => {
  it('validates daily and selected weekday schedules', () => {
    expect(validateSchedule(base)).toEqual({ valid: true, errors: {} })
    expect(validateSchedule({ ...base, recurrence: 'weekly', weekdays: [0, 2, 4] })).toEqual({ valid: true, errors: {} })
    expect(validateSchedule({ ...base, recurrence: 'weekly', weekdays: [] }).errors.weekdays).toBe('Choose at least one day.')
  })

  it('rejects missing content and invalid IANA timezones', () => {
    const result = validateSchedule({ ...base, name: ' ', prompt: '', timezone_name: 'Mars/Olympus' })
    expect(result.valid).toBe(false)
    expect(result.errors.name).toBeDefined()
    expect(result.errors.prompt).toBeDefined()
    expect(result.errors.timezone_name).toBeDefined()
    expect(isValidTimeZone('America/Los_Angeles')).toBe(true)
    expect(isValidTimeZone('Not/AZone')).toBe(false)
  })

  it('previews daily and selected weekdays in the chosen timezone', () => {
    const now = new Date('2025-01-06T07:00:00.000Z') // Monday, 08:00 in Berlin
    expect(nextLocalOccurrence('daily', [], '09:00', 'Europe/Berlin', now)?.toISOString()).toBe('2025-01-06T08:00:00.000Z')
    expect(nextLocalOccurrence('weekly', [2], '09:00', 'Europe/Berlin', now)?.toISOString()).toBe('2025-01-08T08:00:00.000Z')
    expect(nextLocalOccurrence('daily', [], '09:00', 'Not/AZone', now)).toBeNull()
  })

  it('ignores the default hidden weekday selection for daily drafts and previews the next UTC run', () => {
    const now = new Date('2025-01-06T10:00:00.000Z')
    expect(nextLocalOccurrence('daily', [0, 1, 2, 3, 4], '09:00', 'UTC', now)?.toISOString())
      .toBe('2025-01-07T09:00:00.000Z')
  })

  it('skips nonexistent spring-forward wall times for daily and weekly schedules', () => {
    const now = new Date('2025-03-29T12:00:00.000Z')
    expect(nextLocalOccurrence('daily', [], '02:30', 'Europe/Berlin', now)?.toISOString()).toBe('2025-03-31T00:30:00.000Z')
    expect(nextLocalOccurrence('weekly', [6], '02:30', 'Europe/Berlin', now)?.toISOString()).toBe('2025-04-06T00:30:00.000Z')
  })

  it('selects the earlier fall-back instant and never duplicates a passed fold', () => {
    const beforeFold = new Date('2025-10-25T12:00:00.000Z')
    expect(nextLocalOccurrence('daily', [], '02:30', 'Europe/Berlin', beforeFold)?.toISOString()).toBe('2025-10-26T00:30:00.000Z')
    const betweenFolds = new Date('2025-10-26T01:00:00.000Z')
    expect(nextLocalOccurrence('daily', [], '02:30', 'Europe/Berlin', betweenFolds)?.toISOString()).toBe('2025-10-27T01:30:00.000Z')
    expect(nextLocalOccurrence('weekly', [6], '02:30', 'Europe/Berlin', beforeFold)?.toISOString()).toBe('2025-10-26T00:30:00.000Z')
    expect(nextLocalOccurrence('weekly', [6], '02:30', 'Europe/Berlin', betweenFolds)?.toISOString()).toBe('2025-11-02T01:30:00.000Z')
  })

  it('rejects malformed clock strings and invalid recurrence days', () => {
    const now = new Date('2025-01-01T00:00:00.000Z')
    for (const time of ['9:00', '09:0', '24:00', '12:60', '09:00x', ' 09:00']) {
      expect(nextLocalOccurrence('daily', [], time, 'UTC', now)).toBeNull()
    }
    expect(nextLocalOccurrence('weekly', [7], '09:00', 'UTC', now)).toBeNull()
    expect(nextLocalOccurrence('weekly', [], '09:00', 'UTC', now)).toBeNull()
    expect(nextLocalOccurrence('daily', [0], '09:00', 'UTC', now)?.toISOString()).toBe('2025-01-01T09:00:00.000Z')
  })
})
