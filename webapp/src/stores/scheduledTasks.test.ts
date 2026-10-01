import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuthStore } from '@/stores/auth'
import { useScheduledTaskStore } from './scheduledTasks'
import type { ScheduledTask } from '@/services/scheduledTasks.api'

const api = vi.hoisted(() => ({
  list: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
}))
vi.mock('@/services/scheduledTasks.api', () => ({
  listScheduledTasks: api.list,
  createScheduledTask: api.create,
  updateScheduledTask: api.update,
  deleteScheduledTask: api.remove,
}))

const task = (id: string, when = '2026-10-05T10:00:00Z'): ScheduledTask => ({
  id,
  name: id,
  workspace_id: 'ws-1',
  prompt: 'test',
  mode: 'build',
  model: '',
  reasoning_effort: '',
  skill_ids: [],
  recurrence: 'daily',
  weekdays: [],
  local_time: '09:00',
  timezone_name: 'UTC',
  enabled: true,
  next_run_at: when,
  created_at: when,
  updated_at: when,
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.setItem('kern_active_org_id', 'org-1')
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.activeOrganizationId = 'org-1'
})

describe('scheduled task store', () => {
  it('deduplicates list requests and sorts tasks by next run', async () => {
    let resolve!: (tasks: ScheduledTask[]) => void
    api.list.mockReturnValue(
      new Promise((done) => {
        resolve = done
      }),
    )
    const store = useScheduledTaskStore()
    const first = store.refresh()
    const second = store.refresh()
    expect(api.list).toHaveBeenCalledTimes(1)
    resolve([task('later', '2026-10-08T00:00:00Z'), task('soon')])
    await Promise.all([first, second])
    expect(store.tasks.map((item) => item.id)).toEqual(['soon', 'later'])
  })

  it('shares a failed request with every caller instead of masking its error', async () => {
    let reject!: (error: Error) => void
    api.list.mockReturnValueOnce(new Promise((_, fail) => (reject = fail)))
    const store = useScheduledTaskStore()
    const first = store.refresh()
    const second = store.refresh()
    const results = Promise.allSettled([first, second])
    reject(new Error('network down'))
    expect((await results).map((result) => result.status)).toEqual(['rejected', 'rejected'])
    expect(api.list).toHaveBeenCalledTimes(1)
    expect(store.loadError).toBe('network down')
  })

  it('records list errors and allows retries', async () => {
    api.list.mockRejectedValueOnce(new Error('network down')).mockResolvedValueOnce([task('ok')])
    const store = useScheduledTaskStore()
    await expect(store.refresh()).rejects.toThrow('network down')
    expect(store.loadError).toBe('network down')
    await store.refresh(true)
    expect(store.tasks).toHaveLength(1)
    expect(store.loadError).toBe('')
  })

  it('updates its list through create/update/delete mutations', async () => {
    const store = useScheduledTaskStore()
    api.create.mockResolvedValue(task('created'))
    api.update.mockResolvedValue({ ...task('created'), name: 'updated' })
    api.remove.mockResolvedValue(undefined)
    await store.create({} as never)
    await store.update('created', { name: 'updated' })
    expect(store.tasks[0]?.name).toBe('updated')
    await store.remove('created')
    expect(store.tasks).toEqual([])
  })

  it('propagates mutation errors without making optimistic sidebar changes', async () => {
    const store = useScheduledTaskStore()
    store.tasks = [task('existing')]
    api.create.mockRejectedValueOnce(new Error('create denied'))
    api.update.mockRejectedValueOnce(new Error('update denied'))
    api.remove.mockRejectedValueOnce(new Error('delete denied'))
    await expect(store.create({} as never)).rejects.toThrow('create denied')
    await expect(store.update('existing', { name: 'changed' })).rejects.toThrow('update denied')
    await expect(store.remove('existing')).rejects.toThrow('delete denied')
    expect(store.tasks.map((item) => item.name)).toEqual(['existing'])
  })

  it('does not let a refresh started during an update roll the update back', async () => {
    let resolveList!: (tasks: ScheduledTask[]) => void
    let resolveUpdate!: (task: ScheduledTask) => void
    api.list.mockReturnValueOnce(new Promise((done) => (resolveList = done)))
    api.update.mockReturnValueOnce(new Promise((done) => (resolveUpdate = done)))
    const store = useScheduledTaskStore()
    store.tasks = [task('existing')]

    const update = store.update('existing', { name: 'updated' })
    const refresh = store.refresh()
    resolveUpdate({ ...task('existing'), name: 'updated' })
    await update
    resolveList([task('existing')])
    await refresh

    expect(store.tasks.map((item) => item.name)).toEqual(['updated'])
  })

  it('does not let a refresh started during a delete resurrect the task', async () => {
    let resolveList!: (tasks: ScheduledTask[]) => void
    let resolveDelete!: () => void
    api.list.mockReturnValueOnce(new Promise((done) => (resolveList = done)))
    api.remove.mockReturnValueOnce(new Promise<void>((done) => (resolveDelete = done)))
    const store = useScheduledTaskStore()
    store.tasks = [task('existing')]
    store.openTask('existing')

    const remove = store.remove('existing')
    const refresh = store.refresh()
    resolveDelete()
    await remove
    resolveList([task('existing')])
    await refresh

    expect(store.tasks).toEqual([])
    expect(store.selectedTaskId).toBeNull()
  })

  it('keeps a task created while a refresh is in flight', async () => {
    let resolve!: (tasks: ScheduledTask[]) => void
    api.list.mockReturnValue(
      new Promise((done) => {
        resolve = done
      }),
    )
    const store = useScheduledTaskStore()
    store.tasks = [task('existing')]
    const refresh = store.refresh()
    api.create.mockResolvedValue(task('created'))
    await store.create({} as never)
    resolve([task('existing')])
    await refresh
    expect(store.tasks.map((item) => item.id)).toEqual(['existing', 'created'])
  })

  it('preserves unknown selections until a list can hydrate them', async () => {
    const store = useScheduledTaskStore()
    store.openTask('deep-linked')
    api.list.mockResolvedValue([task('deep-linked')])
    await store.refresh()
    expect(store.selectedTaskId).toBe('deep-linked')
    expect(store.selectedTask?.id).toBe('deep-linked')
  })

  it('ignores responses belonging to an organization that is no longer active', async () => {
    let resolve!: (tasks: ScheduledTask[]) => void
    api.list.mockReturnValue(
      new Promise((done) => {
        resolve = done
      }),
    )
    const store = useScheduledTaskStore()
    const request = store.refresh()
    const auth = useAuthStore()
    auth.activeOrganizationId = 'org-2'
    await Promise.resolve()
    resolve([task('old-org')])
    await request
    expect(store.tasks).toEqual([])
  })
})
