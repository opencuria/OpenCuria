import type { ScheduledTaskInput } from '@/services/scheduledTasks.api'

export const WEEKDAYS = [
  { value: 0, label: 'Mon', full: 'Monday' },
  { value: 1, label: 'Tue', full: 'Tuesday' },
  { value: 2, label: 'Wed', full: 'Wednesday' },
  { value: 3, label: 'Thu', full: 'Thursday' },
  { value: 4, label: 'Fri', full: 'Friday' },
  { value: 5, label: 'Sat', full: 'Saturday' },
  { value: 6, label: 'Sun', full: 'Sunday' },
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
 * Find the next valid local wall-clock occurrence. Candidate UTC instants are
 * derived from the zone's actual offsets around the target date, then round-
 * tripped through Intl. This skips spring-forward gaps and picks the earlier
 * UTC instant in a fall-back fold, matching the backend's fold=0 policy.
 */
export function nextLocalOccurrence(
  recurrence: 'daily' | 'weekly',
  weekdays: number[],
  localTime: string,
  timezone: string,
  now = new Date(),
): Date | null {
  if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(localTime) || !isValidTimeZone(timezone) || Number.isNaN(now.getTime())) return null
  if (recurrence === 'weekly' && (!weekdays.length || weekdays.some((day) => !Number.isInteger(day) || day < 0 || day > 6))) return null

  const [hours, minutes] = localTime.split(':').map(Number) as [number, number]
  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone: timezone,
    year: 'numeric', month: 'numeric', day: 'numeric',
    hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
  })
  const localParts = (instant: Date): Record<string, number> => Object.fromEntries(
    formatter.formatToParts(instant).map((part) => [part.type, Number(part.value)]),
  )
  const nowLocal = localParts(now)
  const firstDay = new Date(Date.UTC(nowLocal.year!, nowLocal.month! - 1, nowLocal.day!))
  const weekdayFormatter = new Intl.DateTimeFormat('en-US', { weekday: 'short', timeZone: 'UTC' })
  const weekdayIndexes = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
  const afterMs = now.getTime()

  // Backend searches 15 local dates (today through two weeks ahead).
  for (let offset = 0; offset < 15; offset += 1) {
    const day = new Date(firstDay)
    day.setUTCDate(day.getUTCDate() + offset)
    const weekday = weekdayIndexes.indexOf(weekdayFormatter.format(day))
    if (recurrence === 'weekly' && !weekdays.includes(weekday)) continue

    const wallClockMs = Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), hours, minutes)
    const offsets = new Set<number>()
    // IANA offsets can change close to the target date. Sample a wide UTC
    // window around the wall clock, including both sides of nearby transitions.
    for (let deltaMinutes = -36 * 60; deltaMinutes <= 36 * 60; deltaMinutes += 30) {
      const sampleMs = wallClockMs + deltaMinutes * 60_000
      const sample = localParts(new Date(sampleMs))
      const displayedAsUtc = Date.UTC(sample.year!, sample.month! - 1, sample.day!, sample.hour!, sample.minute!)
      offsets.add(displayedAsUtc - sampleMs)
    }

    const matches: number[] = []
    for (const offsetMs of offsets) {
      const candidateMs = wallClockMs - offsetMs
      const actual = localParts(new Date(candidateMs))
      if (actual.year === day.getUTCFullYear()
        && actual.month === day.getUTCMonth() + 1
        && actual.day === day.getUTCDate()
        && actual.hour === hours
        && actual.minute === minutes) {
        matches.push(candidateMs)
      }
    }
    // An ambiguous fall-back local time has two matches; the first is fold=0.
    // If that instant has passed, skip the local occurrence rather than run
    // it a second time during the repeated hour.
    const candidateMs = matches.sort((a, b) => a - b)[0]
    if (candidateMs !== undefined && candidateMs > afterMs) return new Date(candidateMs)
  }
  return null
}
