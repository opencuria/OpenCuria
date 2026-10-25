import { describe, expect, it } from 'vitest'
import {
  cronWeekdayField,
  defaultScheduleDraft,
  describeSchedule,
  describeScheduleDraft,
  isScheduleEnded,
  isValidDate,
  isValidTimeZone,
  maxRunsPerDay,
  nextDateAt,
  nextLocalOccurrence,
  onceOccurrence,
  scheduleDraftFromTask,
  scheduleDraftToCron,
  schedulePayload,
  scheduleStateLabel,
  validateSchedule,
  validateScheduleDraft,
} from './scheduledTasks'
import type { ScheduleDraft } from './scheduledTasks'

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

describe('schedule drafts', () => {
  const draft = (overrides: Partial<ScheduleDraft>): ScheduleDraft => ({ ...defaultScheduleDraft(), mode: 'times', ...overrides })

  it('defaults new drafts to a one-time run at the next 09:00', () => {
    const fresh = defaultScheduleDraft('UTC')
    expect(fresh).toMatchObject({ mode: 'once', times: ['09:00'] })
    expect(onceOccurrence(fresh.date, '09:00', 'UTC')!.getTime()).toBeGreaterThan(Date.now())
    const now = new Date('2026-10-24T10:00:00Z')
    expect(nextDateAt('09:00', 'UTC', now)).toBe('2026-10-25')
    expect(nextDateAt('11:00', 'UTC', now)).toBe('2026-10-24')
    expect(nextDateAt('09:00', 'Pacific/Kiritimati', now)).toBe('2026-10-25')
  })

  it('stores a one-time run as its local date and time', () => {
    const once = draft({ mode: 'once', date: '2026-11-03', times: ['14:30'] })
    expect(schedulePayload(once)).toEqual({
      recurrence: 'once', weekdays: [], local_time: '14:30', cron_expression: '', run_date: '2026-11-03',
    })
    const task = { recurrence: 'once' as const, weekdays: [], local_time: '14:30', run_date: '2026-11-03' }
    expect(scheduleDraftFromTask(task)).toMatchObject({ mode: 'once', date: '2026-11-03', times: ['14:30'] })
    expect(describeSchedule(task)).toMatch(/^Once · .*2026 · 14:30$/)
    expect(scheduleDraftToCron(once)).toBe('30 14 3 11 *')
    expect(validateScheduleDraft(once)).toEqual({})
    expect(validateScheduleDraft({ ...once, date: '2026-02-30' })).toEqual({ date: 'Choose a date.' })
    expect(validateScheduleDraft({ ...once, times: [''] })).toEqual({ times: 'Enter a valid time.' })
    expect([isValidDate('2028-02-29'), isValidDate('2026-02-29'), isValidDate('')]).toEqual([true, false, false])
  })

  it('resolves one-time runs in their zone with the backend DST policy', () => {
    // Berlin falls back on 25 Oct 2026; 09:00 is already CET.
    expect(onceOccurrence('2026-10-25', '09:00', 'Europe/Berlin')?.toISOString()).toBe('2026-10-25T08:00:00.000Z')
    // Ambiguous 02:30 runs at the first (CEST) instant.
    expect(onceOccurrence('2026-10-25', '02:30', 'Europe/Berlin')?.toISOString()).toBe('2026-10-25T00:30:00.000Z')
    // 02:30 does not exist when Berlin springs forward.
    expect(onceOccurrence('2026-03-29', '02:30', 'Europe/Berlin')).toBeNull()
    expect(onceOccurrence('2026-10-25', '09:00', 'Not/AZone')).toBeNull()
  })

  it('labels finished one-time tasks as ended instead of paused', () => {
    const now = new Date('2026-10-24T10:00:00Z')
    const once = { recurrence: 'once' as const, run_date: '2026-10-24', local_time: '09:00', timezone_name: 'UTC' }
    expect(isScheduleEnded(once, now)).toBe(true)
    expect(scheduleStateLabel({ ...once, enabled: false }, now)).toBe('Ended')
    expect(scheduleStateLabel({ ...once, run_date: '2026-10-25', enabled: false }, now)).toBe('Paused')
    expect(scheduleStateLabel({ ...once, run_date: '2026-10-25', enabled: true }, now)).toBe('')
    expect(scheduleStateLabel({ ...once, recurrence: 'daily', enabled: true }, now)).toBe('')
  })

  it('keeps a single time as the classic daily/weekly payload', () => {
    expect(schedulePayload(draft({ times: ['07:30'] }))).toEqual({
      recurrence: 'daily', weekdays: [], local_time: '07:30', cron_expression: '', run_date: null,
    })
    expect(schedulePayload(draft({ weekdays: [0, 2], times: ['07:30'] }))).toMatchObject({
      recurrence: 'weekly', weekdays: [0, 2],
    })
  })

  it('compiles several times per day into compact cron expressions', () => {
    const multi = draft({ weekdays: [0, 1, 2, 3, 4], times: ['18:00', '09:00', '14:30'] })
    expect(schedulePayload(multi)).toEqual({
      recurrence: 'cron', weekdays: [], local_time: '09:00', cron_expression: '0 9,18 * * 1-5; 30 14 * * 1-5', run_date: null,
    })
    expect(scheduleDraftFromTask({ recurrence: 'cron', weekdays: [], local_time: '09:00', cron_expression: '0 9,18 * * 1-5; 30 14 * * 1-5' }))
      .toMatchObject({ mode: 'times', weekdays: [0, 1, 2, 3, 4], times: ['09:00', '14:30', '18:00'] })
    expect(cronWeekdayField([5, 6])).toBe('0,6')
  })

  it('round-trips repeating intervals with optional active hours', () => {
    const minutes = draft({ mode: 'interval', interval: '15m', window_enabled: true, window_start: 8, window_end: 18 })
    expect(scheduleDraftToCron(minutes)).toBe('*/15 8-17 * * *')
    const hours = draft({ mode: 'interval', interval: '2h', weekdays: [0, 1, 2, 3, 4] })
    expect(scheduleDraftToCron(hours)).toBe('0 */2 * * 1-5')
    for (const value of [minutes, hours]) {
      expect(scheduleDraftFromTask({ recurrence: 'cron', weekdays: [], local_time: '09:00', cron_expression: scheduleDraftToCron(value) }))
        .toEqual(value)
    }
    expect(describeScheduleDraft(minutes)).toBe('Daily · Every 15 min, 08:00–18:00')
    expect(validateScheduleDraft({ ...minutes, window_end: 8 }).window).toBe('End must be after start.')
  })

  it('falls back to the cron editor for anything else', () => {
    const task = { recurrence: 'cron' as const, weekdays: [], local_time: '09:00', cron_expression: '0  9 1 * *' }
    expect(scheduleDraftFromTask(task)).toMatchObject({ mode: 'cron', cron_expression: '0 9 1 * *' })
    expect(describeSchedule(task)).toBe('Day 1 of month · 09:00')
    expect(describeSchedule({ recurrence: 'weekly', weekdays: [0, 1, 2, 3, 4], local_time: '09:00' })).toBe('Weekdays · 09:00')
    expect(validateScheduleDraft(draft({ mode: 'cron', cron_expression: '61 * * * *' })).cron_expression).toBeDefined()
    expect(maxRunsPerDay('*/15 8-17 * * *')).toBe(40)
  })
})

describe('maxRunsPerDay', () => {
  it('counts runs per calendar day rather than across all expressions', () => {
    expect(maxRunsPerDay('0 9-17/2 * * 1-5; 0 10 * * sat')).toBe(5)
    expect(maxRunsPerDay('0 9 * * *; 0 9 * * 1')).toBe(1)
    expect(maxRunsPerDay('nope')).toBe(0)
  })
})
