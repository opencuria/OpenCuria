import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import CreateImageArtifactDialog from './CreateImageArtifactDialog.vue'
import { useWorkspaceStore } from '@/stores/workspaces'
import { useRunnerStore } from '@/stores/runners'
import { useImageArtifactStore } from '@/stores/imageArtifacts'
import { WorkspaceOperation, WorkspaceStatus, RuntimeType, type Workspace } from '@/types'

vi.mock('@/services/workspaces.api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/workspaces.api')>()),
  listCapturedImages: vi.fn(async () => []),
}))

beforeEach(() => setActivePinia(createPinia()))
it.each([WorkspaceStatus.RUNNING, WorkspaceStatus.STOPPED])(
  'captures %s without approval as a new image of the workspace',
  async (status) => {
    const workspaces = useWorkspaceStore()
    workspaces.workspaces = [
      {
        id: 'w',
        name: 'Guest',
        runner_id: 'r',
        status,
        runtime_type: RuntimeType.QEMU,
        active_operation: null,
        credentials_present: false,
      },
    ] as Workspace[]
    const runners = useRunnerStore()
    runners.runners = [
      { id: 'r', available_runtimes: ['qemu'], capabilities: { runtimes: ['qemu'] } },
    ] as never
    vi.spyOn(runners, 'runnerById').mockReturnValue({
      id: 'r',
      available_runtimes: ['qemu'],
    } as never)
    const images = useImageArtifactStore()
    const create = vi.spyOn(images, 'createImageArtifact').mockResolvedValue(true)
    const wrapper = mount(CreateImageArtifactDialog, {
      global: {
        stubs: {
          ...Object.fromEntries(
            [
              'Dialog',
              'DialogContent',
              'DialogBody',
              'DialogHeader',
              'DialogTitle',
              'DialogDescription',
              'DialogFooter',
              'DialogTrigger',
              'SelectContent',
              'SelectItem',
              'SelectTrigger',
              'SelectValue',
            ].map((name) => [name, { template: '<div><slot /></div>' }]),
          ),
          Select: {
            props: ['modelValue'],
            emits: ['update:modelValue'],
            template:
              '<button data-testid="select" @click="$emit(\'update:modelValue\', \'w\')"><slot /></button>',
          },
        },
      },
    })
    await wrapper.get('input').setValue('Snapshot')
    await wrapper.get('[data-testid="select"]').trigger('click')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[role="checkbox"]').exists()).toBe(false)
    expect(create).toHaveBeenCalledWith({ name: 'Snapshot', message: '', workspace_id: 'w' })
    wrapper.unmount()
  },
)

it('selects a workspace with the real Reka select', async () => {
  const workspaces = useWorkspaceStore()
  workspaces.workspaces = [{ id: 'w', name: 'Guest', runner_id: 'r', status: WorkspaceStatus.RUNNING, runtime_type: RuntimeType.QEMU, active_operation: null, credentials_present: false }] as Workspace[]
  const runners = useRunnerStore()
  runners.runners = [{ id: 'r', available_runtimes: ['qemu'] }] as never
  const create = vi.spyOn(useImageArtifactStore(), 'createImageArtifact').mockResolvedValue(true)
  const wrapper = mount(CreateImageArtifactDialog, { attachTo: document.body })
  await wrapper.get('button').trigger('click')
  await flushPromises()
  const input = document.body.querySelector('input')!
  input.value = 'Snapshot'
  input.dispatchEvent(new Event('input', { bubbles: true }))
  const trigger = document.body.querySelector('[role="combobox"]') as HTMLButtonElement
  trigger.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'ArrowDown' }))
  await flushPromises()
  const option = document.body.querySelector('[role="option"]') as HTMLElement
  expect(option.textContent).toContain('Guest')
  option.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'Enter' }))
  await flushPromises()
  expect(trigger.textContent).toContain('Guest')
  workspaces.updateWorkspaceOperation('w', WorkspaceOperation.CAPTURING_IMAGE)
  await flushPromises()
  expect(input.disabled).toBe(true)
  expect(trigger.disabled).toBe(true)
  const submit = document.body.querySelector('button[type="submit"]') as HTMLButtonElement
  expect(submit.disabled).toBe(true)
  workspaces.updateWorkspaceOperation('w', null)
  await flushPromises()
  expect(submit.disabled).toBe(false)
  document.body.querySelector('form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  await flushPromises()
  expect(create).toHaveBeenCalledWith({ name: 'Snapshot', message: '', workspace_id: 'w' })
  wrapper.unmount()
})
