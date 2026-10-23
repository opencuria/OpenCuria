import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent } from 'vue'
import { toast } from 'vue-sonner'
import ScheduledTasksView from './ScheduledTasksView.vue'
import ScheduleWeekGrid from '@/components/scheduled-tasks/ScheduleWeekGrid.vue'
import type { CalendarBlock } from '@/lib/scheduleCalendar'
import { useAuthStore } from '@/stores/auth'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import { useWorkspaceStore } from '@/stores/workspaces'
import type { ScheduledTask } from '@/services/scheduledTasks.api'
import type { Workspace } from '@/types'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  update: vi.fn(),
  runs: vi.fn(),
  runNow: vi.fn(),
}))
vi.mock('@/services/scheduledTasks.api', () => ({
  listScheduledTasks: api.list,
  createScheduledTask: vi.fn(),
  updateScheduledTask: api.update,
  deleteScheduledTask: vi.fn(),
  listScheduledTaskRuns: api.runs,
  runScheduledTaskNow: api.runNow,
}))
vi.mock('@/composables/usePolling', () => ({
  usePolling: (fetch: () => Promise<void>) => ({ start: () => void fetch(), stop: vi.fn() }),
}))
vi.mock('vue-sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}))

const LOCAL_ZONE = Intl.DateTimeFormat().resolvedOptions().timeZone

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
  weekdays: [2],
  cron_expression: '',
  local_time: '09:00',
  timezone_name: LOCAL_ZONE,
  enabled: true,
  next_run_at: '2026-06-17T09:00:00Z',
  created_at: '',
  updated_at: '',
  ...overrides,
})

const workspace = (id: string, name: string) => ({ id, name, status: 'running' }) as Workspace

async function mountView(url = '/scheduled-tasks') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/scheduled-tasks', component: ScheduledTasksView },
      { path: '/workspaces/:id', component: defineComponent({ template: '<div />' }) },
    ],
  })
  await router.push(url)
  await router.isReady()
  const wrapper = mount(ScheduledTasksView, {
    global: { plugins: [router] },
    attachTo: document.body,
  })
  await flushPromises()
  return { wrapper, router }
}

function grid(wrapper: Awaited<ReturnType<typeof mountView>>['wrapper']) {
  return wrapper.findComponent(ScheduleWeekGrid)
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(new Date(2026, 5, 16, 12)) // Tue Jun 16 2026, noon
  vi.clearAllMocks()
  setActivePinia(createPinia())
  useAuthStore().activeOrganizationId = 'org-1'
  useWorkspaceStore().workspaces = [workspace('ws-1', 'Alpha'), workspace('ws-2', 'Beta')]
  api.list.mockResolvedValue([task('a'), task('b', { workspace_id: 'ws-2', enabled: false })])
  api.runs.mockResolvedValue([])
  api.update.mockImplementation(async (id: string, patch: Partial<ScheduledTask>) => ({
    ...task(id),
    ...patch,
  }))
})

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('ScheduledTasksView', () => {
  it('lists tasks by workspace and draws their runs in the week', async () => {
    await useScheduledTaskStore().refresh()
    const { wrapper } = await mountView()
    const rail = wrapper.get('[data-testid="schedule-task-rail"]')
    expect(rail.text()).toContain('Alpha')
    expect(rail.text()).toContain('Paused')
    expect(api.runs).toHaveBeenCalledWith('a')
    const blocks = grid(wrapper).props('blocks') as CalendarBlock[][]
    expect(blocks[2]!.map((block) => block.taskId).sort()).toEqual(['a', 'b'])
  })

  it('renders the full week of hourly schedules when Friday history is unavailable', async () => {
    vi.setSystemTime(new Date(2026, 5, 19, 23))
    api.list.mockResolvedValue([
      task('test', { name: 'Test', recurrence: 'cron', cron_expression: '0 * * * 2' }),
      task('test2', { name: 'Test2', recurrence: 'cron', cron_expression: '0 9-17 * * *' }),
    ])
    api.runs.mockRejectedValue(new Error('History unavailable'))
    await useScheduledTaskStore().refresh()
    const { wrapper } = await mountView()
    for (let day = 0; day < 7; day++) {
      const column = wrapper.get(`[data-testid="schedule-day-${day}"]`)
      expect(column.findAll('[data-testid="schedule-block-test2"]')).toHaveLength(9)
      expect(column.findAll('[data-testid="schedule-block-test"]')).toHaveLength(day === 1 ? 24 : 0)
    }
    wrapper.unmount()
  })

  it('opens a prefilled new task from a calendar slot', async () => {
    await useScheduledTaskStore().refresh()
    const { wrapper } = await mountView()
    const store = useScheduledTaskStore()
    grid(wrapper).vm.$emit('create', new Date(Date.UTC(2026, 5, 20)), 14 * 60 + 30)
    expect(store.dialogOpen).toBe(true)
    expect(store.selectedTaskId).toBeNull()
    expect(store.newTaskPrefill).toEqual({
      workspace_id: undefined,
      weekdays: [5],
      times: ['14:30'],
      timezone_name: LOCAL_ZONE,
    })
  })

  it('pauses a task from the rail', async () => {
    await useScheduledTaskStore().refresh()
    const { wrapper } = await mountView()
    await wrapper.get('[data-testid="schedule-rail-toggle-a"]').trigger('click')
    await flushPromises()
    expect(api.update).toHaveBeenCalledWith('a', { enabled: false })
  })

  it('moves a run and offers undo', async () => {
    await useScheduledTaskStore().refresh()
    const { wrapper } = await mountView()
    const block = (grid(wrapper).props('blocks') as CalendarBlock[][])[2]!.find(
      (item) => item.taskId === 'a',
    )!
    grid(wrapper).vm.$emit('move', block, new Date(Date.UTC(2026, 5, 17)), 11 * 60 + 15)
    await flushPromises()
    expect(api.update).toHaveBeenCalledWith('a', {
      recurrence: 'weekly',
      weekdays: [2],
      local_time: '11:15',
      cron_expression: '',
    })
    const options = vi.mocked(toast.success).mock.calls[0]![1] as {
      action: { label: string; onClick: () => void }
    }
    expect(options.action.label).toBe('Undo')
    options.action.onClick()
    await flushPromises()
    expect(api.update).toHaveBeenLastCalledWith('a', {
      recurrence: 'weekly',
      weekdays: [2],
      local_time: '09:00',
      cron_expression: '',
    })
  })

  it('reports a failed move and restores the schedule', async () => {
    await useScheduledTaskStore().refresh()
    api.update.mockRejectedValueOnce(new Error('Server unavailable'))
    const { wrapper } = await mountView()
    const block = (grid(wrapper).props('blocks') as CalendarBlock[][])[2]!.find(
      (item) => item.taskId === 'a',
    )!
    grid(wrapper).vm.$emit('move', block, new Date(Date.UTC(2026, 5, 17)), 11 * 60 + 15)
    await flushPromises()
    expect(toast.error).toHaveBeenCalledWith(
      'Could not change schedule',
      expect.objectContaining({ description: 'Server unavailable' }),
    )
    expect(useScheduledTaskStore().tasks.find((item) => item.id === 'a')?.local_time).toBe('09:00')
    const moved = (grid(wrapper).props('blocks') as CalendarBlock[][])[2]!.find(
      (item) => item.taskId === 'a',
    )
    expect(moved?.start).toBe(9 * 60)
  })

  it('opens the settings of a legacy ?task= link and drops the query', async () => {
    const { wrapper, router } = await mountView('/scheduled-tasks?task=a')
    await flushPromises()
    const store = useScheduledTaskStore()
    expect(store.dialogOpen).toBe(true)
    expect(store.selectedTaskId).toBe('a')
    expect(router.currentRoute.value.query.task).toBeUndefined()
    wrapper.unmount()
  })
})
