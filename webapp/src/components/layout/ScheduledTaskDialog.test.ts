import { createPinia, setActivePinia } from 'pinia'
import { afterEach } from 'vitest'
import { defineComponent, h, nextTick, provide, inject } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ScheduledTaskDialog from './ScheduledTaskDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { useScheduledTaskStore } from '@/stores/scheduledTasks'
import type { ScheduledTask, ScheduledTaskRun } from '@/services/scheduledTasks.api'

const providerMocks = vi.hoisted(() => ({ load: vi.fn() }))
const api = vi.hoisted(() => ({
  listScheduledTasks: vi.fn(),
  createScheduledTask: vi.fn(),
  updateScheduledTask: vi.fn(),
  deleteScheduledTask: vi.fn(),
  runScheduledTaskNow: vi.fn(),
  listScheduledTaskRuns: vi.fn(),
}))
const workspaceMocks = vi.hoisted(() => ({
  workspaces: [] as Array<Record<string, unknown>>,
  fetchWorkspaces: vi.fn(),
}))
const skillMocks = vi.hoisted(() => ({
  skills: [] as Array<Record<string, unknown>>,
  fetchSkills: vi.fn(),
}))
const notificationMocks = vi.hoisted(() => ({ success: vi.fn(), info: vi.fn(), error: vi.fn() }))
vi.mock('@/services/scheduledTasks.api', () => api)
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => workspaceMocks }))
vi.mock('@/stores/skills', () => ({ useSkillStore: () => skillMocks }))
vi.mock('@/stores/notifications', () => ({ useNotificationStore: () => notificationMocks }))
vi.mock('@/lib/providerCatalog', () => ({ loadProviderModelsCached: providerMocks.load }))
vi.mock('@/lib/recentModels', () => ({
  useRecentModels: () => ({ entries: { value: [] } }),
  recentCatalogModels: () => [],
}))
vi.mock('@/services/socket', () => ({
  sendFilesUpload: vi.fn(),
  sendFilesList: vi.fn(),
  sendFilesFind: vi.fn(),
  onEvent: vi.fn(() => () => {}),
  subscribeToWorkspace: vi.fn(() => () => {}),
}))

const task: ScheduledTask = {
  id: 'task-1',
  name: 'Weekly review',
  workspace_id: 'ws-1',
  prompt: 'Review open issues',
  mode: 'plan',
  harness_id: 'native',
  model: '',
  reasoning_effort: '',
  skill_ids: [],
  recurrence: 'weekly',
  weekdays: [0, 1, 2, 3, 4],
  local_time: '09:30',
  timezone_name: 'Europe/Berlin',
  enabled: true,
  next_run_at: '2026-10-08T07:30:00Z',
  created_at: '2026-10-01T00:00:00Z',
  updated_at: '2026-10-01T00:00:00Z',
}
const run: ScheduledTaskRun = {
  id: 'run-1',
  scheduled_for: '2026-10-08T07:30:00Z',
  status: 'running',
  session_id: 'session-1',
  started_at: '2026-10-08T07:31:00Z',
  finished_at: null,
  error: '',
  reason: '',
  assistant_finish: '',
  assistant_error: '',
}
const editorStub = defineComponent({
  props: {
    modelValue: { type: String, default: '' },
    ariaInvalid: Boolean,
    ariaLabel: String,
    ariaDescribedby: String,
  },
  emits: ['update:modelValue'],
  setup(props, { emit, attrs }) {
    return () =>
      h('textarea', {
        ...attrs,
        'data-testid': 'task-prompt',
        value: props.modelValue,
        onInput: (event: Event) =>
          emit('update:modelValue', (event.target as HTMLTextAreaElement).value),
      })
  },
})
const passthrough = defineComponent({
  setup(_, { slots }) {
    return () => h('div', slots.default?.())
  },
})
const selectUpdateKey = Symbol('select-update')
const tabsModelKey = Symbol('tabs-model')

async function mountDialog(mode: 'edit' | 'new' = 'edit', selectedTask = task) {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.activeOrganizationId = 'org-1'
  workspaceMocks.workspaces = [
    {
      id: 'ws-1',
      name: 'Project Alpha',
      status: 'stopped',
      runner_online: true,
      active_operation: null,
    },
  ]
  skillMocks.skills = [{ id: 'skill-1', name: 'Review skill' }]
  const store = useScheduledTaskStore()
  store.tasks = [selectedTask]
  if (mode === 'new') store.openNew()
  else store.openTask(selectedTask.id)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/workspaces/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(ScheduledTaskDialog, {
    props: { open: true },
    global: {
      plugins: [router],
      stubs: {
        ComposerRichEditor: editorStub,
        ModelPicker: true,
        HarnessChatInput: defineComponent({
          name: 'HarnessChatInput',
          props: {
            prompt: { type: String, default: '' },
            harnessId: { type: String, default: 'native' },
            mode: { type: String, default: 'build' },
            model: { type: String, default: '' },
            effort: { type: String, default: '' },
            promptError: { type: String, default: '' },
            skillIds: { type: Array, default: () => [] },
            disabled: Boolean,
          },
          emits: [
            'update:prompt',
            'update:skillIds',
            'update:model',
            'update:effort',
            'update:mode',
            'update:harnessId',
          ],
          setup(props, { emit }) {
            return () =>
              h('div', [
                h('textarea', {
                  'data-testid': 'task-prompt',
                  value: props.prompt,
                  disabled: props.disabled,
                  onInput: (event: Event) =>
                    emit('update:prompt', (event.target as HTMLTextAreaElement).value),
                }),
                props.promptError
                  ? h('p', { 'data-testid': 'task-prompt-error' }, props.promptError)
                  : null,
              ])
          },
        }),
        DropdownMenu: passthrough,
        DropdownMenuTrigger: passthrough,
        DropdownMenuContent: passthrough,
        DropdownMenuItem: defineComponent({
          emits: ['select'],
          setup(_, { slots, attrs, emit }) {
            return () =>
              h(
                'button',
                {
                  type: 'button',
                  ...attrs,
                  onClick: (event: MouseEvent) => {
                    event.preventDefault()
                    emit('select', event)
                  },
                },
                slots.default?.(),
              )
          },
        }),
        Dialog: defineComponent({
          props: { open: Boolean },
          emits: ['update:open'],
          setup(props, { slots, emit }) {
            return () =>
              props.open
                ? h('div', [
                    h('button', {
                      type: 'button',
                      'data-testid': 'simulate-dialog-outside-click',
                      onClick: () => emit('update:open', false),
                    }),
                    slots.default?.(),
                  ])
                : null
          },
        }),
        DialogContent: passthrough,
        DialogDescription: passthrough,
        DialogFooter: passthrough,
        DialogHeader: passthrough,
        DialogTitle: passthrough,
        Tabs: defineComponent({
          props: { modelValue: String },
          emits: ['update:modelValue'],
          setup(props, { slots, emit }) {
            provide(tabsModelKey, { props, emit })
            return () => h('div', slots.default?.())
          },
        }),
        TabsContent: defineComponent({
          props: { value: String },
          setup(props, { slots, attrs }) {
            const tabs = inject<{
              props: { modelValue?: string }
              emit: (event: string, value: string) => void
            }>(tabsModelKey)
            return () =>
              props.value === tabs?.props.modelValue
                ? h('div', { ...attrs, 'data-tab-content': props.value }, slots.default?.())
                : null
          },
        }),
        TabsList: passthrough,
        TabsTrigger: defineComponent({
          props: { value: String },
          setup(props, { slots, attrs }) {
            const tabs = inject<{
              props: { modelValue?: string }
              emit: (event: string, value: string) => void
            }>(tabsModelKey)
            return () =>
              h(
                'button',
                {
                  type: 'button',
                  ...attrs,
                  onClick: () => {
                    if (props.value) tabs?.emit('update:modelValue', props.value)
                  },
                },
                slots.default?.(),
              )
          },
        }),
        Select: defineComponent({
          props: { modelValue: String, disabled: Boolean },
          emits: ['update:modelValue'],
          setup(props, { slots, emit }) {
            provide(selectUpdateKey, (value: string) => emit('update:modelValue', value))
            return () =>
              h('div', { 'aria-disabled': props.disabled ? 'true' : undefined }, slots.default?.())
          },
        }),
        SelectTrigger: defineComponent({
          setup(_, { slots, attrs }) {
            return () => h('button', { type: 'button', ...attrs }, slots.default?.())
          },
        }),
        SelectValue: defineComponent({ setup: () => () => h('span', 'Select a workspace…') }),
        SelectContent: passthrough,
        SelectItem: defineComponent({
          props: { value: String },
          setup(props, { slots, attrs }) {
            const update = inject<(value: string) => void>(selectUpdateKey)
            return () =>
              h(
                'button',
                { type: 'button', ...attrs, onClick: () => props.value && update?.(props.value) },
                slots.default?.(),
              )
          },
        }),
      },
    },
  })
  await flushPromises()
  return { wrapper, router, store }
}

beforeEach(() => {
  vi.clearAllMocks()
  providerMocks.load.mockResolvedValue([])
  api.listScheduledTasks.mockResolvedValue([task])
  api.listScheduledTaskRuns.mockResolvedValue([run])
  api.createScheduledTask.mockImplementation(async (values: Partial<ScheduledTask>) => ({
    ...task,
    ...values,
    id: 'task-new',
  }))
  api.updateScheduledTask.mockImplementation(
    async (_id: string, values: Partial<ScheduledTask>) => ({ ...task, ...values }),
  )
  api.runScheduledTaskNow.mockResolvedValue(run)
  api.deleteScheduledTask.mockResolvedValue(undefined)
})

describe('ScheduledTaskDialog', () => {
  afterEach(() => {
    document.body
      .querySelectorAll('[data-testid="composer-suggestions-portal"]')
      .forEach((node) => node.remove())
  })
  it('shows concise settings, stopped-workspace hint and delayed file controls without running or saving', async () => {
    const { wrapper } = await mountDialog()
    expect(wrapper.get('[data-testid="task-name"]').element).toHaveProperty(
      'value',
      'Weekly review',
    )
    expect(wrapper.get('[data-testid="task-prompt"]').element).toHaveProperty(
      'value',
      'Review open issues',
    )
    expect(wrapper.text()).toContain('Stopped workspace resumes automatically')
    expect(wrapper.find('[data-testid="workspace-files"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="save-task"]').attributes('disabled')).toBeDefined()
    expect(api.listScheduledTaskRuns).not.toHaveBeenCalled()
    expect(api.createScheduledTask).not.toHaveBeenCalled()
    expect(api.runScheduledTaskNow).not.toHaveBeenCalled()
  })

  it('validates the prompt without separate skill or file panels', async () => {
    const { wrapper } = await mountDialog('new')
    await wrapper.get('[data-testid="task-prompt"]').setValue('draft')
    await wrapper.get('[data-testid="task-prompt"]').setValue('')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    expect(wrapper.get('[data-testid="save-task"]').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="skills-picker"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="toggle-files"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="workspace-files"]').exists()).toBe(false)
  })

  it('defaults to seven days and saves daily with empty API weekdays', async () => {
    const { wrapper } = await mountDialog('new')
    expect(wrapper.find('[data-testid="recurrence-daily"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="recurrence-weekly"]').exists()).toBe(false)
    for (let day = 0; day < 7; day += 1) {
      expect(wrapper.get(`[data-testid="weekday-${day}"]`).attributes('aria-pressed')).toBe('true')
    }
    await wrapper.get('[data-testid="task-name"]').setValue('Daily review')
    await wrapper.get('[data-testid="task-prompt"]').setValue('Review issues')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.createScheduledTask).toHaveBeenCalledWith(
      expect.objectContaining({
        recurrence: 'daily',
        weekdays: [],
        harness_id: 'native',
        skill_ids: [],
      }),
    )
  })

  it('restores Claude task mode and selected skills when editing', async () => {
    const claudeTask: ScheduledTask = {
      ...task,
      harness_id: 'claude',
      model: 'opus',
      reasoning_effort: 'low',
      skill_ids: ['skill-1'],
    }
    const { wrapper } = await mountDialog('edit', claudeTask)
    const composer = wrapper.findComponent({ name: 'HarnessChatInput' })
    expect(composer.props()).toMatchObject({
      harnessId: 'claude',
      mode: 'plan',
      model: 'opus',
      effort: 'low',
      skillIds: ['skill-1'],
    })

    await wrapper.get('[data-testid="task-name"]').setValue('Claude review')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.updateScheduledTask).toHaveBeenCalledWith(
      claudeTask.id,
      expect.objectContaining({
        harness_id: 'claude',
        model: 'opus',
        reasoning_effort: 'low',
        skill_ids: ['skill-1'],
      }),
    )
  })

  it('loads daily tasks with seven days and derives weekly after deselecting one', async () => {
    const { wrapper } = await mountDialog('edit', { ...task, recurrence: 'daily', weekdays: [] })
    for (let day = 0; day < 7; day += 1) {
      expect(wrapper.get(`[data-testid="weekday-${day}"]`).attributes('aria-pressed')).toBe('true')
    }
    expect(wrapper.get('[data-testid="save-task"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="weekday-6"]').trigger('click')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.updateScheduledTask).toHaveBeenCalledWith(
      task.id,
      expect.objectContaining({ recurrence: 'weekly', weekdays: [0, 1, 2, 3, 4, 5] }),
    )
  })

  it('retains weekly days and derives daily when all days are selected', async () => {
    const { wrapper } = await mountDialog('edit', { ...task, weekdays: [1, 5] })
    for (let day = 0; day < 7; day += 1) {
      expect(wrapper.get(`[data-testid="weekday-${day}"]`).attributes('aria-pressed')).toBe(
        [1, 5].includes(day) ? 'true' : 'false',
      )
    }
    await wrapper.get('[data-testid="task-name"]').setValue('Updated review')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.updateScheduledTask).toHaveBeenLastCalledWith(
      task.id,
      expect.objectContaining({ recurrence: 'weekly', weekdays: [1, 5] }),
    )
    for (const day of [0, 2, 3, 4, 6]) {
      await wrapper.get(`[data-testid="weekday-${day}"]`).trigger('click')
    }
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.updateScheduledTask).toHaveBeenLastCalledWith(
      task.id,
      expect.objectContaining({ recurrence: 'daily', weekdays: [] }),
    )
  })

  it('previews only selected days and rejects an empty selection', async () => {
    const { wrapper } = await mountDialog('new')
    await wrapper.get('[data-testid="task-name"]').setValue('Monday review')
    await wrapper.get('[data-testid="task-prompt"]').setValue('Review issues')
    await wrapper.get('[data-testid="task-timezone"]').setValue('UTC')
    for (let day = 1; day < 7; day += 1) {
      await wrapper.get(`[data-testid="weekday-${day}"]`).trigger('click')
    }
    expect(wrapper.text()).toMatch(/Next run · Mon/)
    await wrapper.get('[data-testid="weekday-0"]').trigger('click')
    expect(wrapper.text()).toContain('Next run · Choose a valid time')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    expect(wrapper.text()).toContain('Choose at least one day.')
    expect(api.createScheduledTask).not.toHaveBeenCalled()
  })

  it('saves a weekly task through shared store, but does not run it', async () => {
    const { wrapper, store } = await mountDialog('new')
    await wrapper.get('[data-testid="task-name"]').setValue('Morning check')
    await wrapper.get('[data-testid="task-prompt"]').setValue('Check build health')
    await wrapper.get('[data-testid="task-timezone"]').setValue('Europe/Berlin')
    await wrapper.get('[data-testid="weekdays-weekdays"]').trigger('click')
    await wrapper.get('[data-testid="weekday-0"]').trigger('click')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await flushPromises()
    expect(api.createScheduledTask).toHaveBeenCalledWith(
      expect.objectContaining({
        name: 'Morning check',
        workspace_id: 'ws-1',
        prompt: 'Check build health',
        skill_ids: [],
        mode: 'build',
        harness_id: 'native',
        model: '',
        reasoning_effort: '',
        recurrence: 'weekly',
        weekdays: [1, 2, 3, 4],
        timezone_name: 'Europe/Berlin',
      }),
    )
    expect(store.tasks.some((item) => item.id === 'task-new')).toBe(true)
    expect(api.runScheduledTaskNow).not.toHaveBeenCalled()
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(wrapper.find('[data-testid="composer-send"]').exists()).toBe(false)
  })

  it('closes clean edits from both Cancel and the close button without passing DOM events', async () => {
    const { wrapper } = await mountDialog()
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)

    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    await wrapper.get('[data-testid="close-task-dialog"]').trigger('click')

    expect(wrapper.emitted('update:open')).toEqual([[false], [false]])
    expect(consoleError).not.toHaveBeenCalled()
    consoleError.mockRestore()
  })

  it('guards the close button and Escape with the discard confirmation', async () => {
    const { wrapper } = await mountDialog()
    await wrapper.get('[data-testid="task-name"]').setValue('Unsaved title')

    await wrapper.get('[data-testid="close-task-dialog"]').trigger('click')
    expect(wrapper.text()).toContain('Discard unsaved changes?')
    await wrapper.get('[data-testid="keep-editing"]').trigger('click')
    await wrapper.get('[data-testid="task-settings-tab"]').trigger('keydown.esc')
    expect(wrapper.text()).toContain('Discard unsaved changes?')
    await wrapper.get('[data-testid="keep-editing"]').trigger('click')
    await wrapper.get('[data-testid="simulate-dialog-outside-click"]').trigger('click')
    expect(wrapper.text()).toContain('Discard unsaved changes?')
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })

  it('blocks editing and close while a save is pending', async () => {
    let resolveCreate!: (saved: ScheduledTask) => void
    api.createScheduledTask.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveCreate = resolve
      }),
    )
    const { wrapper } = await mountDialog('new')
    await wrapper.get('[data-testid="task-name"]').setValue('New task')
    await wrapper.get('[data-testid="task-prompt"]').setValue('A valid scheduled task prompt')
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    await nextTick()
    expect(api.createScheduledTask).toHaveBeenCalledTimes(1)
    const settings = wrapper.find('[aria-busy="true"]')
    expect(settings.exists()).toBe(true)
    expect(settings.attributes('inert')).toBeDefined()
    expect(wrapper.find('[aria-disabled="true"]').exists()).toBe(true)
    expect(wrapper.get('[data-testid="task-prompt"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    await wrapper.get('[data-testid="close-task-dialog"]').trigger('click')
    expect(wrapper.emitted('update:open')).toBeUndefined()
    expect(wrapper.text()).not.toContain('Discard unsaved changes?')

    resolveCreate({ ...task, name: 'New task' })
    await flushPromises()
    expect(wrapper.emitted('update:open')).toBeUndefined()
  })

  it('shows run-history errors, retries, and opens the run chat after discarding edits', async () => {
    api.listScheduledTaskRuns.mockRejectedValueOnce(new Error('Run history unavailable'))
    const { wrapper, router } = await mountDialog()
    await wrapper.get('[data-testid="task-runs-tab"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="task-runs-error"]').text()).toContain(
      'Run history unavailable',
    )

    await wrapper.get('[data-testid="task-runs-error"] button').trigger('click')
    await flushPromises()
    expect(api.listScheduledTaskRuns).toHaveBeenCalledTimes(2)
    await wrapper.get('[data-testid="task-settings-tab"]').trigger('click')
    await wrapper.get('[data-testid="task-name"]').setValue('Unsaved title')
    await wrapper.get('[data-testid="task-runs-tab"]').trigger('click')
    await flushPromises()
    await wrapper.get('[data-testid="open-run-run-1"]').trigger('click')
    expect(wrapper.text()).toContain('Discard unsaved changes?')
    await wrapper.get('[data-testid="discard-changes"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/workspaces/ws-1?session=session-1')
  })

  it('loads history only when Runs is selected and offers a chat link', async () => {
    const { wrapper, router } = await mountDialog()
    expect(api.listScheduledTaskRuns).not.toHaveBeenCalled()
    await wrapper.get('[data-testid="task-runs-tab"]').trigger('click')
    await flushPromises()
    expect(api.listScheduledTaskRuns).toHaveBeenCalledWith('task-1')
    expect(wrapper.text()).not.toContain('Back to settings')
    expect(wrapper.get('[data-testid="task-settings-tab"]').text()).toBe('Settings')
    const panel = wrapper.get('[data-testid="task-runs-panel"]')
    expect(panel.findAll('button').map((button) => button.text())).toEqual(['Open chat', 'Run now'])
    expect(panel.findAll('button')[1]?.element.parentElement?.className).toContain('!justify-end')
    await wrapper.get('[data-testid="open-run-run-1"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/workspaces/ws-1?session=session-1')
  })

  it('hydrates a task that appears after the editor opened from a deep link', async () => {
    const { wrapper, store } = await mountDialog()
    store.tasks = []
    store.openTask(task.id)
    await nextTick()
    expect(wrapper.find('[data-testid="task-name"]').exists()).toBe(false)
    store.tasks = [task]
    await nextTick()
    expect(wrapper.get('[data-testid="task-name"]').element).toHaveProperty('value', task.name)
    expect(wrapper.get('[data-testid="save-task"]').attributes('disabled')).toBeDefined()
  })

  it('shows delete failures inside the confirmation instead of behind it', async () => {
    const { wrapper } = await mountDialog()
    api.deleteScheduledTask.mockRejectedValueOnce(new Error('Delete denied'))
    await wrapper.get('[data-testid="task-actions"]').trigger('click')
    await wrapper.get('[data-testid="delete-task"]').trigger('click')
    await wrapper.get('[data-testid="confirm-delete"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="delete-task-error"]').text()).toContain('Delete denied')
    expect(wrapper.find('[data-testid="task-api-error"]').exists()).toBe(false)
  })

  it('refuses to save when the selected task is unavailable', async () => {
    const { wrapper, store } = await mountDialog()
    store.tasks = []
    store.openTask('missing')
    await flushPromises()
    expect(wrapper.get('[data-testid="task-unavailable"]').text()).toContain(
      'This task is no longer available.',
    )
    await wrapper.get('[data-testid="save-task"]').trigger('click')
    expect(api.updateScheduledTask).not.toHaveBeenCalled()
    expect(api.createScheduledTask).not.toHaveBeenCalled()
  })

  it('disables Run now for unsaved task drafts', async () => {
    const { wrapper } = await mountDialog()
    await wrapper.get('[data-testid="task-name"]').setValue('Changed name')
    expect(wrapper.get('[data-testid="run-now"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="task-enabled"]').trigger('click')
    expect(api.updateScheduledTask).not.toHaveBeenCalled()
  })

  it('guards cancel and close while edits are dirty, then allows discard', async () => {
    const { wrapper } = await mountDialog()
    await wrapper.get('[data-testid="task-name"]').setValue('Unsaved title')
    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    expect(wrapper.text()).toContain('Discard unsaved changes?')
    await wrapper.get('[data-testid="keep-editing"]').trigger('click')
    expect(wrapper.get('[data-testid="task-name"]').element).toHaveProperty(
      'value',
      'Unsaved title',
    )
    await wrapper.get('[data-testid="cancel-task"]').trigger('click')
    expect(wrapper.text()).toContain('Discard unsaved changes?')
    await wrapper.get('[data-testid="discard-changes"]').trigger('click')
    expect(wrapper.emitted('update:open')?.slice(-1)[0]).toEqual([false])
  })
})
