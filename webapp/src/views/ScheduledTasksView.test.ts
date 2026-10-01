import { defineComponent, h, inject, nextTick, provide } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import ScheduledTasksView from './ScheduledTasksView.vue'
import type { ScheduledTask, ScheduledTaskRun } from '@/services/scheduledTasks.api'

const providerMocks = vi.hoisted(() => ({ load: vi.fn() }))
const api = vi.hoisted(() => ({
  listScheduledTasks: vi.fn(), createScheduledTask: vi.fn(), updateScheduledTask: vi.fn(),
  deleteScheduledTask: vi.fn(), runScheduledTaskNow: vi.fn(), listScheduledTaskRuns: vi.fn(),
}))
const workspaceMocks = vi.hoisted(() => ({ workspaces: [] as Array<Record<string, unknown>>, fetchWorkspaces: vi.fn() }))
const skillMocks = vi.hoisted(() => ({ skills: [] as Array<Record<string, unknown>>, fetchSkills: vi.fn() }))
const fileMocks = vi.hoisted(() => ({ tree: [], findFiles: vi.fn(), fetchDirectory: vi.fn(), trackAndUpload: vi.fn(), failUpload: vi.fn() }))
const notificationMocks = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn() }))

vi.mock('@/services/scheduledTasks.api', () => api)
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => workspaceMocks }))
vi.mock('@/stores/skills', () => ({ useSkillStore: () => skillMocks }))
vi.mock('@/stores/fileExplorer', () => ({ useFileExplorerStore: () => fileMocks }))
vi.mock('@/stores/notifications', () => ({ useNotificationStore: () => notificationMocks }))
vi.mock('@/lib/providerCatalog', () => ({ loadProviderModelsCached: providerMocks.load }))
vi.mock('@/lib/recentModels', () => ({ useRecentModels: () => ({ entries: { value: [] } }), recentCatalogModels: () => [] }))
vi.mock('@/services/socket', () => ({ sendFilesUpload: vi.fn(), sendFilesList: vi.fn(), sendFilesFind: vi.fn(), onEvent: vi.fn(() => () => {}), subscribeToWorkspace: vi.fn(() => () => {}) }))

const task: ScheduledTask = {
  id: 'task-1', name: 'Weekly review', workspace_id: 'ws-1', prompt: 'Review open issues',
  mode: 'plan', model: '', reasoning_effort: '', skill_ids: [], recurrence: 'weekly',
  weekdays: [0, 1, 2, 3, 4], local_time: '09:30', timezone_name: 'Europe/Berlin',
  enabled: true, next_run_at: '2025-01-08T08:30:00Z', created_at: '2025-01-01T00:00:00Z', updated_at: '2025-01-01T00:00:00Z',
}
const run: ScheduledTaskRun = {
  id: 'run-1', scheduled_for: '2025-01-08T08:30:00Z', status: 'running', session_id: 'session-1',
  started_at: '2025-01-08T08:31:00Z', finished_at: null, error: '', reason: '', assistant_finish: '', assistant_error: '',
}
const editorStub = defineComponent({
  props: { modelValue: { type: String, default: '' }, ariaInvalid: Boolean, ariaLabel: String, ariaDescribedby: String },
  emits: ['update:modelValue'],
  setup(props, { emit, attrs }) {
    return () => h('textarea', {
      ...attrs, 'data-testid': 'task-prompt', value: props.modelValue,
      'aria-invalid': props.ariaInvalid,
      onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLTextAreaElement).value),
    })
  },
})
const dialogStub = defineComponent({
  props: { open: Boolean },
  setup(props, { slots }) { return () => props.open ? h('div', slots.default?.()) : null },
})
const passthrough = defineComponent({ setup(_, { slots }) { return () => h('div', slots.default?.()) } })
const dropdownItemStub = defineComponent({ setup(_, { slots, attrs }) { return () => h('button', { type: 'button', ...attrs }, slots.default?.()) } })
const selectUpdateKey = Symbol('select-update')

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/workspaces/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(ScheduledTasksView, {
    global: {
      plugins: [router],
      stubs: {
        ComposerRichEditor: editorStub,
        HarnessModelPicker: true,
        DropdownMenu: passthrough, DropdownMenuTrigger: passthrough,
        DropdownMenuContent: passthrough, DropdownMenuItem: dropdownItemStub,
        Dialog: dialogStub, DialogContent: passthrough, DialogDescription: passthrough,
        DialogFooter: passthrough, DialogHeader: passthrough, DialogTitle: passthrough,
        Select: defineComponent({
          props: { modelValue: String }, emits: ['update:modelValue'],
          setup(_, { slots, emit }) {
            provide(selectUpdateKey, (value: string) => emit('update:modelValue', value))
            return () => h('div', slots.default?.())
          },
        }),
        SelectTrigger: defineComponent({
          setup(_, { slots, attrs }) { return () => h('button', { type: 'button', ...attrs }, slots.default?.()) },
        }),
        SelectValue: defineComponent({ setup: () => () => h('span', 'Select a workspace…') }),
        SelectContent: passthrough,
        SelectItem: defineComponent({
          props: { value: String },
          setup(props, { slots, attrs }) {
            const update = inject<(value: string) => void>(selectUpdateKey)
            return () => h('button', { type: 'button', ...attrs, onClick: () => { if (props.value) update?.(props.value) } }, slots.default?.())
          },
        }),
      },
    },
  })
  await flushPromises()
  return { wrapper, router }
}

beforeEach(() => {
  vi.clearAllMocks()
  providerMocks.load.mockResolvedValue([])
  workspaceMocks.workspaces = [{ id: 'ws-1', name: 'Project Alpha', status: 'stopped', runner_online: true, active_operation: null }]
  skillMocks.skills = []
  fileMocks.tree = []
  api.listScheduledTasks.mockResolvedValue([task])
  api.listScheduledTaskRuns.mockResolvedValue([run])
  api.updateScheduledTask.mockImplementation(async (id: string, values: Partial<ScheduledTask>) => ({ ...task, ...values, id }))
  api.runScheduledTaskNow.mockResolvedValue(run)
  api.deleteScheduledTask.mockResolvedValue(undefined)
})
afterEach(() => undefined)

describe('ScheduledTasksView', () => {
  it('shows personal schedules, stopped-workspace behavior, and disables file tools without navigation side effects', async () => {
    const { wrapper, router } = await mountView()
    expect(wrapper.text()).toContain('Weekly review')
    expect(wrapper.text()).toContain('Stopped workspaces resume automatically')
    expect(wrapper.get('[data-testid="upload-files"]').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="files-disabled-hint"]').text()).toContain('File browse and upload need a running workspace')
    expect(wrapper.get('[data-testid="task-prompt"]').element).toBeTruthy()
    expect(api.createScheduledTask).not.toHaveBeenCalled()
    expect(api.runScheduledTaskNow).not.toHaveBeenCalled()
    expect(router.currentRoute.value.fullPath).toBe('/')
  })

  it('pauses, runs without navigating, links to the created chat, and deletes schedules', async () => {
    const { wrapper, router } = await mountView()
    await wrapper.get('[data-testid="pause-task"]').trigger('click')
    await flushPromises()
    expect(api.updateScheduledTask).toHaveBeenCalledWith('task-1', { enabled: false })
    await wrapper.get('[data-testid="resume-task"]').trigger('click')
    await flushPromises()
    expect(api.updateScheduledTask).toHaveBeenCalledWith('task-1', { enabled: true })
    await wrapper.get('[data-testid="run-now"]').trigger('click')
    await flushPromises()
    expect(api.runScheduledTaskNow).toHaveBeenCalledWith('task-1')
    expect(router.currentRoute.value.fullPath).toBe('/')
    expect(wrapper.get('[data-testid="open-run-run-1"]').text()).toContain('Open chat')
    await wrapper.get('[data-testid="open-run-run-1"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/workspaces/ws-1?session=session-1')

    await wrapper.get('[data-testid="delete-task"]').trigger('click')
    await nextTick()
    await wrapper.get('[data-testid="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(api.deleteScheduledTask).toHaveBeenCalledWith('task-1')
    expect(wrapper.text()).toContain('Nothing on the calendar yet')
  })

  it('displays prompt validation errors beside the editor', async () => {
    const { wrapper } = await mountView()
    await wrapper.get('[data-testid="task-prompt"]').setValue('')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    expect(wrapper.get('[data-testid="task-prompt-error"]').text()).toContain('Add a prompt')
    expect(wrapper.find('[data-testid="task-prompt"]').attributes('aria-invalid')).toBe('true')
  })

  it('protects dirty edits when switching tasks or starting a new draft', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const { wrapper } = await mountView()
    await wrapper.get('[data-testid="task-name"]').setValue('Unsaved title')
    await wrapper.get('[data-testid="create-task"]').trigger('click')
    expect(confirm).toHaveBeenCalledWith('Discard your unsaved schedule changes?')
    expect(wrapper.find('[data-testid="task-name"]').element).toHaveProperty('value', 'Unsaved title')
    expect(wrapper.find('[data-testid="task-prompt-error"]').exists()).toBe(false)
  })

  it('creates weekly schedules with chosen weekdays and no throwaway chat/session calls', async () => {
    api.listScheduledTasks.mockResolvedValueOnce([])
    const created = { ...task, id: 'task-2', name: 'Morning check' }
    api.createScheduledTask.mockResolvedValue(created)
    const { wrapper } = await mountView()
    await wrapper.get('[data-testid="create-task"]').trigger('click')
    await wrapper.get('[data-testid="task-name"]').setValue('Morning check')
    await wrapper.get('[data-testid="task-prompt"]').setValue('Check build health')
    await nextTick()
    await wrapper.get('[data-testid="task-timezone"]').setValue('Europe/Berlin')
    await wrapper.get('[data-testid="recurrence-weekly"]').trigger('click')
    await wrapper.get('[data-testid="weekday-0"]').trigger('click')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.createScheduledTask).toHaveBeenCalledWith(expect.objectContaining({
      name: 'Morning check', workspace_id: 'ws-1', prompt: 'Check build health',
      recurrence: 'weekly', weekdays: [1, 2, 3, 4], timezone_name: 'Europe/Berlin',
    }))
    expect(api.createScheduledTask).toHaveBeenCalledTimes(1)
    expect(api.runScheduledTaskNow).not.toHaveBeenCalled()
  })
})
