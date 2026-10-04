import { mount, flushPromises } from '@vue/test-utils'
import { it, expect, vi } from 'vitest'
import WorkspaceImageArtifactDialog from './WorkspaceImageArtifactDialog.vue'
const api = vi.hoisted(() => ({
  createImageArtifact: vi.fn(),
  isWorkspaceTransitioning: () => false,
}))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => api }))
it('automatically captures a running QEMU workspace without approval and retains failure errors', async () => {
  api.createImageArtifact.mockResolvedValue(false)
  const w = mount(WorkspaceImageArtifactDialog, {
    props: {
      workspace: { id: 'w', name: 'Running', status: 'running', runtime_type: 'qemu' } as never,
      open: true,
    },
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
          ].map((n) => [n, { template: '<div><slot /></div>' }]),
        ),
        Checkbox: {
          props: ['modelValue'],
          emits: ['update:modelValue'],
          template: '<button @click="$emit(\'update:modelValue\', !modelValue)">Approve</button>',
        },
      },
    },
  })
  await w.get('input').setValue('Capture')
  await w.get('form').trigger('submit')
  expect(api.createImageArtifact).toHaveBeenCalledWith('w', {
    name: 'Capture',
  })
  expect(w.emitted('update:open')).toBeUndefined()
  expect(w.text()).toContain('controlled credential scrub proof')
  w.unmount()
})
