import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ScheduleWeekGrid from './ScheduleWeekGrid.vue'
import { addDays, buildCalendarBlocks } from '@/lib/scheduleCalendar'
import type { CalendarOccurrence } from '@/lib/scheduleCalendar'
import type { ScheduledTask, ScheduledTaskRun } from '@/services/scheduledTasks.api'

const HOUR_PX = 48
const COLUMN_PX = 100
const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone
const MONDAY = new Date(Date.UTC(2026, 5, 15))
const DAYS = Array.from({ length: 7 }, (_, index) => addDays(MONDAY, index))

const task = (id: string, overrides: Partial<ScheduledTask> = {}): ScheduledTask => ({
  id,
  name: `Task ${id}`,
  workspace_id: 'ws-1',
  prompt: 'Check',
  mode: 'build',
  model: '',
  reasoning_effort: '',
  skill_ids: [],
  recurrence: 'weekly',
  weekdays: [1],
  local_time: '09:00',
  timezone_name: 'UTC',
  enabled: true,
  next_run_at: '',
  created_at: '',
  updated_at: '',
  ...overrides,
})

const failedRun = (at: Date): ScheduledTaskRun => ({
  id: 'run-1',
  scheduled_for: at.toISOString(),
  status: 'error',
  session_id: 's-1',
  started_at: null,
  finished_at: null,
  error: 'boom',
  reason: '',
  assistant_finish: '',
  assistant_error: '',
})

function mountGrid(
  occurrences: CalendarOccurrence[],
  tasks: ScheduledTask[],
  movable: string[] = [],
) {
  return mount(ScheduleWeekGrid, {
    attachTo: document.body,
    props: {
      days: DAYS,
      blocks: buildCalendarBlocks(occurrences, MONDAY, 7, LOCAL_ZONE),
      tasks: new Map(tasks.map((item) => [item.id, item])),
      colors: new Map([['ws-1', 'var(--git-branch-1)']]),
      now: new Date(2026, 5, 16, 12),
      zone: LOCAL_ZONE,
      focusTaskId: null,
      movableTaskIds: new Set(movable),
    },
  })
}

/** jsdom lacks PointerEvent; a MouseEvent with pointer fields is enough here. */
async function pointer(element: Element, type: string, init: MouseEventInit = {}): Promise<void> {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, ...init })
  Object.defineProperty(event, 'pointerType', { value: 'mouse' })
  Object.defineProperty(event, 'pointerId', { value: 1 })
  element.dispatchEvent(event)
  await nextTick()
}

beforeEach(() => {
  // jsdom has no layout: every column starts at the top and is 100px wide.
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement,
  ) {
    const columns = this.dataset.testid?.startsWith('schedule-day-') ? 1 : 7
    return { top: 0, left: 0, width: COLUMN_PX * columns, height: 24 * HOUR_PX } as DOMRect
  })
  HTMLElement.prototype.setPointerCapture = vi.fn()
})

afterEach(() => {
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('ScheduleWeekGrid', () => {
  it('renders day headers, the now line and run status', () => {
    const at = new Date(2026, 5, 16, 9)
    const wrapper = mountGrid([{ taskId: 'a', at, kind: 'run', run: failedRun(at) }], [task('a')])
    expect(wrapper.findAll('[data-testid^="schedule-day-header-"]')).toHaveLength(7)
    expect(wrapper.findAll('[data-testid="schedule-now-line"]')).toHaveLength(1)
    const block = wrapper.get('[data-testid="schedule-block-a"]')
    expect(block.attributes('aria-label')).toBe('Task a · 09:00 · Failed')
    expect(block.find('.lucide-circle-x').exists()).toBe(true)
  })

  it('plans a new run at the clicked half-hour slot', async () => {
    const wrapper = mountGrid([], [])
    await wrapper
      .get('[data-testid="schedule-day-2"]')
      .trigger('click', { clientY: 10.6 * HOUR_PX })
    const [day, minute] = wrapper.emitted('create')![0] as [Date, number]
    expect(day.getTime()).toBe(Date.UTC(2026, 5, 17))
    expect(minute).toBe(10 * 60 + 30)
  })

  it('opens details on click without creating a task', async () => {
    const wrapper = mountGrid(
      [{ taskId: 'a', at: new Date(2026, 5, 17, 9), kind: 'planned' }],
      [task('a')],
    )
    await wrapper.get('[data-testid="schedule-block-a"]').trigger('click')
    expect(wrapper.emitted('select')).toHaveLength(1)
    expect(wrapper.emitted('create')).toBeUndefined()
  })

  it('drags a movable run to another day and snapped time', async () => {
    const wrapper = mountGrid(
      [{ taskId: 'a', at: new Date(2026, 5, 16, 9), kind: 'planned' }],
      [task('a')],
      ['a'],
    )
    const block = wrapper.get('[data-testid="schedule-block-a"]')
    await pointer(block.element, 'pointerdown', { clientX: 150, clientY: 9 * HOUR_PX })
    await pointer(block.element, 'pointermove', { clientX: 350, clientY: 10.6 * HOUR_PX })
    expect(wrapper.get('[data-testid="schedule-drag-ghost"]').text()).toContain('10:30')
    await pointer(block.element, 'pointerup')
    await block.trigger('click')
    const [, day, minute] = wrapper.emitted('move')![0] as [unknown, Date, number]
    expect(day.getTime()).toBe(Date.UTC(2026, 5, 18))
    expect(minute).toBe(10 * 60 + 30)
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('does not drag series, runs or tasks that cannot be moved', async () => {
    const wrapper = mountGrid(
      [{ taskId: 'a', at: new Date(2026, 5, 16, 9), kind: 'planned' }],
      [task('a', { recurrence: 'cron', cron_expression: '*/15 * * * *' })],
    )
    const block = wrapper.get('[data-testid="schedule-block-a"]')
    await pointer(block.element, 'pointerdown', { clientX: 150, clientY: 9 * HOUR_PX })
    await pointer(block.element, 'pointermove', { clientX: 350, clientY: 12 * HOUR_PX })
    await pointer(block.element, 'pointerup')
    expect(wrapper.find('[data-testid="schedule-drag-ghost"]').exists()).toBe(false)
    expect(wrapper.emitted('move')).toBeUndefined()
  })
})
