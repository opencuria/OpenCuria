import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  addDays,
  buildCalendarBlocks,
  calendarDay,
  calendarOccurrences,
  canReschedule,
  describeSeries,
  formatWeekLabel,
  layoutDayBlocks,
  rescheduleTask,
  startOfWeek,
  taskCronExpression,
  wallClock,
  workspaceColors,
} from './scheduleCalendar'
import type { CalendarBlock, CalendarOccurrence } from './scheduleCalendar'
import type { ScheduledTask, ScheduledTaskRun } from '@/services/scheduledTasks.api'

// Instants use the local zone; calendar dates always use midnight UTC.
const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone
const local = (day: number, hour: number, minute = 0): Date => new Date(2026, 5, day, hour, minute)
const MONDAY = calendarDay(2026, 6, 15)
const RANGE_START = local(15, 0)
const RANGE_END = local(22, 0)

const task = (overrides: Partial<ScheduledTask> = {}): ScheduledTask => ({
  id: 'task-1',
  name: 'Review',
  workspace_id: 'ws-1',
  prompt: 'Check',
  mode: 'build',
  model: '',
  reasoning_effort: '',
  skill_ids: [],
  recurrence: 'weekly',
  weekdays: [0, 2],
  cron_expression: '',
  local_time: '09:00',
  timezone_name: LOCAL_ZONE,
  enabled: true,
  next_run_at: '2026-06-15T07:00:00Z',
  created_at: '',
  updated_at: '',
  ...overrides,
})

const run = (at: Date, status = 'succeeded'): ScheduledTaskRun => ({
  id: `run-${at.getTime()}`,
  scheduled_for: at.toISOString(),
  status,
  session_id: 'session-1',
  started_at: at.toISOString(),
  finished_at: null,
  error: '',
  reason: '',
  assistant_finish: '',
  assistant_error: '',
})

const occurrence = (at: Date, taskId = 'task-1'): CalendarOccurrence => ({
  taskId,
  at,
  kind: 'planned',
})

describe('schedule calendar', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(local(19, 23)) // Friday, after the week's weekday slots
  })

  afterEach(() => vi.useRealTimers())

  it('starts weeks on Monday and labels month ranges', () => {
    expect(startOfWeek(calendarDay(2026, 6, 18)).getTime()).toBe(MONDAY.getTime())
    expect(startOfWeek(calendarDay(2026, 6, 21)).getTime()).toBe(MONDAY.getTime())
    expect(formatWeekLabel(MONDAY, addDays(MONDAY, 6))).toMatch(/2026/)
    expect(formatWeekLabel(new Date(2026, 5, 29), new Date(2026, 6, 5))).toMatch(/–/)
  })

  it('derives cron expressions for every stored recurrence', () => {
    expect(taskCronExpression(task())).toBe('0 9 * * 1,3')
    expect(taskCronExpression(task({ recurrence: 'daily', weekdays: [] }))).toBe('0 9 * * *')
    expect(
      taskCronExpression(task({ recurrence: 'cron', cron_expression: '*/15 9-17 * * 1-5' })),
    ).toBe('*/15 9-17 * * 1-5')
  })

  it('overlays recorded runs on the full schedule without duplicating slots', () => {
    const summary = (runs: ScheduledTaskRun[]) =>
      calendarOccurrences([task()], { 'task-1': runs }, RANGE_START, RANGE_END).map((item) => [
        item.kind,
        item.at.getDate(),
        item.run?.status ?? null,
      ])
    // A partial history must not hide Wednesday's scheduled slot.
    expect(summary([run(local(15, 9), 'error'), run(local(8, 9))])).toEqual([
      ['run', 15, 'error'],
      ['planned', 17, null],
    ])
    // A run claimed for Wednesday replaces the planned slot.
    expect(summary([run(local(17, 9), 'claimed')])).toEqual([
      ['run', 17, 'claimed'],
      ['planned', 15, null],
    ])
  })

  it('shows hourly daily slots on every visible day, including Monday through Friday', () => {
    const items = calendarOccurrences(
      [task({ name: 'Test2', recurrence: 'cron', cron_expression: '0 9-17 * * *' })],
      {},
      RANGE_START,
      RANGE_END,
    )
    const days = buildCalendarBlocks(items, MONDAY, 7, LOCAL_ZONE)
    expect(days.map((blocks) => blocks.length)).toEqual([9, 9, 9, 9, 9, 9, 9])
    for (const blocks of days)
      expect(blocks.map((block) => block.start)).toEqual(
        Array.from({ length: 9 }, (_, index) => (9 + index) * 60),
      )
    expect(items.every((item) => item.kind === 'planned')).toBe(true)
  })

  it('shows Tuesday hourly tasks even after Tuesday has passed', () => {
    const items = calendarOccurrences(
      [task({ name: 'Test', recurrence: 'cron', cron_expression: '0 * * * 2' })],
      {},
      RANGE_START,
      RANGE_END,
    )
    expect(items).toHaveLength(24)
    expect(items.every((item) => item.at.getDate() === 16)).toBe(true)
    expect(items.map((item) => item.at.getHours())).toEqual(
      Array.from({ length: 24 }, (_, hour) => hour),
    )
  })

  it('shows schedules in wholly past weeks without depending on run history', () => {
    const items = calendarOccurrences([task()], {}, local(8, 0), RANGE_START)
    expect(items.map((item) => item.at.getDate())).toEqual([8, 10])
    expect(items.every((item) => item.kind === 'planned')).toBe(true)
  })

  it('keeps every minute of the full week instead of truncating dense schedules', () => {
    const items = calendarOccurrences(
      [task({ recurrence: 'cron', cron_expression: '* * * * *' })],
      {},
      RANGE_START,
      RANGE_END,
    )
    expect(items).toHaveLength(7 * 24 * 60)
    expect(items[0]!.at).toEqual(RANGE_START)
    expect(items[items.length - 1]!.at).toEqual(local(21, 23, 59))
    const days = buildCalendarBlocks(items, MONDAY, 7, LOCAL_ZONE)
    expect(days.map((blocks) => blocks.length)).toEqual([1, 1, 1, 1, 1, 1, 1])
    expect(days.map((blocks) => blocks[0]!.occurrences.length)).toEqual(Array(7).fill(1440))
  })

  it('preserves off-schedule runs and ignores invalid or out-of-range timestamps', () => {
    const manual = { ...run(local(16, 12)), trigger: 'manual' }
    const items = calendarOccurrences(
      [task({ recurrence: 'cron', cron_expression: 'invalid' })],
      { 'task-1': [manual, run(RANGE_END), run(local(8, 9)), { ...manual, scheduled_for: 'bad' }] },
      RANGE_START,
      RANGE_END,
    )
    expect(items).toEqual([{ taskId: 'task-1', at: local(16, 12), kind: 'run', run: manual }])
  })

  it('merges dense runs into series and keeps sparse runs as single chips', () => {
    const items = [
      ...[0, 30, 60, 90].map((minute) => occurrence(local(16, 8, minute), 'dense')),
      occurrence(local(16, 14), 'dense'),
      occurrence(local(16, 9), 'sparse'),
      occurrence(local(16, 11), 'sparse'),
      occurrence(local(30, 9), 'outside'),
    ]
    const days = buildCalendarBlocks(items, MONDAY, 7, LOCAL_ZONE)
    expect(days).toHaveLength(7)
    const tuesday = days[1]!
    const series = tuesday.find((block) => block.taskId === 'dense' && block.occurrences.length > 1)
    expect(series).toMatchObject({ start: 8 * 60, end: 9 * 60 + 30 })
    expect(describeSeries(series!)).toBe('Every 30 min')
    expect(tuesday.filter((block) => block.taskId === 'sparse')).toHaveLength(2)
    expect(
      tuesday.find((block) => block.taskId === 'sparse' && block.start === 9 * 60)?.inset,
    ).toBe(true)
    expect(tuesday.find((block) => block.start === 14 * 60)?.inset).toBe(false)
    expect(days.flat().some((block) => block.taskId === 'outside')).toBe(false)
  })

  it('places overlapping single runs side by side', () => {
    const block = (key: string, start: number): CalendarBlock => ({
      key,
      taskId: key,
      kind: 'planned',
      day: 0,
      start,
      end: start,
      step: 0,
      occurrences: [occurrence(local(15, 0), key)],
      column: 0,
      columns: 1,
      inset: false,
    })
    // Chips are drawn 30 minutes tall: 09:15 still overlaps both 09:00 chips,
    // 09:30 reuses the first column, 10:00 starts a new cluster.
    const laid = layoutDayBlocks([
      block('a', 540),
      block('b', 540),
      block('c', 555),
      block('d', 570),
      block('e', 600),
    ])
    const byKey = Object.fromEntries(laid.map((item) => [item.key, item]))
    expect(['a', 'b', 'c', 'd'].map((key) => byKey[key]!.column)).toEqual([0, 1, 2, 0])
    expect(['a', 'b', 'c', 'd'].map((key) => byKey[key]!.columns)).toEqual([3, 3, 3, 3])
    expect(byKey.e).toMatchObject({ column: 0, columns: 1 })
  })

  it('moves a set-time run to another time for every selected day', () => {
    const berlin = task({ timezone_name: 'Europe/Berlin', weekdays: [0, 1, 2, 3, 4] })
    // Mon Jun 15 09:00 Berlin (07:00 UTC) → 10:30 Berlin
    const patch = rescheduleTask(
      berlin,
      new Date(Date.UTC(2026, 5, 15, 7)),
      new Date(Date.UTC(2026, 5, 15, 8, 30)),
    )
    expect(patch).toEqual({
      recurrence: 'weekly',
      weekdays: [0, 1, 2, 3, 4],
      local_time: '10:30',
      cron_expression: '',
    })
  })

  it('swaps the dragged day on weekly schedules and ignores the day on daily ones', () => {
    const weekly = task({ timezone_name: 'UTC', weekdays: [0, 2] })
    expect(
      rescheduleTask(
        weekly,
        new Date(Date.UTC(2026, 5, 15, 9)),
        new Date(Date.UTC(2026, 5, 18, 9)),
      ),
    ).toMatchObject({ recurrence: 'weekly', weekdays: [2, 3], local_time: '09:00' })
    const daily = task({ timezone_name: 'UTC', recurrence: 'daily', weekdays: [] })
    expect(
      rescheduleTask(daily, new Date(Date.UTC(2026, 5, 15, 9)), new Date(Date.UTC(2026, 5, 18, 9))),
    ).toBeNull()
    expect(
      rescheduleTask(
        daily,
        new Date(Date.UTC(2026, 5, 15, 9)),
        new Date(Date.UTC(2026, 5, 18, 11)),
      ),
    ).toMatchObject({ recurrence: 'daily', local_time: '11:00' })
  })

  it('keeps multi-time schedules as cron and refuses interval schedules', () => {
    const twice = task({
      timezone_name: 'UTC',
      recurrence: 'cron',
      cron_expression: '30 8,17 * * 1-5',
    })
    expect(canReschedule(twice)).toBe(true)
    expect(
      rescheduleTask(
        twice,
        new Date(Date.UTC(2026, 5, 15, 17, 30)),
        new Date(Date.UTC(2026, 5, 15, 18)),
      ),
    ).toEqual({
      recurrence: 'cron',
      weekdays: [],
      local_time: '08:30',
      cron_expression: '30 8 * * 1-5; 0 18 * * 1-5',
    })
    const interval = task({ recurrence: 'cron', cron_expression: '*/15 * * * *' })
    expect(canReschedule(interval)).toBe(false)
    expect(rescheduleTask(interval, local(15, 9), local(15, 10))).toBeNull()
  })

  it('reads wall clocks in the task zone and assigns distinct workspace colors', () => {
    expect(wallClock(new Date(Date.UTC(2026, 5, 15, 22, 30)), 'Asia/Tokyo')).toEqual({
      weekday: 1,
      time: '07:30',
    })
    const colors = workspaceColors([
      { id: 'b', name: 'Beta' },
      { id: 'a', name: 'Alpha' },
    ])
    expect(colors.get('a')).toBe('var(--git-branch-1)')
    expect(colors.get('b')).not.toBe(colors.get('a'))
  })
})
