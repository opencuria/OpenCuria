import { mount, flushPromises } from '@vue/test-utils'
import { beforeEach, it, expect, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import WorkspaceImageArtifactDialog from './WorkspaceImageArtifactDialog.vue'
import type { CapturedImage } from '@/types'

const api = vi.hoisted(() => ({
  createImageArtifact: vi.fn(),
  isWorkspaceTransitioning: () => false,
}))
const lines = vi.hoisted(() => ({ value: [] as CapturedImage[] }))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => api }))
vi.mock('@/services/workspaces.api', () => ({
  listCapturedImages: vi.fn(async () => lines.value),
}))

const stubs = {
  ...Object.fromEntries(
    [
      'Dialog',
      'DialogContent',
      'DialogBody',
      'DialogHeader',
      'DialogTitle',
      'DialogDescription',
      'DialogFooter',
      'SelectContent',
      'SelectItem',
      'SelectSeparator',
      'SelectTrigger',
      'SelectValue',
    ].map((n) => [n, { template: '<div><slot /></div>' }]),
  ),
  Select: {
    props: ['modelValue'],
    template: '<div data-testid="target" :data-value="modelValue"><slot /></div>',
  },
}

beforeEach(() => {
  setActivePinia(createPinia())
  api.createImageArtifact.mockReset()
  lines.value = []
})

it('captures a running QEMU workspace as a new image and retains failure errors', async () => {
  api.createImageArtifact.mockResolvedValue(false)
  const w = mount(WorkspaceImageArtifactDialog, {
    props: {
      workspace: { id: 'w', name: 'Running', status: 'running', runtime_type: 'qemu' } as never,
      open: true,
    },
    global: { stubs },
  })
  await flushPromises()
  await w.get('[data-testid="capture-name-input"]').setValue('Capture')
  await w.get('[data-testid="capture-message-input"]').setValue('  Added tools ')
  await w.get('form').trigger('submit')
  expect(api.createImageArtifact).toHaveBeenCalledWith('w', {
    name: 'Capture',
    message: 'Added tools',
  })
  expect(w.emitted('update:open')).toBeUndefined()
  expect(w.text()).toContain('controlled credential scrub proof')
  w.unmount()
})

it('preselects the next version of the image the workspace is based on', async () => {
  api.createImageArtifact.mockResolvedValue(true)
  lines.value = [
    {
      id: 'line-1',
      name: 'Node dev',
      status: 'active',
      runner_id: 'r',
      runner_online: true,
      versions: [{ id: 'v2', version: 2, status: 'ready' }],
    } as never,
  ]
  const w = mount(WorkspaceImageArtifactDialog, {
    props: {
      workspace: {
        id: 'w',
        name: 'Running',
        status: 'running',
        runtime_type: 'qemu',
        runner_id: 'r',
        base_image: { line_kind: 'captured', line_id: 'line-1' },
      } as never,
      open: true,
    },
    global: { stubs },
  })
  await flushPromises()
  expect(w.get('[data-testid="target"]').attributes('data-value')).toBe('line-1')
  expect(w.text()).toContain('Node dev · v3')
  expect(w.find('[data-testid="capture-name-input"]').exists()).toBe(false)
  await w.get('form').trigger('submit')
  expect(api.createImageArtifact).toHaveBeenCalledWith('w', {
    captured_image_id: 'line-1',
    message: '',
  })
  w.unmount()
})
