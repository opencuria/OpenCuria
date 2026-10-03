import { mount, flushPromises } from '@vue/test-utils'
import { it, expect, vi } from 'vitest'
import WorkspaceImageArtifactDialog from './WorkspaceImageArtifactDialog.vue'
const api = vi.hoisted(() => ({ createImageArtifact: vi.fn() }))
vi.mock('@/stores/workspaces', () => ({ useWorkspaceStore: () => api }))
it('requires explicit running QEMU stop/capture/restart approval and retains failed proof errors', async () => {
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
  expect(api.createImageArtifact).not.toHaveBeenCalled()
  await w
    .findAll('button')
    .find((b) => b.text() === 'Approve')!
    .trigger('click')
  await w.get('form').trigger('submit')
  await flushPromises()
  expect(api.createImageArtifact).toHaveBeenCalledWith('w', {
    name: 'Capture',
    stop_and_restart: true,
  })
  expect(w.emitted('update:open')).toBeUndefined()
  expect(w.text()).toContain('controlled credential scrub proof')
  w.unmount()
})
