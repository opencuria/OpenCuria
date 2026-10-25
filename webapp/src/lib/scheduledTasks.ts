import type { ScheduledTask, ScheduledTaskInput } from '@/services/scheduledTasks.api'
import {
  cronMatchesDate,
  cronOccurrencesBetween,
  describeCron,
  describeCronWeekdays,
  formatClock,
  nextCronOccurrences,
  parseCron,
  parseCronField,
  splitCronExpressions,
  validateCronExpression,
} from '@/lib/cron'

export const WEEKDAYS = [
  { value: 0, label: 'Mon', full: 'Monday' },
  { value: 1, label: 'Tue', full: 'Tuesday' },
  { value: 2, label: 'Wed', full: 'Wednesday' },
  { value: 3, label: 'Thu', full: 'Thursday' },
  { value: 4, label: 'Fri', full: 'Friday' },
  { value: 5, label: 'Sat', full: 'Saturday' },
  { value: 6, label: 'Sun', full: 'Sunday' },
] as const

/** Suggestions for time zone pickers; any valid IANA zone is accepted. */
export const COMMON_TIME_ZONES = [
  'UTC',
  'Europe/London',
  'Europe/Berlin',
  'America/New_York',
  'America/Los_Angeles',
  'Asia/Tokyo',
] as const

export interface ScheduleValidation {
  valid: boolean
  errors: Partial<Record<'name' | 'workspace_id' | 'prompt' | 'weekdays' | 'timezone_name', string>>
}

export function isValidTimeZone(value: string): boolean {
  try {
    new Intl.DateTimeFormat('en', { timeZone: value })
    return value.trim() === value && value.length > 0 && value.length <= 64
  } catch {
    return false
  }
}

export function validateSchedule(input: Pick<ScheduledTaskInput,
  'name' | 'workspace_id' | 'prompt' | 'recurrence' | 'weekdays' | 'timezone_name'>,
): ScheduleValidation {
  const errors: ScheduleValidation['errors'] = {}
  if (!input.name.trim()) errors.name = 'Give this schedule a name.'
  else if (input.name.trim().length > 255) errors.name = 'Use 255 characters or fewer.'
  if (!input.workspace_id) errors.workspace_id = 'Choose a workspace.'
  if (!input.prompt.trim()) errors.prompt = 'Add a prompt for the agent.'
  if (input.recurrence === 'weekly' && input.weekdays.length === 0) {
    errors.weekdays = 'Choose at least one day.'
  }
  if (!isValidTimeZone(input.timezone_name)) errors.timezone_name = 'Enter a valid IANA time zone, such as Europe/Berlin.'
  return { valid: Object.keys(errors).length === 0, errors }
}

/**
 * Find the next valid local wall-clock occurrence. Spring-forward gaps are
 * skipped and a fall-back fold runs once at the earlier UTC instant, matching
 * the backend's fold=0 policy.
 */
export function nextLocalOccurrence(
  recurrence: 'daily' | 'weekly',
  weekdays: number[],
  localTime: string,
  timezone: string,
  now = new Date(),
): Date | null {
  if (!CLOCK.test(localTime) || !isValidTimeZone(timezone)) return null
  if (recurrence === 'weekly' && (!weekdays.length || weekdays.some((day) => !Number.isInteger(day) || day < 0 || day > 6))) return null
  const [hours, minutes] = localTime.split(':').map(Number) as [number, number]
  const days = recurrence === 'daily' ? '*' : cronWeekdayField(weekdays)
  return nextCronOccurrences(`${minutes} ${hours} * * ${days}`, timezone, now)[0] ?? null
}

// ---------------------------------------------------------------------------
// Schedule drafts: the dialog's friendly modes, stored as once, daily/weekly
// or cron
// ---------------------------------------------------------------------------

/** Stored schedule fields of a task. */
export type ScheduleFields = Pick<
  ScheduledTask,
  'recurrence' | 'weekdays' | 'local_time' | 'cron_expression' | 'run_date'
>

export type ScheduleMode = 'once' | 'times' | 'interval' | 'cron'

export const INTERVAL_OPTIONS = [
  { value: '5m', label: '5 minutes', minutes: 5 },
  { value: '10m', label: '10 minutes', minutes: 10 },
  { value: '15m', label: '15 minutes', minutes: 15 },
  { value: '20m', label: '20 minutes', minutes: 20 },
  { value: '30m', label: '30 minutes', minutes: 30 },
  { value: '1h', label: 'hour', minutes: 60 },
  { value: '2h', label: '2 hours', minutes: 120 },
  { value: '3h', label: '3 hours', minutes: 180 },
  { value: '4h', label: '4 hours', minutes: 240 },
  { value: '6h', label: '6 hours', minutes: 360 },
  { value: '8h', label: '8 hours', minutes: 480 },
  { value: '12h', label: '12 hours', minutes: 720 },
] as const

export const CRON_PRESETS = [
  { label: 'Every 15 minutes', expression: '*/15 * * * *' },
  { label: 'Hourly on weekdays, 9–17', expression: '0 9-17 * * 1-5' },
  { label: 'Twice a day', expression: '0 9,17 * * *' },
  { label: '1st of every month', expression: '0 9 1 * *' },
] as const

export const MAX_SCHEDULE_TIMES = 24

export interface ScheduleDraft {
  mode: ScheduleMode
  /** Local YYYY-MM-DD date; used by `once` together with the first time. */
  date: string
  /** 0 = Monday … 6 = Sunday; used by `times` and `interval`. */
  weekdays: number[]
  /** HH:MM wall-clock times; used by `times`, the first one by `once`. */
  times: string[]
  interval: string
  window_enabled: boolean
  /** Active hours for `interval`: start hour 0-23, end hour 1-24. */
  window_start: number
  window_end: number
  cron_expression: string
}

export type ScheduleDraftErrors = Partial<
  Record<'date' | 'weekdays' | 'times' | 'window' | 'cron_expression', string>
>

const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/
const DATE = /^(\d{4})-(\d{2})-(\d{2})$/
const ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
const DAY_MS = 86_400_000

/** Whether `value` is a real YYYY-MM-DD calendar date. */
export function isValidDate(value: string): boolean {
  const match = DATE.exec(value)
  if (!match) return false
  const [year, month, day] = match.slice(1).map(Number) as [number, number, number]
  const date = new Date(Date.UTC(year, month - 1, day))
  return (
    date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
  )
}

function localDate(instant: Date, timezone: string): string {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    })
      .formatToParts(instant)
      .map((part) => [part.type, part.value]),
  )
  return `${parts.year}-${parts.month}-${parts.day}`
}

/** Local YYYY-MM-DD date of the next `time` (HH:MM) after `now` in `timezone`. */
export function nextDateAt(time: string, timezone: string, now = new Date()): string {
  const zone = isValidTimeZone(timezone) ? timezone : 'UTC'
  const [hour, minute] = CLOCK.test(time) ? time.split(':').map(Number) : [9, 0]
  return localDate(nextCronOccurrences(`${minute} ${hour} * * *`, zone, now)[0] ?? now, zone)
}

/**
 * Instant of a one-time run, or null for an invalid date or time or a wall
 * time inside a DST gap (which the backend rejects). The date's yearly cron is
 * searched only around that date to reuse the zone-aware cron resolution, so
 * an ambiguous fall-back time runs at its first instant, as in the backend.
 */
export function onceOccurrence(date: string, time: string, timezone: string): Date | null {
  if (!isValidDate(date) || !CLOCK.test(time)) return null
  const [, month, day] = date.split('-').map(Number) as [number, number, number]
  const [hour, minute] = time.split(':').map(Number) as [number, number]
  const midnight = new Date(`${date}T00:00:00Z`).getTime()
  return (
    cronOccurrencesBetween(
      `${minute} ${hour} ${day} ${month} *`,
      timezone,
      new Date(midnight - DAY_MS),
      new Date(midnight + 2 * DAY_MS),
      1,
    )[0] ?? null
  )
}

/** A one-time task whose run time has passed; it will not run again. */
export function isScheduleEnded(
  task: Pick<ScheduledTask, 'recurrence' | 'run_date' | 'local_time' | 'timezone_name'>,
  now = new Date(),
): boolean {
  if (task.recurrence !== 'once') return false
  const at = onceOccurrence(task.run_date ?? '', task.local_time, task.timezone_name)
  return !at || at <= now
}

/** Running dispatches stay in navigation until their ledger settles. */
export function isScheduleVisibleInSidebar(task: ScheduledTask, now = new Date()): boolean {
  return !!task.is_running || task.enabled || !isScheduleEnded(task, now)
}

/** Run state takes precedence over the schedule's enabled flag. */
export function scheduleStateLabel(
  task: Pick<
    ScheduledTask,
    'enabled' | 'is_running' | 'recurrence' | 'run_date' | 'local_time' | 'timezone_name'
  >,
  now = new Date(),
): '' | 'Paused' | 'Ended' | 'Running' {
  if (task.is_running) return 'Running'
  if (isScheduleEnded(task, now)) return 'Ended'
  return task.enabled ? '' : 'Paused'
}

/** New tasks run once, at the next 09:00 in `timezone`. */
export function defaultScheduleDraft(
  timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
): ScheduleDraft {
  return {
    mode: 'once',
    date: nextDateAt('09:00', timezone),
    weekdays: [...ALL_DAYS],
    times: ['09:00'],
    interval: '1h',
    window_enabled: false,
    window_start: 9,
    window_end: 17,
    cron_expression: '',
  }
}

/** Cron day-of-week field for UI weekdays (0 = Monday); ranges for ≥3 days. */
export function cronWeekdayField(weekdays: number[]): string {
  const days = [...new Set(weekdays.map((day) => (day + 1) % 7))].sort((a, b) => a - b)
  if (days.length >= 7) return '*'
  const parts: string[] = []
  for (let index = 0; index < days.length;) {
    let end = index
    while (end + 1 < days.length && days[end + 1] === days[end]! + 1) end += 1
    if (end - index >= 2) parts.push(`${days[index]}-${days[end]}`)
    else for (let item = index; item <= end; item += 1) parts.push(String(days[item]))
    index = end + 1
  }
  return parts.join(',')
}

function uiWeekdays(field: string): number[] | null {
  if (field === '*') return [...ALL_DAYS]
  try {
    const days = parseCronField(field, 0, 7, 'day-of-week', ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT'])
    return [...new Set([...days].map((day) => (day + 6) % 7))].sort((a, b) => a - b)
  } catch {
    return null
  }
}

function sortedTimes(times: string[]): string[] {
  return [...new Set(times)].sort()
}

function intervalMinutes(value: string): number {
  return INTERVAL_OPTIONS.find((option) => option.value === value)?.minutes ?? 60
}

/**
 * Build the cron expression equivalent to a draft in any mode. A one-time run
 * maps to its yearly date and time, a starting point for the cron editor.
 */
export function scheduleDraftToCron(draft: ScheduleDraft): string {
  if (draft.mode === 'cron') return splitCronExpressions(draft.cron_expression).join('; ')
  if (draft.mode === 'once') {
    const time = draft.times[0] ?? ''
    if (!CLOCK.test(time) || !isValidDate(draft.date)) return ''
    const [, month, day] = draft.date.split('-').map(Number)
    const [hour, minute] = time.split(':').map(Number)
    return `${minute} ${hour} ${day} ${month} *`
  }
  const days = cronWeekdayField(draft.weekdays)
  if (draft.mode === 'interval') {
    const every = intervalMinutes(draft.interval)
    const { window_enabled: windowed, window_start: start, window_end: end } = draft
    if (every < 60) {
      const hours = !windowed ? '*' : end - 1 === start ? `${start}` : `${start}-${end - 1}`
      return `*/${every} ${hours} * * ${days}`
    }
    const hours = windowed ? `${start}-${Math.min(end, 23)}` : '*'
    return `0 ${hours}/${every / 60} * * ${days}`
  }
  const minutesByHour = new Map<number, number[]>()
  for (const time of sortedTimes(draft.times.filter((value) => CLOCK.test(value)))) {
    const [hour, minute] = time.split(':').map(Number) as [number, number]
    minutesByHour.set(hour, [...(minutesByHour.get(hour) ?? []), minute])
  }
  // Hours sharing the same minutes collapse into one expression.
  const groups = new Map<string, number[]>()
  for (const [hour, minutes] of minutesByHour) {
    const key = minutes.join(',')
    groups.set(key, [...(groups.get(key) ?? []), hour])
  }
  return [...groups].map(([minutes, hours]) => `${minutes} ${hours.join(',')} * * ${days}`).join('; ')
}

function parseTimesDraft(parts: string[]): ScheduleDraft | null {
  let dayField: string | null = null
  const times: string[] = []
  for (const part of parts) {
    const match = /^(\d+(?:,\d+)*) (\d+(?:,\d+)*) \* \* (\S+)$/.exec(part)
    if (!match || (dayField !== null && dayField !== match[3])) return null
    dayField = match[3]!
    for (const hour of match[2]!.split(',').map(Number)) {
      for (const minute of match[1]!.split(',').map(Number)) {
        if (hour > 23 || minute > 59) return null
        times.push(formatClock(hour, minute))
      }
    }
  }
  const weekdays = dayField === null ? null : uiWeekdays(dayField)
  if (!weekdays?.length || times.length > MAX_SCHEDULE_TIMES) return null
  return { ...defaultScheduleDraft(), mode: 'times', weekdays, times: sortedTimes(times) }
}

function parseIntervalDraft(part: string): ScheduleDraft | null {
  const minutely = /^\*\/(\d+) (\*|\d+|\d+-\d+) \* \* (\S+)$/.exec(part)
  const hourly = /^0 (\*|\d+-\d+)\/(\d+) \* \* (\S+)$/.exec(part)
  const match = minutely ?? hourly
  if (!match) return null
  const every = minutely ? Number(minutely[1]) : Number(hourly![2]) * 60
  const option = INTERVAL_OPTIONS.find((item) => item.minutes === every)
  const hourField = minutely ? minutely[2]! : hourly![1]!
  const weekdays = uiWeekdays(match[3]!)
  if (!option || !weekdays?.length) return null
  const draft: ScheduleDraft = { ...defaultScheduleDraft(), mode: 'interval', interval: option.value, weekdays }
  if (hourField !== '*') {
    const [start, last = start] = hourField.split('-').map(Number) as [number, number?]
    draft.window_enabled = true
    draft.window_start = start
    draft.window_end = minutely ? last + 1 : last
  }
  return draft
}

/** Derive the friendliest editor mode that reproduces a stored schedule. */
export function scheduleDraftFromTask(task: ScheduleFields): ScheduleDraft {
  if (task.recurrence === 'once') {
    return {
      ...defaultScheduleDraft(),
      mode: 'once',
      date: task.run_date ?? '',
      times: [task.local_time],
    }
  }
  if (task.recurrence !== 'cron') {
    return {
      ...defaultScheduleDraft(),
      mode: 'times',
      weekdays: task.recurrence === 'daily' ? [...ALL_DAYS] : [...task.weekdays],
      times: [task.local_time],
    }
  }
  const expression = task.cron_expression ?? ''
  const parts = splitCronExpressions(expression)
  const canonical = parts.join('; ')
  for (const candidate of [parts.length === 1 ? parseIntervalDraft(parts[0]!) : null, parseTimesDraft(parts)]) {
    if (candidate && scheduleDraftToCron(candidate) === canonical) return candidate
  }
  return { ...defaultScheduleDraft(), mode: 'cron', cron_expression: canonical }
}

export function validateScheduleDraft(draft: ScheduleDraft): ScheduleDraftErrors {
  const errors: ScheduleDraftErrors = {}
  if (draft.mode === 'once') {
    if (!isValidDate(draft.date)) errors.date = 'Choose a date.'
    if (!CLOCK.test(draft.times[0] ?? '')) errors.times = 'Enter a valid time.'
    return errors
  }
  if (draft.mode === 'cron') {
    const error = validateCronExpression(draft.cron_expression)
    if (error) errors.cron_expression = error
    return errors
  }
  if (!draft.weekdays.length) errors.weekdays = 'Choose at least one day.'
  if (draft.mode === 'times') {
    if (!draft.times.length) errors.times = 'Add at least one time.'
    else if (draft.times.some((time) => !CLOCK.test(time))) errors.times = 'Enter a valid time.'
    else if (draft.times.length > MAX_SCHEDULE_TIMES) errors.times = `Use at most ${MAX_SCHEDULE_TIMES} times.`
  } else if (draft.window_enabled && draft.window_end <= draft.window_start) {
    errors.window = 'End must be after start.'
  }
  return errors
}

/** API fields for a draft; a single daily time keeps the classic format. */
export function schedulePayload(
  draft: ScheduleDraft,
): Pick<
  ScheduledTaskInput,
  'recurrence' | 'weekdays' | 'local_time' | 'cron_expression' | 'run_date'
> {
  if (draft.mode === 'once') {
    return {
      recurrence: 'once',
      weekdays: [],
      local_time: draft.times[0] ?? '09:00',
      cron_expression: '',
      run_date: draft.date,
    }
  }
  const times = sortedTimes(draft.times)
  if (draft.mode === 'times' && times.length === 1) {
    const daily = ALL_DAYS.every((day) => draft.weekdays.includes(day))
    return {
      recurrence: daily ? 'daily' : 'weekly',
      weekdays: daily ? [] : [...draft.weekdays].sort((a, b) => a - b),
      local_time: times[0]!,
      cron_expression: '',
      run_date: null,
    }
  }
  return {
    recurrence: 'cron',
    weekdays: [],
    local_time: times.find((time) => CLOCK.test(time)) ?? '09:00',
    cron_expression: scheduleDraftToCron(draft),
    run_date: null,
  }
}

function uiDaysLabel(weekdays: number[]): string {
  return describeCronWeekdays(weekdays.map((day) => (day + 1) % 7))
}

function hourLabel(hour: number): string {
  return `${String(hour).padStart(2, '0')}:00`
}

/** Compact cadence label such as "Weekdays · 09:00, 14:30" or "Once · Oct 25, 2026 · 09:00". */
export function describeScheduleDraft(draft: ScheduleDraft): string {
  if (draft.mode === 'cron') return describeCron(draft.cron_expression)
  if (draft.mode === 'once') {
    const time = draft.times[0] ?? ''
    if (!isValidDate(draft.date)) return `Once · ${time}`
    const day = new Intl.DateTimeFormat(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone: 'UTC',
    }).format(new Date(`${draft.date}T00:00:00Z`))
    return `Once · ${day} · ${time}`
  }
  const days = uiDaysLabel(draft.weekdays)
  if (draft.mode === 'times') return `${days} · ${sortedTimes(draft.times).join(', ')}`
  const every = intervalMinutes(draft.interval)
  const cadence = every < 60 ? `Every ${every} min` : every === 60 ? 'Every hour' : `Every ${every / 60} h`
  const window = draft.window_enabled ? `, ${hourLabel(draft.window_start)}–${hourLabel(draft.window_end)}` : ''
  return `${days} · ${cadence}${window}`
}

export function describeSchedule(task: ScheduleFields): string {
  return describeScheduleDraft(scheduleDraftFromTask(task))
}

/** Most distinct wall-clock runs on any calendar day within the next year. */
export function maxRunsPerDay(expression: string, now = new Date()): number {
  try {
    const specs = splitCronExpressions(expression).map(parseCron)
    let most = 0
    for (let offset = 0; offset < 366; offset += 1) {
      const date = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + offset))
      const walls = new Set<number>()
      for (const spec of specs) {
        if (!cronMatchesDate(spec, date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate())) continue
        for (const hour of spec.hours) for (const minute of spec.minutes) walls.add(hour * 60 + minute)
      }
      most = Math.max(most, walls.size)
    }
    return most
  } catch {
    return 0
  }
}
