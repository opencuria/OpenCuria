/**
 * Five-field cron support mirroring backend/apps/scheduled_tasks/recurrence.py:
 * Vixie day-of-month/day-of-week semantics, names, macros, `;`-separated
 * expressions, and wall-clock evaluation in an IANA zone (gaps skipped,
 * ambiguous times run once at the earlier instant).
 */

export interface CronSpec {
  minutes: number[]
  hours: number[]
  days: Set<number>
  months: Set<number>
  /** 0 = Sunday … 6 = Saturday */
  weekdays: Set<number>
  dayRestricted: boolean
  weekdayRestricted: boolean
  monthRestricted: boolean
}

export const MAX_CRON_EXPRESSIONS = 32
export const MAX_CRON_LENGTH = 1000
const SEARCH_DAYS = 366 * 9

const MONTH_NAMES = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
const DAY_NAMES = ['SUN', 'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT']
const MACROS: Record<string, string> = {
  '@yearly': '0 0 1 1 *',
  '@annually': '0 0 1 1 *',
  '@monthly': '0 0 1 * *',
  '@weekly': '0 0 * * 0',
  '@daily': '0 0 * * *',
  '@midnight': '0 0 * * *',
  '@hourly': '0 * * * *',
}

function parseValue(raw: string, label: string, names?: string[], offset = 0): number {
  const named = names?.indexOf(raw.toUpperCase()) ?? -1
  if (named >= 0) return named + offset
  if (!/^\d+$/.test(raw)) throw new Error(`Invalid ${label} value “${raw}”.`)
  return Number(raw)
}

export function parseCronField(
  raw: string,
  low: number,
  high: number,
  label: string,
  names?: string[],
  nameOffset = 0,
): Set<number> {
  const values = new Set<number>()
  for (const part of raw.split(',')) {
    if (!part) throw new Error(`Empty list item in the ${label} field.`)
    const [base = '', stepRaw, extra] = part.split('/')
    if (extra !== undefined) throw new Error(`Invalid step in the ${label} field.`)
    let step = 1
    if (stepRaw !== undefined) {
      if (!/^\d+$/.test(stepRaw) || Number(stepRaw) < 1) throw new Error(`Invalid step “/${stepRaw}” in the ${label} field.`)
      step = Number(stepRaw)
    }
    let start: number
    let end: number
    if (base === '*' || base === '?') {
      start = low
      end = high
    } else if (base.includes('-')) {
      const [startRaw = '', endRaw = ''] = base.split('-', 2)
      start = parseValue(startRaw, label, names, nameOffset)
      end = parseValue(endRaw, label, names, nameOffset)
    } else {
      start = parseValue(base, label, names, nameOffset)
      end = stepRaw !== undefined ? high : start
    }
    if (start < low || start > high || end < low || end > high) {
      throw new Error(`${label[0]!.toUpperCase()}${label.slice(1)} values must be between ${low} and ${high}.`)
    }
    if (start > end) throw new Error(`Invalid range “${base}” in the ${label} field.`)
    for (let value = start; value <= end; value += step) values.add(value)
  }
  return values
}

const sortNumbers = (values: Iterable<number>): number[] => [...values].sort((a, b) => a - b)

export function parseCron(expression: string): CronSpec {
  let text = expression.trim().split(/\s+/).join(' ')
  text = MACROS[text.toLowerCase()] ?? text
  const fields = text.split(' ')
  if (fields.length !== 5) {
    throw new Error('Use 5 fields: minute hour day-of-month month day-of-week.')
  }
  const [minute, hour, dom, month, dow] = fields as [string, string, string, string, string]
  const weekdays = parseCronField(dow, 0, 7, 'day-of-week', DAY_NAMES)
  if (weekdays.delete(7)) weekdays.add(0)
  return {
    minutes: sortNumbers(parseCronField(minute, 0, 59, 'minute')),
    hours: sortNumbers(parseCronField(hour, 0, 23, 'hour')),
    days: parseCronField(dom, 1, 31, 'day-of-month'),
    months: parseCronField(month, 1, 12, 'month', MONTH_NAMES, 1),
    weekdays,
    dayRestricted: !/^[*?]/.test(dom),
    weekdayRestricted: !/^[*?]/.test(dow),
    monthRestricted: !/^[*?]/.test(month),
  }
}

export function splitCronExpressions(value: string): string[] {
  return value
    .split(/[;\n]/)
    .map((part) => part.trim().split(/\s+/).join(' '))
    .filter(Boolean)
}

/** `year`, `month` (1-12), `day` of a local calendar date. */
export function cronMatchesDate(spec: CronSpec, year: number, month: number, day: number): boolean {
  if (!spec.months.has(month)) return false
  const dom = spec.days.has(day)
  const dow = spec.weekdays.has(new Date(Date.UTC(year, month - 1, day)).getUTCDay())
  return spec.dayRestricted && spec.weekdayRestricted ? dom || dow : dom && dow
}

/** Return a user-facing error for an invalid expression list, or null. */
export function validateCronExpression(value: string): string | null {
  if (!value.trim()) return 'Enter a cron expression.'
  if (value.length > MAX_CRON_LENGTH) return `Use ${MAX_CRON_LENGTH} characters or fewer.`
  const parts = splitCronExpressions(value)
  if (parts.length > MAX_CRON_EXPRESSIONS) return `Use at most ${MAX_CRON_EXPRESSIONS} expressions.`
  const today = new Date()
  for (const part of parts) {
    let spec: CronSpec
    try {
      spec = parseCron(part)
    } catch (error) {
      return parts.length > 1 ? `“${part}”: ${(error as Error).message}` : (error as Error).message
    }
    let matches = false
    for (let offset = 0; offset < SEARCH_DAYS && !matches; offset += 1) {
      const day = new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate() + offset))
      matches = cronMatchesDate(spec, day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate())
    }
    if (!matches) return `“${part}” never matches a date.`
  }
  return null
}

type LocalParts = Record<'year' | 'month' | 'day' | 'hour' | 'minute', number>

function zoneFormatter(timezone: string): ((instant: Date) => LocalParts) | null {
  let formatter: Intl.DateTimeFormat
  try {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      year: 'numeric', month: 'numeric', day: 'numeric',
      hour: 'numeric', minute: 'numeric', hourCycle: 'h23',
    })
  } catch {
    return null
  }
  return (instant) => Object.fromEntries(
    formatter.formatToParts(instant).map((part) => [part.type, Number(part.value)]),
  ) as LocalParts
}

/**
 * Upcoming occurrences strictly after `now`. Candidate UTC instants are derived
 * from the zone's actual offsets around each matching date, then round-tripped
 * through Intl; spring-forward gaps are skipped and a fall-back fold runs once.
 */
export function nextCronOccurrences(
  expression: string,
  timezone: string,
  now = new Date(),
  count = 1,
): Date[] {
  const localParts = zoneFormatter(timezone)
  if (!localParts || Number.isNaN(now.getTime()) || count < 1) return []
  let specs: CronSpec[]
  try {
    specs = splitCronExpressions(expression).map(parseCron)
  } catch {
    return []
  }
  if (!specs.length) return []
  const nowLocal = localParts(now)
  const afterMs = now.getTime()
  const results: Date[] = []

  for (let offset = 0; offset < SEARCH_DAYS && results.length < count; offset += 1) {
    const date = new Date(Date.UTC(nowLocal.year, nowLocal.month - 1, nowLocal.day + offset))
    const [year, month, day] = [date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate()]
    const wallTimes = new Set<number>()
    for (const spec of specs) {
      if (!cronMatchesDate(spec, year, month, day)) continue
      for (const hour of spec.hours) for (const minute of spec.minutes) wallTimes.add(hour * 60 + minute)
    }
    if (!wallTimes.size) continue

    const midnightMs = Date.UTC(year, month - 1, day)
    const offsets = new Set<number>()
    // IANA offsets can change close to the target date. Sample a wide UTC
    // window around the day, including both sides of nearby transitions.
    for (let deltaMinutes = -36 * 60; deltaMinutes <= 60 * 60; deltaMinutes += 30) {
      const sampleMs = midnightMs + deltaMinutes * 60_000
      const sample = localParts(new Date(sampleMs))
      offsets.add(Date.UTC(sample.year, sample.month - 1, sample.day, sample.hour, sample.minute) - sampleMs)
    }

    const dayResults: number[] = []
    for (const wall of sortNumbers(wallTimes)) {
      const wallMs = midnightMs + wall * 60_000
      const candidates = [...offsets].map((offsetMs) => wallMs - offsetMs)
      if (candidates.every((candidateMs) => candidateMs <= afterMs)) continue
      const matches: number[] = []
      for (const candidateMs of candidates) {
        const actual = localParts(new Date(candidateMs))
        if (actual.year === year && actual.month === month && actual.day === day
          && actual.hour * 60 + actual.minute === wall) matches.push(candidateMs)
      }
      // An ambiguous fall-back time has two matches; only the first (fold=0)
      // runs. If it has passed, skip rather than run in the repeated hour.
      const first = matches.sort((a, b) => a - b)[0]
      if (first !== undefined && first > afterMs) {
        dayResults.push(first)
        if (results.length + dayResults.length >= count) break
      }
    }
    results.push(...dayResults.sort((a, b) => a - b).map((ms) => new Date(ms)))
  }
  return results.slice(0, count)
}

// ---------------------------------------------------------------------------
// Human-readable summaries
// ---------------------------------------------------------------------------

const SHORT_DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
const SHORT_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const pad = (value: number): string => String(value).padStart(2, '0')
export const formatClock = (hour: number, minute: number): string => `${pad(hour)}:${pad(minute)}`

/** Label for cron weekdays (0 = Sunday), e.g. "Daily", "Weekdays", "Mon, Wed". */
export function describeCronWeekdays(days: Iterable<number>): string {
  const set = new Set(days)
  if (set.size === 7) return 'Daily'
  if (set.size === 5 && [1, 2, 3, 4, 5].every((day) => set.has(day))) return 'Weekdays'
  if (set.size === 2 && set.has(0) && set.has(6)) return 'Weekends'
  return [1, 2, 3, 4, 5, 6, 0].filter((day) => set.has(day)).map((day) => SHORT_DAYS[day]).join(', ')
}

function arithmeticStep(values: number[]): number | null {
  if (values.length < 2) return null
  const step = values[1]! - values[0]!
  return values.every((value, index) => index === 0 || value - values[index - 1]! === step) ? step : null
}

function describeTimes(spec: CronSpec): string {
  const { minutes, hours } = spec
  if (minutes.length * hours.length <= 4) {
    return hours.flatMap((hour) => minutes.map((minute) => formatClock(hour, minute))).join(', ')
  }
  const fullDay = hours.length === 24
  const range = arithmeticStep(hours) === 1
    ? `${formatClock(hours[0]!, 0)}–${formatClock(hours[hours.length - 1]!, 59)}`
    : null
  if (minutes.length === 1) {
    const hourStep = arithmeticStep(hours)
    const at = minutes[0] ? ` at :${pad(minutes[0])}` : ''
    if (fullDay) return `Every hour${at}`
    if (hourStep) {
      const spansDay = hours[0]! < hourStep && hours[hours.length - 1]! + hourStep > 23
      const window = spansDay ? '' : `, ${formatClock(hours[0]!, minutes[0]!)}–${formatClock(hours[hours.length - 1]!, minutes[0]!)}`
      return `Every ${hourStep === 1 ? 'hour' : `${hourStep} h`}${at}${window}`
    }
    return `At :${pad(minutes[0]!)} past ${hours.map(String).join(', ')}`
  }
  const minuteStep = arithmeticStep(minutes)
  const everyMinutes = minutes.length === 60
    ? 'Every minute'
    : minuteStep && minutes[0]! < minuteStep && minutes[minutes.length - 1]! + minuteStep >= 60
      ? `Every ${minuteStep} min`
      : `At :${minutes.map(pad).join(', :')}`
  if (fullDay) return everyMinutes
  return `${everyMinutes}, ${range ?? `hours ${hours.join(', ')}`}`
}

function describeDays(spec: CronSpec): string {
  const parts: string[] = []
  const days = sortNumbers(spec.days)
  const dayList = `day ${days.join(', ')}`
  if (spec.dayRestricted && spec.weekdayRestricted) {
    parts.push(`${describeCronWeekdays(spec.weekdays)} or ${dayList} of month`)
  } else if (spec.dayRestricted) {
    parts.push(`${dayList[0]!.toUpperCase()}${dayList.slice(1)} of month`)
  } else {
    parts.push(describeCronWeekdays(spec.weekdays))
  }
  if (spec.monthRestricted) {
    parts.push(`in ${sortNumbers(spec.months).map((month) => SHORT_MONTHS[month - 1]).join(', ')}`)
  }
  return parts.join(' ')
}

/** Short description like "Weekdays · Every 15 min, 08:00–17:59". */
export function describeCron(expression: string): string {
  try {
    const parts = splitCronExpressions(expression)
    if (!parts.length) return 'Custom schedule'
    return parts.map((part) => {
      const spec = parseCron(part)
      return `${describeDays(spec)} · ${describeTimes(spec)}`
    }).join('; ')
  } catch {
    return 'Custom schedule'
  }
}
