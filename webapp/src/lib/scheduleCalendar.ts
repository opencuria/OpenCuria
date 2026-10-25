/**
 * Calendar model for the scheduled-tasks week view: planned occurrences and
 * past runs placed on the days of a display time zone, dense cadences merged
 * into series blocks, overlap columns, and drag-to-reschedule for one-time
 * and set-time schedules.
 */
import type {
  ScheduledTask,
  ScheduledTaskInput,
  ScheduledTaskRun,
} from '@/services/scheduledTasks.api'
import { cronOccurrencesBetween, formatClock } from '@/lib/cron'
import {
  onceOccurrence,
  scheduleDraftFromTask,
  scheduleDraftToCron,
  schedulePayload,
} from '@/lib/scheduledTasks'
import type { ScheduleDraft, ScheduleFields } from '@/lib/scheduledTasks'

export const MINUTES_PER_DAY = 24 * 60
/** Shortest drawn event; used for overlap layout so chips never cover each other. */
export const MIN_EVENT_MINUTES = 30
/** Runs closer than a drawn chip merge into one series band. */
const SERIES_GAP_MINUTES = MIN_EVENT_MINUTES
const SERIES_MIN_RUNS = 3
/** `--git-branch-N` theme colors, ordered for contrast; red last (reads as failure). */
const WORKSPACE_PALETTE = [1, 2, 5, 4, 3, 7, 8, 6]

export type CalendarEntryKind = 'planned' | 'run'

export interface CalendarOccurrence {
  taskId: string
  at: Date
  kind: CalendarEntryKind
  run?: ScheduledTaskRun
}

export interface CalendarBlock {
  key: string
  taskId: string
  kind: CalendarEntryKind
  /** Index into the visible days. */
  day: number
  /** Minutes after midnight (display zone) of the first and last occurrence. */
  start: number
  end: number
  /** Smallest gap between the runs of a series; 0 for a single run. */
  step: number
  occurrences: CalendarOccurrence[]
  column: number
  columns: number
  /** Single runs drawn over a series band leave the band's label side free. */
  inset: boolean
}

export type SchedulePatch = Pick<
  ScheduledTaskInput,
  'recurrence' | 'weekdays' | 'local_time' | 'cron_expression' | 'run_date'
>

/**
 * A calendar date without a time zone, stored as 00:00 UTC of that date so
 * day arithmetic never shifts across DST or the viewer's own zone.
 */
export type CalendarDay = Date

export function calendarDay(year: number, month: number, day: number): CalendarDay {
  return new Date(Date.UTC(year, month - 1, day))
}

export function addDays(day: CalendarDay, days: number): CalendarDay {
  return new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate() + days))
}

/** Monday of the week containing `day`. */
export function startOfWeek(day: CalendarDay): CalendarDay {
  return addDays(day, -weekdayIndex(day))
}

/** 0 = Monday … 6 = Sunday */
export function weekdayIndex(day: CalendarDay): number {
  return (day.getUTCDay() + 6) % 7
}

export function isSameDay(a: CalendarDay, b: CalendarDay): boolean {
  return a.getTime() === b.getTime()
}

/** YYYY-MM-DD of a calendar day. */
export function isoDate(day: CalendarDay): string {
  return day.toISOString().slice(0, 10)
}

export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
}

const zoneFormatters = new Map<string, Intl.DateTimeFormat>()

function zoneFormatter(zone: string): Intl.DateTimeFormat {
  let formatter = zoneFormatters.get(zone)
  if (!formatter) {
    formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      hourCycle: 'h23',
    })
    zoneFormatters.set(zone, formatter)
  }
  return formatter
}

/** Calendar day and minute of day of an instant in `zone`. */
export function zonedTime(instant: Date, zone: string): { day: CalendarDay; minute: number } {
  const parts = Object.fromEntries(
    zoneFormatter(zone)
      .formatToParts(instant)
      .map((part) => [part.type, Number(part.value)]),
  ) as Record<'year' | 'month' | 'day' | 'hour' | 'minute', number>
  return {
    day: calendarDay(parts.year, parts.month, parts.day),
    minute: parts.hour * 60 + parts.minute,
  }
}

function zoneOffsetMs(instantMs: number, zone: string): number {
  const { day, minute } = zonedTime(new Date(instantMs), zone)
  return day.getTime() + minute * 60_000 - Math.floor(instantMs / 60_000) * 60_000
}

/** Instant of a wall-clock minute on `day` in `zone` (DST gaps shift forward). */
export function zonedInstant(day: CalendarDay, minute: number, zone: string): Date {
  const wallMs = day.getTime() + minute * 60_000
  const first = wallMs - zoneOffsetMs(wallMs, zone)
  const second = wallMs - zoneOffsetMs(first, zone)
  const candidates = [first, second].filter((ms) => {
    const local = zonedTime(new Date(ms), zone)
    return local.day.getTime() + local.minute * 60_000 === wallMs
  })
  // Ambiguous fall-back times use the earlier instant; gap times the later one.
  return new Date(candidates.length ? Math.min(...candidates) : Math.max(first, second))
}

/** Short offset label of `zone` on `day`, such as "GMT+2" or "UTC". */
export function zoneOffsetLabel(day: CalendarDay, zone: string): string {
  return (
    new Intl.DateTimeFormat(undefined, { timeZone: zone, timeZoneName: 'short' })
      .formatToParts(zonedInstant(day, 12 * 60, zone))
      .find((part) => part.type === 'timeZoneName')?.value ?? zone
  )
}

/** Toolbar label such as "October 2026" or "Oct – Nov 2026". */
export function formatWeekLabel(start: CalendarDay, end: CalendarDay): string {
  const month = (day: CalendarDay, style: 'long' | 'short'): string =>
    new Intl.DateTimeFormat(undefined, { month: style, timeZone: 'UTC' }).format(day)
  const [startYear, endYear] = [start.getUTCFullYear(), end.getUTCFullYear()]
  if (startYear !== endYear)
    return `${month(start, 'short')} ${startYear} – ${month(end, 'short')} ${endYear}`
  if (start.getUTCMonth() !== end.getUTCMonth())
    return `${month(start, 'short')} – ${month(end, 'short')} ${endYear}`
  return `${month(start, 'long')} ${startYear}`
}

/** Five-field cron equivalent of a recurring stored schedule. */
export function taskCronExpression(task: ScheduleFields): string {
  if (task.recurrence === 'cron') return task.cron_expression ?? ''
  return scheduleDraftToCron(scheduleDraftFromTask(task))
}

/** Planned instants of a task within `[start, end)`. */
function plannedOccurrences(task: ScheduledTask, start: Date, end: Date): Date[] {
  if (task.recurrence === 'once') {
    const at = onceOccurrence(task.run_date ?? '', task.local_time, task.timezone_name)
    return at && at >= start && at < end ? [at] : []
  }
  return cronOccurrencesBetween(
    taskCronExpression(task),
    task.timezone_name,
    start,
    end,
    // The visible range bounds the search; a fixed cap drops dense schedules.
    Number.POSITIVE_INFINITY,
  )
}

/**
 * Full schedule within `[start, end)`, including elapsed slots without a loaded
 * run. Recorded runs replace matching slots and retain their actual outcome.
 */
export function calendarOccurrences(
  tasks: ScheduledTask[],
  runsByTask: Record<string, ScheduledTaskRun[] | undefined>,
  start: Date,
  end: Date,
): CalendarOccurrence[] {
  const result: CalendarOccurrence[] = []
  for (const task of tasks) {
    const taken = new Set<number>()
    for (const run of runsByTask[task.id] ?? []) {
      const at = new Date(run.scheduled_for)
      if (Number.isNaN(at.getTime()) || at < start || at >= end) continue
      taken.add(at.getTime())
      result.push({ taskId: task.id, at, kind: 'run', run })
    }
    for (const at of plannedOccurrences(task, start, end)) {
      if (!taken.has(at.getTime())) result.push({ taskId: task.id, at, kind: 'planned' })
    }
  }
  return result
}

/** Visual extent: a chip's minimum height, or a series up to one step after its last run. */
export function blockVisualEnd(block: Pick<CalendarBlock, 'start' | 'end' | 'step'>): number {
  return Math.min(
    Math.max(block.end + block.step, block.start + MIN_EVENT_MINUTES),
    MINUTES_PER_DAY,
  )
}

export function isSeries(block: Pick<CalendarBlock, 'occurrences'>): boolean {
  return block.occurrences.length > 1
}

/**
 * Layout for one day (mutates `blocks`): series are background bands, single
 * runs sit on top; each layer splits overlapping items into columns.
 */
export function layoutDayBlocks(blocks: CalendarBlock[]): CalendarBlock[] {
  const series = layoutColumns(blocks.filter(isSeries))
  const singles = layoutColumns(blocks.filter((block) => !isSeries(block)))
  for (const single of singles) {
    single.inset = series.some(
      (band) => band.start < blockVisualEnd(single) && single.start < blockVisualEnd(band),
    )
  }
  return [...series, ...singles]
}

function layoutColumns(blocks: CalendarBlock[]): CalendarBlock[] {
  const sorted = [...blocks].sort(
    (a, b) =>
      a.start - b.start || b.end - b.start - (a.end - a.start) || a.key.localeCompare(b.key),
  )
  let cluster: CalendarBlock[] = []
  let columnEnds: number[] = []
  let clusterEnd = -1
  const flush = (): void => {
    for (const block of cluster) block.columns = columnEnds.length
    cluster = []
    columnEnds = []
  }
  for (const block of sorted) {
    if (block.start >= clusterEnd) flush()
    let column = columnEnds.findIndex((columnEnd) => columnEnd <= block.start)
    if (column < 0) column = columnEnds.push(0) - 1
    columnEnds[column] = blockVisualEnd(block)
    block.column = column
    cluster.push(block)
    clusterEnd = Math.max(clusterEnd, blockVisualEnd(block))
  }
  flush()
  return sorted
}

/**
 * Group occurrences into blocks per task, kind and visible day of `zone`.
 * Three or more runs closer than a chip's height become one series band; the
 * rest stay single chips.
 */
export function buildCalendarBlocks(
  occurrences: CalendarOccurrence[],
  firstDay: CalendarDay,
  dayCount: number,
  zone: string,
): CalendarBlock[][] {
  const buckets = new Map<
    string,
    { day: number; items: { minute: number; item: CalendarOccurrence }[] }
  >()
  for (const occurrence of occurrences) {
    const local = zonedTime(occurrence.at, zone)
    const day = Math.round((local.day.getTime() - firstDay.getTime()) / 86_400_000)
    if (day < 0 || day >= dayCount) continue
    const key = `${occurrence.taskId}:${occurrence.kind}:${day}`
    const bucket = buckets.get(key) ?? { day, items: [] }
    bucket.items.push({ minute: local.minute, item: occurrence })
    buckets.set(key, bucket)
  }
  const days: CalendarBlock[][] = Array.from({ length: dayCount }, () => [])
  for (const { day, items } of buckets.values()) {
    items.sort((a, b) => a.item.at.getTime() - b.item.at.getTime())
    const groups: (typeof items)[] = []
    for (const entry of items) {
      const group = groups[groups.length - 1]
      const previous = group?.[group.length - 1]
      if (group && previous && entry.minute - previous.minute <= SERIES_GAP_MINUTES)
        group.push(entry)
      else groups.push([entry])
    }
    for (const group of groups) {
      const parts = group.length >= SERIES_MIN_RUNS ? [group] : group.map((entry) => [entry])
      for (const part of parts) {
        const first = part[0]!
        const gaps = part.slice(1).map((entry, index) => entry.minute - part[index]!.minute)
        days[day]!.push({
          key: `${first.item.taskId}:${first.item.kind}:${first.item.at.getTime()}`,
          taskId: first.item.taskId,
          kind: first.item.kind,
          day,
          start: first.minute,
          end: part[part.length - 1]!.minute,
          step: gaps.length ? Math.max(Math.min(...gaps), 1) : 0,
          occurrences: part.map((entry) => entry.item),
          column: 0,
          columns: 1,
          inset: false,
        })
      }
    }
  }
  return days.map(layoutDayBlocks)
}

/** "Every 15 min" for evenly spaced series, otherwise the run count. */
export function describeSeries(block: Pick<CalendarBlock, 'occurrences'>): string {
  const times = block.occurrences.map((item) => item.at.getTime())
  if (times.length < 2) return ''
  const gaps = new Set(
    times.slice(1).map((time, index) => Math.round((time - times[index]!) / 60_000)),
  )
  const [gap] = [...gaps]
  if (gaps.size !== 1 || !gap) return `${times.length} runs`
  if (gap < 60) return `Every ${gap} min`
  return gap === 60 ? 'Every hour' : `Every ${gap / 60} h`
}

export function formatMinutes(minutes: number): string {
  return formatClock(Math.floor(minutes / 60) % 24, minutes % 60)
}

/** Weekday (0 = Monday) and HH:MM of an instant in an IANA zone. */
export function wallClock(date: Date, timezone: string): { weekday: number; time: string } {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: timezone,
      weekday: 'short',
      hour: 'numeric',
      minute: 'numeric',
      hourCycle: 'h23',
    })
      .formatToParts(date)
      .map((part) => [part.type, part.value]),
  )
  const weekday = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(parts.weekday ?? '')
  return { weekday, time: formatClock(Number(parts.hour), Number(parts.minute)) }
}

/** Only one-time and set-time schedules map a dragged run back to their fields. */
export function canReschedule(task: ScheduleFields): boolean {
  const { mode } = scheduleDraftFromTask(task)
  return mode === 'once' || mode === 'times'
}

/** Single planned slots of reschedulable tasks can be dragged. */
export function canMoveBlock(
  block: Pick<CalendarBlock, 'kind' | 'occurrences' | 'taskId'>,
  movableTaskIds: Set<string>,
): boolean {
  return block.kind === 'planned' && !isSeries(block) && movableTaskIds.has(block.taskId)
}

export function currentSchedulePatch(task: ScheduledTask): SchedulePatch {
  return {
    recurrence: task.recurrence,
    weekdays: [...task.weekdays],
    local_time: task.local_time,
    cron_expression: task.cron_expression ?? '',
    run_date: task.run_date ?? null,
  }
}

/**
 * Schedule fields after moving one occurrence from `from` to `to`. A one-time
 * run moves to the target date and time. On set-time schedules the run's time
 * changes for every selected day; on non-daily schedules the run's day is
 * swapped for the target day. Returns null when nothing would change.
 */
export function rescheduleTask(task: ScheduledTask, from: Date, to: Date): SchedulePatch | null {
  const draft = scheduleDraftFromTask(task)
  let moved: ScheduleDraft
  if (draft.mode === 'once') {
    const target = zonedTime(to, task.timezone_name)
    moved = { ...draft, date: isoDate(target.day), times: [formatMinutes(target.minute)] }
  } else if (draft.mode === 'times') {
    const before = wallClock(from, task.timezone_name)
    const after = wallClock(to, task.timezone_name)
    if (!draft.times.includes(before.time) || before.weekday < 0 || after.weekday < 0) return null
    const times = draft.times.map((time) => (time === before.time ? after.time : time))
    let weekdays = draft.weekdays
    if (weekdays.length < 7 && before.weekday !== after.weekday) {
      weekdays = [
        ...new Set([...weekdays.filter((day) => day !== before.weekday), after.weekday]),
      ].sort((a, b) => a - b)
    }
    moved = { ...draft, times, weekdays }
  } else return null
  const next = schedulePayload(moved)
  return JSON.stringify(next) === JSON.stringify(schedulePayload(draft)) ? null : next
}

/** Stable, distinct palette colors for the given workspaces (by name order). */
export function workspaceColors(workspaces: { id: string; name: string }[]): Map<string, string> {
  const sorted = [...workspaces].sort(
    (a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id),
  )
  return new Map(
    sorted.map((workspace, index) => [
      workspace.id,
      `var(--git-branch-${WORKSPACE_PALETTE[index % WORKSPACE_PALETTE.length]})`,
    ]),
  )
}
