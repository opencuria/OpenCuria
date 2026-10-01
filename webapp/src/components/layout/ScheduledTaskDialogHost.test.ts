import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ScheduledTaskDialogHost from './ScheduledTaskDialogHost.vue'
import { useAuthStore } from '@/stores/auth'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import type { ScheduledTask } from '@/services/scheduledTasks.api'

const api = vi.hoisted(() => ({ list: vi.fn() }))
vi.mock('@/services/scheduledTasks.api', () => ({
  listScheduledTasks: api.list,
  createScheduledTask: vi.fn(),
  updateScheduledTask: vi.fn(),
  deleteScheduledTask: vi.fn(),
}))
vi.mock('@/services/socket', () => ({ onReconnect: vi.fn(() => () => {}) }))
vi.mock('@/composables/usePolling', () => ({
  usePolling: () => ({ start: vi.fn(), stop: vi.fn() }),
}))

const task = (id: string, nextRun = '2026-10-05T10:00:00Z'): ScheduledTask => ({
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
  next_run_at: nextRun,
  created_at: nextRun,
  updated_at: nextRun,
})

const dialogStub = defineComponent({
  props: { open: Boolean },
  emits: ['update:open'],
  setup(props, { emit }) {
    return () =>
      props.open
        ? h('section', { 'data-testid': 'dialog' }, [
            h('button', { 'data-testid': 'dialog-close', onClick: () => emit('update:open', false) }),
          ])
        : null
  },
})
const page = defineComponent({ template: '<div />' })
let wrapper: VueWrapper | null = null

async function mountHost(url: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: page },
      { path: '/workspaces/:id', component: page },
    ],
  })
  await router.push(url)
  await router.isReady()
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.activeOrganizationId = 'org-1'
  const app = defineComponent({
    setup() {
      const store = useScheduledTaskStore()
      return () =>
        h('div', [
          h('button', { 'data-testid': 'task-opener', onClick: () => store.openTask('task-1') }, 'Open'),
          h(ScheduledTaskDialogHost),
        ])
    },
  })
  wrapper = mount(app, {
    attachTo: document.body,
    global: { plugins: [pinia, router], stubs: { ScheduledTaskDialog: dialogStub } },
  })
  await flushPromises()
  return { router, store: useScheduledTaskStore(), auth }
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
})

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.setItem('kern_active_org_id', 'org-1')
  api.list.mockResolvedValue([task('task-1'), task('task-2', '2026-10-06T10:00:00Z')])
})

describe('ScheduledTaskDialogHost', () => {
  it('opens legacy and deep-link queries and preserves unrelated query params', async () => {
    const { router, store } = await mountHost('/?session=chat-1&scheduledTask=task-1')

    await vi.waitFor(() => expect(store.selectedTaskId).toBe('task-1'))
    await vi.waitFor(() => expect(router.currentRoute.value.query.scheduledTask).toBeUndefined())
    expect(store.dialogOpen).toBe(true)
    expect(router.currentRoute.value.query.session).toBe('chat-1')
    expect(api.list).toHaveBeenCalledTimes(1)
  })

  it('resolves latest to the earliest scheduled task', async () => {
    const { router, store } = await mountHost('/?scheduledTask=latest&filter=active')

    await vi.waitFor(() => expect(store.selectedTaskId).toBe('task-1'))
    await vi.waitFor(() => expect(router.currentRoute.value.query.scheduledTask).toBeUndefined())
    expect(router.currentRoute.value.query.filter).toBe('active')
  })

  it('does not open from a query after the active organization changes during loading', async () => {
    let resolve!: (tasks: ScheduledTask[]) => void
    api.list.mockReturnValueOnce(new Promise((done) => (resolve = done)))
    const { auth, store } = await mountHost('/?scheduledTask=task-1')
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledTimes(1))

    auth.activeOrganizationId = 'org-2'
    resolve([task('task-1')])
    await flushPromises()

    expect(store.dialogOpen).toBe(false)
    expect(store.tasks).toEqual([])
  })

  it('does not open over a route change while a deep link is loading', async () => {
    let resolve!: (tasks: ScheduledTask[]) => void
    api.list.mockReturnValueOnce(new Promise((done) => (resolve = done)))
    const { router, store } = await mountHost('/?scheduledTask=task-1')
    await vi.waitFor(() => expect(api.list).toHaveBeenCalledTimes(1))

    await router.push('/workspaces/ws-1?session=chat-2')
    resolve([task('task-1')])
    await flushPromises()

    expect(store.dialogOpen).toBe(false)
    expect(router.currentRoute.value.fullPath).toBe('/workspaces/ws-1?session=chat-2')
  })

  it('retains the requested task on load failure instead of creating a new task', async () => {
    api.list.mockRejectedValueOnce(new Error('offline'))
    const { store } = await mountHost('/?scheduledTask=task-from-link')

    await vi.waitFor(() => expect(store.dialogOpen).toBe(true))
    expect(store.selectedTaskId).toBe('task-from-link')
    expect(store.tasks).toEqual([])
    expect(wrapper?.find('[data-testid="dialog"]').exists()).toBe(true)
    expect(api.list).toHaveBeenCalledTimes(1)
  })

  it('restores focus to the clicked opener after a clean close', async () => {
    const { store } = await mountHost('/')
    const opener = wrapper!.get('[data-testid="task-opener"]')
    ;(opener.element as HTMLButtonElement).focus()
    await opener.trigger('click')
    await flushPromises()
    expect(wrapper!.find('[data-testid="dialog"]').exists()).toBe(true)

    const raf = vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      callback(0)
      return 0
    })
    await wrapper!.get('[data-testid="dialog-close"]').trigger('click')
    await flushPromises()
    expect(store.dialogOpen).toBe(false)
    expect(document.activeElement).toBe(opener.element)
    expect(raf).toHaveBeenCalled()
    raf.mockRestore()
  })

  it('mounts the modal only while the shared dialog is open', async () => {
    const { store } = await mountHost('/')
    expect(wrapper!.find('[data-testid="dialog"]').exists()).toBe(false)
    store.openTask('task-1')
    await flushPromises()
    expect(wrapper!.find('[data-testid="dialog"]').exists()).toBe(true)
    store.closeDialog()
    await flushPromises()
    expect(wrapper!.find('[data-testid="dialog"]').exists()).toBe(false)
  })
})
