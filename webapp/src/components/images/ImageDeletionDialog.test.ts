import { describe, it, vi, beforeEach, expect } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ImageDeletionDialog from './ImageDeletionDialog.vue'
const api = vi.hoisted(() => ({
  listDeletions: vi.fn(),
  previewDeletion: vi.fn(),
  requestDeletion: vi.fn(),
  cancelDeletion: vi.fn(),
}))
vi.mock('@/services/runnerStorage.api', () => api)
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ isAdmin: true }) }))
const graph = {
  fingerprint: 'exact-graph',
  blockers: [],
  counts: { images: 1, workspaces: 1 },
  images: [{ id: 'i', name: 'Base', owner_id: '1' }],
  workspaces: [{ id: 'w', name: 'Other owner data', owner_id: '2' }],
}
const stubs = Object.fromEntries(
  [
    'Dialog',
    'DialogContent',
    'DialogBody',
    'DialogHeader',
    'DialogTitle',
    'DialogDescription',
    'DialogFooter',
  ].map((n) => [n, { template: '<div><slot /></div>' }]),
)
const mountDialog = () =>
  mount(ImageDeletionDialog, {
    props: { target: { target_type: 'image', target_id: 'i' } },
    global: {
      stubs: {
        ...stubs,
        Checkbox: {
          props: ['modelValue', 'disabled'],
          emits: ['update:modelValue'],
          template:
            '<button :disabled="disabled" @click="$emit(\'update:modelValue\', !modelValue)">Approve</button>',
        },
      },
    },
  })
describe('durable deletion confirmation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listDeletions.mockResolvedValue([])
    api.previewDeletion.mockResolvedValue(graph)
    api.requestDeletion.mockResolvedValue({
      id: 'd',
      phase: 'waiting_dependency',
      can_cancel: true,
      diagnostic: 'Waiting on pins',
    })
  })
  it('defaults deferred, displays waiting diagnostic, cancels only server-authorized request', async () => {
    const w = mountDialog()
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Store deferred deletion intent')!
      .trigger('click')
    await flushPromises()
    expect(api.requestDeletion).toHaveBeenCalledWith(
      { target_type: 'image', target_id: 'i' },
      'deferred',
      '',
    )
    expect(w.text()).toContain('Waiting on pins')
    api.cancelDeletion.mockResolvedValue({ id: 'd', phase: 'cancelled', can_cancel: false })
    await w
      .findAll('button')
      .find((b) => b.text() === 'Cancel pending request')!
      .trigger('click')
    await flushPromises()
    expect(api.cancelDeletion).toHaveBeenCalledWith('d')
    expect(w.text()).not.toContain('Cancel pending request')
    w.unmount()
  })
  it('requires explicit fingerprint approval and a second approval for a changed graph', async () => {
    const w = mountDialog()
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Review force deletion…')!
      .trigger('click')
    expect(w.text()).toContain('Other owner data')
    const confirm = () =>
      w.findAll('button').find((b) => b.text() === 'Confirm permanent deletion')!
    expect(confirm().attributes('disabled')).toBeDefined()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Approve')!
      .trigger('click')
    api.requestDeletion.mockRejectedValue(new Error('Graph changed'))
    api.previewDeletion.mockResolvedValue({ ...graph, fingerprint: 'new-graph' })
    await confirm().trigger('click')
    await flushPromises()
    expect(confirm().attributes('disabled')).toBeDefined()
    expect(api.requestDeletion).toHaveBeenCalledWith(
      { target_type: 'image', target_id: 'i' },
      'force',
      'exact-graph',
    )
    await w
      .findAll('button')
      .find((b) => b.text() === 'Approve')!
      .trigger('click')
    api.requestDeletion.mockResolvedValue({ id: 'd', phase: 'executing', can_cancel: false })
    await confirm().trigger('click')
    await flushPromises()
    expect(api.requestDeletion).toHaveBeenLastCalledWith(
      { target_type: 'image', target_id: 'i' },
      'force',
      'new-graph',
    )
    w.unmount()
  })
  it('blocks force on stale/incomplete preview', async () => {
    api.previewDeletion.mockResolvedValue({ ...graph, blockers: ['Runner offline / stale scan'] })
    const w = mountDialog()
    await flushPromises()
    await w
      .findAll('button')
      .find((b) => b.text() === 'Review force deletion…')!
      .trigger('click')
    expect(w.text()).toContain('Runner offline / stale scan')
    expect(
      w
        .findAll('button')
        .find((b) => b.text() === 'Confirm permanent deletion')!
        .attributes('disabled'),
    ).toBeDefined()
    w.unmount()
  })
})
